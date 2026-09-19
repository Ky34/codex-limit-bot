"""Telegram alerts for the five-hour and weekly Codex usage limits."""

from __future__ import annotations

import argparse
import json
import os
import queue
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


LIMITS = {300: "Пятичасовой", 10080: "Недельный"}
try:
    MOSCOW = ZoneInfo("Europe/Moscow")
except ZoneInfoNotFoundError:
    MOSCOW = timezone(timedelta(hours=3))


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
    if remaining <= 10:
        return 3
    if remaining <= 20:
        return 2
    if remaining <= 30:
        return 1
    return 0


def format_percent(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def format_message(duration: int, remaining: float, resets_at: int) -> str:
    reset = datetime.fromtimestamp(resets_at, MOSCOW).strftime("%d.%m.%Y в %H:%M МСК")
    icon = {1: "⚠️", 2: "🟠", 3: "🔴"}[severity(remaining)]
    return (
        f"{icon} {LIMITS[duration]} лимит Codex: осталось {format_percent(remaining)}%.\n"
        f"Сброс: {reset}."
    )


def notifications(windows: dict[int, dict], state: dict) -> tuple[list[tuple[int, str]], dict]:
    updated = dict(state)
    pending: list[tuple[int, str]] = []
    for duration, window in windows.items():
        used = float(window["usedPercent"])
        remaining = max(0.0, min(100.0, 100.0 - used))
        resets_at = int(window["resetsAt"])
        level = severity(remaining)
        key = str(duration)
        previous = updated.get(key, {})
        previous_level = previous.get("level", 0) if previous.get("resetsAt") == resets_at else 0
        if level > previous_level:
            pending.append((duration, format_message(duration, remaining, resets_at)))
        updated[key] = {"resetsAt": resets_at, "level": max(previous_level, level)}
    return pending, updated


def telegram_request(token: str, method: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}", data=body, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
    except (HTTPError, URLError):
        raise RuntimeError(f"Telegram {method} request failed") from None
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram {method} failed")
    return payload


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path("/var/lib/codex-limit-bot/state.json"))
    parser.add_argument("--codex", default="/root/.local/bin/codex")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--discover-chat", action="store_true")
    args = parser.parse_args()

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

    state = json.loads(args.state.read_text(encoding="utf-8")) if args.state.exists() else {}
    try:
        response = read_rate_limits(args.codex)
        windows = codex_windows(response)
    except Exception:
        if not args.dry_run and not state.get("_error_sent"):
            telegram_request(
                token,
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": "⚠️ Мониторинг лимитов Codex временно не получает данные. Проверю снова через 10 минут.",
                },
            )
            state["_error_sent"] = True
            save_state(args.state, state)
        raise
    if state.pop("_error_sent", False) and not args.dry_run:
        telegram_request(
            token,
            "sendMessage",
            {"chat_id": chat_id, "text": "✅ Мониторинг лимитов Codex восстановлен."},
        )
        save_state(args.state, state)
    pending, updated = notifications(windows, state)
    for duration, message in pending:
        if args.dry_run:
            print(message)
        else:
            telegram_request(token, "sendMessage", {"chat_id": chat_id, "text": message})
            state[str(duration)] = updated[str(duration)]
            save_state(args.state, state)
    if not args.dry_run:
        save_state(args.state, updated)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"codex-limit-bot: {exc}", file=sys.stderr)
        sys.exit(1)
