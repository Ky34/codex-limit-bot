"""Telegram alerts for the five-hour and weekly Codex usage limits."""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from urllib.error import HTTPError, URLError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from tzfpy import get_tz


LIMITS = {300: "Пятичасовой", 10080: "Недельный"}
LIMIT_ICONS = {300: "⏱️", 10080: "📅"}
MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
THRESHOLDS = (20, 10, 5)
THRESHOLDS_VERSION = 2
DIVIDER = "────────────────────"
QUIET_START_HOUR = 2
QUIET_END_HOUR = 10
TIMEZONE_STATE = Path("/var/lib/codex-limit-bot/timezone.json")
try:
    MINSK = ZoneInfo("Europe/Minsk")
except ZoneInfoNotFoundError:
    MINSK = timezone(timedelta(hours=3))


def read_rate_limits(codex: str = "codex", timeout: int = 30) -> dict:
    proc = subprocess.Popen(
        [codex, "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    messages: queue.Queue[dict] = queue.Queue()

    def collect_stdout() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            try:
                messages.put(json.loads(line))
            except json.JSONDecodeError:
                continue

    def drain_stderr() -> None:
        assert proc.stderr is not None
        for _ in proc.stderr:
            pass

    threading.Thread(target=collect_stdout, daemon=True).start()
    threading.Thread(target=drain_stderr, daemon=True).start()

    def send(payload: dict) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        proc.stdin.flush()

    try:
        send(
            {
                "method": "initialize",
                "id": 1,
                "params": {
                    "clientInfo": {
                        "name": "codex_limit_notifier",
                        "title": "Codex Limit Notifier",
                        "version": "1.0.0",
                    }
                },
            }
        )
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Codex App Server did not initialize")
            try:
                message = messages.get(timeout=min(remaining, 1))
            except queue.Empty:
                if proc.poll() is not None:
                    raise RuntimeError("Codex App Server exited during initialization")
                continue
            if message.get("id") == 1:
                if "error" in message:
                    raise RuntimeError(f"Codex initialization failed: {message['error']}")
                break
        send({"method": "initialized", "params": {}})
        send({"method": "account/rateLimits/read", "id": 2})
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Codex rate limits request timed out")
            try:
                message = messages.get(timeout=min(remaining, 1))
            except queue.Empty:
                if proc.poll() is not None:
                    raise RuntimeError("Codex App Server exited before returning limits")
                continue
            if message.get("id") == 2:
                if "error" in message:
                    raise RuntimeError(f"Codex rate limits request failed: {message['error']}")
                return message["result"]
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def codex_windows(response: dict) -> dict[int, dict]:
    buckets = response.get("rateLimitsByLimitId") or {}
    bucket = buckets.get("codex") or response.get("rateLimits") or {}
    found: dict[int, dict] = {}
    for window in (bucket.get("primary"), bucket.get("secondary")):
        if not isinstance(window, dict):
            continue
        duration = window.get("windowDurationMins")
        if duration in LIMITS and window.get("usedPercent") is not None:
            found[duration] = window
    if set(found) != set(LIMITS):
        raise ValueError("Codex did not return both five-hour and weekly limits")
    return found


def severity(remaining: float) -> int:
    return sum(remaining <= threshold for threshold in THRESHOLDS)


def format_percent(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def remaining_percent(window: dict) -> float:
    return max(0.0, min(100.0, 100.0 - float(window["usedPercent"])))


def limit_block(duration: int, window: dict, next_update: bool = False) -> str:
    reset_at = int(window["resetsAt"])
    reset = datetime.fromtimestamp(reset_at, MINSK)
    fallback_date = f"{reset.day} {MONTHS[reset.month - 1]}, {reset:%H:%M}"
    date = f'<tg-time unix="{reset_at}" format="Dt">{fallback_date}</tg-time>'
    label = "Следующее обновление" if next_update else "Обновление"
    return (
        f"{LIMIT_ICONS[duration]} <b>{LIMITS[duration]} лимит</b>\n\n"
        f"Осталось: <b>{format_percent(remaining_percent(window))}%</b>\n"
        f"{label}: {date}"
    )


def other_duration(duration: int) -> int:
    return next(candidate for candidate in LIMITS if candidate != duration)


def format_status(windows: dict[int, dict]) -> str:
    return "📊 <b>Лимиты Codex сейчас</b>\n\n" + f"\n\n{DIVIDER}\n\n".join(
        limit_block(duration, windows[duration])
        for duration in LIMITS
    )


def format_threshold_alert(duration: int, windows: dict[int, dict]) -> str:
    remaining = remaining_percent(windows[duration])
    icon = {1: "⚠️", 2: "🟠", 3: "🔴"}[severity(remaining)]
    threshold = THRESHOLDS[severity(remaining) - 1]
    title = "исчерпан!" if remaining == 0 else f"порог {threshold}% пройден!"
    separator = " " if remaining == 0 else ": "
    other = other_duration(duration)
    return (
        f"{icon} <b>{LIMITS[duration]} лимит{separator}{title}</b>\n\n"
        f"{limit_block(duration, windows[duration], True)}\n\n"
        f"{DIVIDER}\n\n"
        f"{limit_block(other, windows[other])}"
    )


def format_reset_alert(duration: int, windows: dict[int, dict]) -> str:
    other = other_duration(duration)
    return (
        f"🔄 <b>{LIMITS[duration]} лимит обновлён!</b>\n\n"
        f"{limit_block(duration, windows[duration], True)}\n\n"
        f"{DIVIDER}\n\n"
        f"{limit_block(other, windows[other])}"
    )


def migrate_state(state: dict) -> dict:
    updated = dict(state)
    if updated.get("_thresholds_version", 1) < THRESHOLDS_VERSION:
        for duration in LIMITS:
            key = str(duration)
            if isinstance(updated.get(key), dict):
                previous = dict(updated[key])
                previous["level"] = max(0, int(previous.get("level", 0)) - 1)
                updated[key] = previous
        updated["_thresholds_version"] = THRESHOLDS_VERSION
    return updated


def notifications(
    windows: dict[int, dict], state: dict, now: int | None = None
) -> tuple[list[tuple[str, dict]], dict]:
    updated = migrate_state(state)
    pending: list[tuple[str, dict]] = []
    now = int(time.time()) if now is None else now
    for duration, window in windows.items():
        key = str(duration)
        previous = updated.get(key, {})
        resets_at = int(window["resetsAt"])
        used = float(window["usedPercent"])
        old_reset = previous.get("resetsAt")
        old_used = previous.get("usedPercent")
        reset_happened = old_reset is not None and old_reset != resets_at and (
            int(old_reset) <= now or (old_used is not None and used < float(old_used))
        )
        if reset_happened:
            updated[key] = {"resetsAt": resets_at, "level": 0, "usedPercent": used}
            pending.append((format_reset_alert(duration, windows), dict(updated)))
            previous_level = 0
        else:
            previous_level = int(previous.get("level", 0)) if old_reset == resets_at else 0

        level = severity(remaining_percent(window))
        if level > previous_level:
            updated[key] = {"resetsAt": resets_at, "level": level, "usedPercent": used}
            pending.append((format_threshold_alert(duration, windows), dict(updated)))
        else:
            updated[key] = {
                "resetsAt": resets_at,
                "level": max(previous_level, level),
                "usedPercent": used,
            }
    return pending, updated


def telegram_request(token: str, method: str, data: dict, timeout: int = 20) -> dict:
    body = urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}", data=body, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError):
        raise RuntimeError(f"Telegram {method} request failed") from None
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram {method} failed")
    return payload


def read_timezone_state(path: Path = TIMEZONE_STATE) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return {}


def local_zone(path: Path = TIMEZONE_STATE):
    try:
        return ZoneInfo(read_timezone_state(path).get("zone", "Europe/Minsk"))
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        return MINSK


def utc_offset(zone_name: str, now: datetime | None = None) -> timedelta:
    instant = now or datetime.now(timezone.utc)
    return instant.astimezone(ZoneInfo(zone_name)).utcoffset() or timedelta()


def format_utc_offset(value: timedelta) -> str:
    minutes = int(value.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    hours, remainder = divmod(abs(minutes), 60)
    return f"UTC{sign}{hours:02d}:{remainder:02d}"


def parse_utc_offset(value: str) -> timedelta | None:
    match = re.fullmatch(r"([+-])(\d{2}):(\d{2})", value)
    if not match:
        return None
    hours, minutes = int(match[2]), int(match[3])
    if hours > 14 or minutes >= 60 or (hours == 14 and minutes != 0):
        return None
    total = timedelta(hours=hours, minutes=minutes)
    return total if match[1] == "+" else -total


def is_quiet_hours(now: datetime | None = None, timezone_state_path: Path = TIMEZONE_STATE) -> bool:
    local_time = (now or datetime.now(timezone.utc)).astimezone(local_zone(timezone_state_path))
    return QUIET_START_HOUR <= local_time.hour < QUIET_END_HOUR


def send_automatic_message(
    token: str, chat_id: str, message: str, parse_mode: str | None = None
) -> None:
    data = {"chat_id": chat_id, "text": message}
    if parse_mode:
        data["parse_mode"] = parse_mode
    if is_quiet_hours():
        data["disable_notification"] = True
    telegram_request(token, "sendMessage", data)


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def handle_update(
    update: dict, token: str, chat_id: str, codex: str, status_command: str,
    timezone_state_path: Path = TIMEZONE_STATE,
) -> None:
    message = update.get("message") or {}
    if str((message.get("chat") or {}).get("id")) != chat_id:
        return
    location = message.get("location")
    if isinstance(location, dict):
        previous = read_timezone_state(timezone_state_path)
        try:
            latitude = float(location["latitude"])
            longitude = float(location["longitude"])
            if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
                return
            zone_name = get_tz(longitude, latitude)
            ZoneInfo(zone_name)
        except (KeyError, TypeError, ValueError, ZoneInfoNotFoundError):
            return
        old_zone = previous.get("zone", "Europe/Minsk")
        try:
            old_offset = utc_offset(old_zone)
        except (ZoneInfoNotFoundError, TypeError, ValueError):
            old_offset = utc_offset("Europe/Minsk")
        new_offset = utc_offset(zone_name)
        save_state(timezone_state_path, {"zone": zone_name})
        change = (
            f"Смещение изменилось: {format_utc_offset(old_offset)} → {format_utc_offset(new_offset)}.\n"
            if old_offset != new_offset else
            "Смещение времени не изменилось.\n"
        )
        telegram_request(token, "sendMessage", {
            "chat_id": chat_id,
            "text": (
                f"📍 Часовой пояс сохранён: {zone_name} ({format_utc_offset(new_offset)}).\n"
                f"{change}Тихие часы: 02:00–10:00 по местному времени."
            ),
            "reply_markup": json.dumps({"remove_keyboard": True}),
        })
        return
    text = message.get("text", "")
    command = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text.strip() else ""
    if command == "/tzcheck":
        parts = text.split()
        reported = parse_utc_offset(parts[1]) if len(parts) == 2 else None
        if reported is None:
            return
        state = read_timezone_state(timezone_state_path)
        saved = local_zone(timezone_state_path)
        expected = datetime.now(timezone.utc).astimezone(saved).utcoffset() or timedelta()
        marker = format_utc_offset(reported)
        if reported == expected:
            if state.pop("last_prompt_offset", None) is not None:
                save_state(timezone_state_path, state)
            return
        if state.get("last_prompt_offset") == marker:
            return
        state["last_prompt_offset"] = marker
        save_state(timezone_state_path, state)
        telegram_request(token, "sendMessage", {
            "chat_id": chat_id,
            "text": (
                f"🕒 Часовой пояс телефона изменился: {marker}. "
                f"Для тихих часов сейчас сохранено {format_utc_offset(expected)}.\n"
                "Отправьте /timezone и однократную геопозицию, чтобы обновить ночной режим."
            ),
        })
        return
    if command == "/timezone":
        telegram_request(token, "sendMessage", {
            "chat_id": chat_id,
            "text": "📍 Нажмите кнопку ниже, чтобы один раз отправить текущую геопозицию. Бот сохранит только часовой пояс.",
            "reply_markup": json.dumps({
                "keyboard": [[{"text": "📍 Отправить геопозицию", "request_location": True}]],
                "resize_keyboard": True,
                "one_time_keyboard": True,
            }, ensure_ascii=False),
        })
        return
    if command != f"/{status_command}":
        return
    try:
        windows = codex_windows(read_rate_limits(codex))
        reply = format_status(windows)
    except Exception:
        reply = "⚠️ Сейчас не удалось получить лимиты Codex. Попробуйте снова позже."
    telegram_request(
        token, "sendMessage", {"chat_id": chat_id, "text": reply, "parse_mode": "HTML"}
    )


def listen_commands(
    token: str, chat_id: str, codex: str, status_command: str,
    offset_path: Path, timezone_state_path: Path = TIMEZONE_STATE,
) -> None:
    offset = int(json.loads(offset_path.read_text(encoding="utf-8"))["offset"]) if offset_path.exists() else 0
    while True:
        try:
            updates = telegram_request(
                token,
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 25,
                    "allowed_updates": json.dumps(["message"]),
                },
                timeout=35,
            )["result"]
            for update in updates:
                handle_update(update, token, chat_id, codex, status_command, timezone_state_path)
                offset = int(update["update_id"]) + 1
                save_state(offset_path, {"offset": offset})
        except Exception as exc:
            print(f"codex-limit-bot listener: {exc}", file=sys.stderr)
            time.sleep(5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path("/var/lib/codex-limit-bot/state.json"))
    parser.add_argument("--codex", default="/root/.local/bin/codex")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--discover-chat", action="store_true")
    parser.add_argument("--listen", action="store_true")
    parser.add_argument("--offset", type=Path, default=Path("/var/lib/codex-limit-bot/updates.json"))
    parser.add_argument("--timezone-state", type=Path, default=TIMEZONE_STATE)
    parser.add_argument("--status-command", default=os.environ.get("TELEGRAM_STATUS_COMMAND", "limits"))
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", args.status_command):
        parser.error("--status-command must be a Telegram command without /")

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if args.discover_chat:
        if not token:
            parser.error("TELEGRAM_BOT_TOKEN is required")
        updates = telegram_request(token, "getUpdates", {"limit": 10})["result"]
        for update in updates:
            chat = (update.get("message") or {}).get("chat") or {}
            if chat.get("id") is not None:
                print(f"chat_id={chat['id']} name={chat.get('first_name', '')}")
        return 0
    if not args.dry_run and (not token or not chat_id):
        parser.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required")
    if args.listen:
        listen_commands(token, chat_id, args.codex, args.status_command, args.offset, args.timezone_state)
        return 0

    state = json.loads(args.state.read_text(encoding="utf-8")) if args.state.exists() else {}
    try:
        response = read_rate_limits(args.codex)
        windows = codex_windows(response)
    except Exception:
        if not args.dry_run and not state.get("_error_sent"):
            send_automatic_message(
                token, chat_id,
                "⚠️ Мониторинг лимитов Codex временно не получает данные. Проверю снова через минуту.",
            )
            state["_error_sent"] = True
            save_state(args.state, state)
        raise
    if state.pop("_error_sent", False) and not args.dry_run:
        send_automatic_message(token, chat_id, "✅ Мониторинг лимитов Codex восстановлен.")
        save_state(args.state, state)
    pending, updated = notifications(windows, state)
    for message, snapshot in pending:
        if args.dry_run:
            print(message)
        else:
            send_automatic_message(token, chat_id, message, parse_mode="HTML")
            save_state(args.state, snapshot)
    if not args.dry_run:
        save_state(args.state, updated)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"codex-limit-bot: {exc}", file=sys.stderr)
        sys.exit(1)
