import io
import json
import os
import sys
import unittest
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, call, patch

import bot


def windows(five_hour_remaining=50, weekly_remaining=60, five_hour_reset=2000, weekly_reset=9000):
    return {
        300: {"usedPercent": 100 - five_hour_remaining, "resetsAt": five_hour_reset},
        10080: {"usedPercent": 100 - weekly_remaining, "resetsAt": weekly_reset},
    }


class BorderQueueTests(unittest.TestCase):
    def test_live_response_uses_car_live_queue_length(self):
        response = io.BytesIO(json.dumps({"carLiveQueue": [{}, {}], "busLiveQueue": [{}]}).encode())
        with patch("bot.urllib.request.urlopen", return_value=response) as opener:
            self.assertEqual(bot.read_border_car_count(), 2)
        self.assertEqual(opener.call_args.kwargs["timeout"], 10)

    def test_missing_car_queue_does_not_count_other_queues(self):
        response = io.BytesIO(json.dumps({"busLiveQueue": [{}]}).encode())
        with patch("bot.urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(ValueError, "carLiveQueue"):
                bot.read_border_car_count()

    def test_each_threshold_alerts_once_and_checks_every_fourteen_minutes(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path = Path(temp) / "border-state.json"
            with patch("bot.read_border_car_count", side_effect=[150, 151, 201, 251, 40, 251]) as reader, \
                 patch("bot.send_automatic_message") as sender:
                bot.check_border_queue("token", "123", state_path, now=0)
                bot.check_border_queue("token", "123", state_path, now=839)
                self.assertEqual(reader.call_count, 1)
                for now in (840, 1680, 2520, 3360, 4200):
                    bot.check_border_queue("token", "123", state_path, now=now)
            self.assertEqual(reader.call_count, 6)
            self.assertEqual(sender.call_count, 3)
            for threshold, call_args in zip(bot.BORDER_THRESHOLDS, sender.call_args_list):
                self.assertIn(str(threshold), call_args.args[2])
            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8"))["alerted_thresholds"],
                [150, 200, 250],
            )

    def test_first_read_above_all_thresholds_sends_each_once(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path = Path(temp) / "border-state.json"
            with patch("bot.read_border_car_count", return_value=260), \
                 patch("bot.send_automatic_message") as sender:
                bot.check_border_queue("token", "123", state_path, now=0)
                bot.check_border_queue("token", "123", state_path, now=840)
            self.assertEqual(sender.call_count, 3)

    def test_border_failure_does_not_block_codex_monitor(self):
        response = {
            "rateLimits": {
                "primary": {"usedPercent": 50, "resetsAt": 2000, "windowDurationMins": 300},
                "secondary": {"usedPercent": 40, "resetsAt": 9000, "windowDurationMins": 10080},
            }
        }
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(sys, "argv", ["bot.py", "--state", str(Path(temp) / "state.json")]), \
                 patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_CHAT_ID": "123"}), \
                 patch("bot.check_border_queue", side_effect=TimeoutError("border timeout")), \
                 patch("bot.read_rate_limits", return_value=response) as read_limits, \
                 patch("bot.send_automatic_message"):
                self.assertEqual(bot.main(), 0)
                read_limits.assert_called_once()


class NotificationTests(unittest.TestCase):
    def test_thresholds_and_other_limit(self):
        state = {}
        events, state = bot.notifications(windows(five_hour_remaining=25), state, now=1000)
        self.assertEqual(events, [])
        events, state = bot.notifications(windows(five_hour_remaining=19), state, now=1000)
        self.assertEqual(len(events), 1)
        self.assertIn("⚠️ <b>Пятичасовой лимит: порог 20% пройден!</b>", events[0][0])
        self.assertIn("⏱️ <b>Пятичасовой лимит</b>\n\nОсталось: <b>19%</b>", events[0][0])
        self.assertIn("📅 <b>Недельный лимит</b>\n\nОсталось: <b>60%</b>", events[0][0])
        self.assertIn("────────────────────", events[0][0])
        self.assertEqual(events[0][0].count('<tg-time unix='), 2)
        self.assertNotIn("Другой лимит", events[0][0])
        events, state = bot.notifications(windows(five_hour_remaining=9), state, now=1000)
        self.assertEqual(len(events), 1)
        self.assertIn("🟠 <b>Пятичасовой лимит: порог 10% пройден!</b>", events[0][0])
        self.assertIn("Осталось: <b>9%</b>", events[0][0])
        events, state = bot.notifications(windows(five_hour_remaining=4), state, now=1000)
        self.assertEqual(len(events), 1)
        self.assertIn("🔴 <b>Пятичасовой лимит: порог 5% пройден!</b>", events[0][0])
        self.assertIn("Осталось: <b>4%</b>", events[0][0])
        events, _ = bot.notifications(windows(five_hour_remaining=4), state, now=1000)
        self.assertEqual(events, [])

    def test_skipped_thresholds_make_one_message(self):
        events, _ = bot.notifications(windows(five_hour_remaining=4), {}, now=1000)
        self.assertEqual(len(events), 1)
        self.assertIn("🔴 <b>Пятичасовой лимит: порог 5% пройден!</b>", events[0][0])
        self.assertIn("Осталось: <b>4%</b>", events[0][0])

    def test_exhausted_limit_has_accurate_title(self):
        events, _ = bot.notifications(windows(five_hour_remaining=0), {}, now=1000)
        self.assertIn("🔴 <b>Пятичасовой лимит исчерпан!</b>", events[0][0])

    def test_exhausted_limit_is_reported_once_when_reset_time_moves(self):
        old = {"_thresholds_version": 2, "10080": {
            "resetsAt": 9000, "level": 3, "usedPercent": 96,
            "exhausted": False,
        }}
        current = windows(weekly_remaining=0, weekly_reset=9001)
        events, state = bot.notifications(current, old, now=1000)
        self.assertEqual(len(events), 1)
        self.assertIn("🔴 <b>Недельный лимит исчерпан!</b>", events[0][0])

        current = windows(weekly_remaining=0, weekly_reset=9002)
        events, state = bot.notifications(current, state, now=1001)
        self.assertEqual(events, [])
        self.assertTrue(state["10080"]["exhausted"])

    def test_legacy_exhausted_state_does_not_repeat_alert(self):
        old = {"_thresholds_version": 2, "10080": {
            "resetsAt": 9000, "level": 3, "usedPercent": 100,
        }}
        events, state = bot.notifications(
            windows(weekly_remaining=0, weekly_reset=9001), old, now=1000
        )
        self.assertEqual(events, [])
        self.assertTrue(state["10080"]["exhausted"])

    def test_reset_alert_and_new_period(self):
        old = {"_thresholds_version": 2, "300": {"resetsAt": 2000, "level": 3, "usedPercent": 96}}
        events, state = bot.notifications(
            windows(five_hour_remaining=100, five_hour_reset=4000), old, now=2001
        )
        self.assertEqual(len(events), 1)
        self.assertIn("🔄 <b>Пятичасовой лимит обновлён!</b>", events[0][0])
        self.assertIn("⏱️ <b>Пятичасовой лимит</b>\n\nОсталось: <b>100%</b>", events[0][0])
        self.assertIn("Следующее обновление:", events[0][0])
        self.assertIn("────────────────────", events[0][0])
        self.assertIn("📅 <b>Недельный лимит</b>\n\nОсталось: <b>60%</b>", events[0][0])
        events, _ = bot.notifications(
            windows(five_hour_remaining=100, five_hour_reset=4000), state, now=2002
        )
        self.assertEqual(events, [])

    def test_weekly_reset_alert_and_new_period(self):
        old = {"_thresholds_version": 2, "10080": {"resetsAt": 9000, "level": 2, "usedPercent": 92}}
        current = windows(weekly_remaining=100, weekly_reset=12000)
        events, state = bot.notifications(current, old, now=9001)
        self.assertEqual(len(events), 1)
        self.assertIn("🔄 <b>Недельный лимит обновлён!</b>", events[0][0])
        self.assertIn("📅 <b>Недельный лимит</b>\n\nОсталось: <b>100%</b>", events[0][0])
        self.assertIn("⏱️ <b>Пятичасовой лимит</b>\n\nОсталось: <b>50%</b>", events[0][0])
        events, _ = bot.notifications(current, state, now=9002)
        self.assertEqual(events, [])

    def test_reset_date_uses_russian_month(self):
        reset = int(datetime(2026, 9, 20, 5, 30, tzinfo=bot.MINSK).timestamp())
        message = bot.format_reset_alert(300, windows(five_hour_reset=reset))
        self.assertIn(
            f'Следующее обновление: <tg-time unix="{reset}" format="Dt">'
            '20 сентября, 05:30</tg-time>', message
        )

    def test_existing_10_percent_alert_does_not_hide_new_5_percent_alert(self):
        old = {"300": {"resetsAt": 2000, "level": 3}}
        events, state = bot.notifications(windows(five_hour_remaining=7), old, now=1000)
        self.assertEqual(events, [])
        events, _ = bot.notifications(windows(five_hour_remaining=4), state, now=1000)
        self.assertEqual(len(events), 1)
        self.assertIn("🔴 <b>Пятичасовой лимит: порог 5% пройден!</b>", events[0][0])
        self.assertIn("Осталось: <b>4%</b>", events[0][0])


class HealthCheckTests(unittest.TestCase):
    @patch("bot.telegram_request")
    @patch("bot.read_rate_limits")
    def test_health_check_validates_telegram_and_codex_without_sending(self, read_limits, telegram):
        telegram.side_effect = [
            {"result": {"id": 123, "is_bot": True}},
            {"result": {"id": -456, "type": "private"}},
        ]
        read_limits.return_value = {
            "rateLimits": {
                "primary": {"usedPercent": 25, "resetsAt": 2000, "windowDurationMins": 300},
                "secondary": {"usedPercent": 40, "resetsAt": 9000, "windowDurationMins": 10080},
            }
        }
        with patch.object(sys, "argv", ["bot.py", "--health-check", "--codex", "codex"]), \
             patch.dict(os.environ, {
                 "TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_CHAT_ID": "-456",
             }, clear=True):
            self.assertEqual(bot.main(), 0)
        self.assertEqual(telegram.call_args_list, [
            call("token", "getMe", {}),
            call("token", "getChat", {"chat_id": "-456"}),
        ])
        read_limits.assert_called_once_with("codex")

    @patch("bot.telegram_request", return_value={"result": {"id": 123, "is_bot": False}})
    def test_health_check_rejects_non_bot_identity(self, telegram):
        with patch.object(sys, "argv", ["bot.py", "--health-check"]), \
             patch.dict(os.environ, {
                 "TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_CHAT_ID": "-456",
             }, clear=True):
            with self.assertRaisesRegex(RuntimeError, "invalid bot identity"):
                bot.main()

    @patch("bot.telegram_request")
    def test_health_check_rejects_unexpected_chat(self, telegram):
        telegram.side_effect = [
            {"result": {"id": 123, "is_bot": True}},
            {"result": {"id": -999, "type": "private"}},
        ]
        with patch.object(sys, "argv", ["bot.py", "--health-check"]), \
             patch.dict(os.environ, {
                 "TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_CHAT_ID": "-456",
             }, clear=True):
            with self.assertRaisesRegex(RuntimeError, "unexpected chat"):
                bot.main()


class AvailabilityAlertTests(unittest.TestCase):
    response = {
        "rateLimits": {
            "primary": {"usedPercent": 50, "resetsAt": 2000, "windowDurationMins": 300},
            "secondary": {"usedPercent": 40, "resetsAt": 9000, "windowDurationMins": 10080},
        }
    }

    def run_monitor(self, state_path, read_limits, sender, expect_error=False):
        with patch.object(sys, "argv", ["bot.py", "--state", str(state_path), "--codex", "codex"]), \
             patch.dict(os.environ, {
                 "TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_CHAT_ID": "123",
             }, clear=True), \
             patch("bot.read_rate_limits", read_limits), \
             patch("bot.check_border_queue"), \
             patch("bot.send_automatic_message", sender):
            if expect_error:
                with self.assertRaisesRegex(RuntimeError, "unavailable"):
                    bot.main()
            else:
                self.assertEqual(bot.main(), 0)

    def test_one_error_does_not_send_alert(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path = Path(temp) / "state.json"
            sender = Mock()
            self.run_monitor(state_path, Mock(side_effect=RuntimeError("unavailable")), sender, True)
            sender.assert_not_called()
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["_error_count"], 1)

    def test_one_error_then_success_sends_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path = Path(temp) / "state.json"
            sender = Mock()
            self.run_monitor(state_path, Mock(side_effect=RuntimeError("unavailable")), sender, True)
            self.run_monitor(state_path, Mock(return_value=self.response), sender)
            sender.assert_not_called()
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertNotIn("_error_count", state)
            self.assertNotIn("_error_sent", state)

    def test_two_errors_send_one_warning(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path = Path(temp) / "state.json"
            sender = Mock()
            failure = Mock(side_effect=RuntimeError("unavailable"))
            self.run_monitor(state_path, failure, sender, True)
            self.run_monitor(state_path, failure, sender, True)
            sender.assert_called_once()
            self.assertIn("временно не получает данные", sender.call_args.args[2])
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["_error_count"], 2)
            self.assertTrue(state["_error_sent"])

    def test_three_errors_still_send_one_warning(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path = Path(temp) / "state.json"
            sender = Mock()
            failure = Mock(side_effect=RuntimeError("unavailable"))
            for _ in range(3):
                self.run_monitor(state_path, failure, sender, True)
            sender.assert_called_once()
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["_error_count"], 3)

    def test_two_errors_then_success_send_warning_and_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path = Path(temp) / "state.json"
            sender = Mock()
            failure = Mock(side_effect=RuntimeError("unavailable"))
            self.run_monitor(state_path, failure, sender, True)
            self.run_monitor(state_path, failure, sender, True)
            self.run_monitor(state_path, Mock(return_value=self.response), sender)
            self.assertEqual(sender.call_count, 2)
            self.assertIn("временно не получает данные", sender.call_args_list[0].args[2])
            self.assertEqual(sender.call_args_list[1].args[2], "✅ Мониторинг лимитов Codex восстановлен.")

    def test_recovery_clears_state_for_a_new_single_error(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path = Path(temp) / "state.json"
            sender = Mock()
            failure = Mock(side_effect=RuntimeError("unavailable"))
            self.run_monitor(state_path, failure, sender, True)
            self.run_monitor(state_path, failure, sender, True)
            self.run_monitor(state_path, Mock(return_value=self.response), sender)
            self.run_monitor(state_path, failure, sender, True)
            self.assertEqual(sender.call_count, 2)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["_error_count"], 1)
            self.assertNotIn("_error_sent", state)

    def test_legacy_error_sent_state_recovers_without_migration(self):
        with tempfile.TemporaryDirectory() as temp:
            state_path = Path(temp) / "state.json"
            state_path.write_text(json.dumps({"_error_sent": True, "legacy": "kept"}), encoding="utf-8")
            sender = Mock()
            self.run_monitor(state_path, Mock(side_effect=RuntimeError("unavailable")), sender, True)
            sender.assert_not_called()
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["_error_count"], 2)
            self.run_monitor(state_path, Mock(return_value=self.response), sender)
            sender.assert_called_once_with("token", "123", "✅ Мониторинг лимитов Codex восстановлен.")
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertNotIn("_error_count", state)
            self.assertNotIn("_error_sent", state)
            self.assertEqual(state["legacy"], "kept")


class CommandTests(unittest.TestCase):
    @patch("bot.telegram_request")
    def test_timezone_button_requests_one_time_location(self, telegram):
        for command in ("/start", "/timezone"):
            with self.subTest(command=command):
                bot.handle_update(
                    {"message": {"chat": {"id": 123}, "text": command}},
                    "token", "123", "codex", "limits",
                )
                markup = json.loads(telegram.call_args.args[2]["reply_markup"])
                self.assertEqual(markup["keyboard"][0], [bot.STATUS_BUTTON])
                self.assertTrue(markup["keyboard"][1][0]["request_location"])
                self.assertTrue(markup["is_persistent"])

    @patch("bot.telegram_request")
    @patch("bot.read_rate_limits")
    def test_limits_button_uses_status_response(self, read_limits, telegram):
        read_limits.return_value = {
            "rateLimits": {
                "primary": {"usedPercent": 75, "resetsAt": 2000, "windowDurationMins": 300},
                "secondary": {"usedPercent": 40, "resetsAt": 9000, "windowDurationMins": 10080},
            }
        }
        bot.handle_update(
            {"message": {"chat": {"id": 123}, "text": bot.STATUS_BUTTON}},
            "token", "123", "codex", "limits",
        )
        reply = telegram.call_args.args[2]
        self.assertIn("Лимиты Codex сейчас", reply["text"])
        self.assertEqual(reply["text"].count('<tg-time unix='), 2)
        self.assertEqual(len(json.loads(reply["reply_markup"])["keyboard"]), 2)

    @patch("bot.telegram_request")
    @patch("bot.read_rate_limits")
    def test_status_command_replies_with_both_limits(self, read_limits, telegram):
        read_limits.return_value = {
            "rateLimits": {
                "primary": {"usedPercent": 75, "resetsAt": 2000, "windowDurationMins": 300},
                "secondary": {"usedPercent": 40, "resetsAt": 9000, "windowDurationMins": 10080},
            }
        }
        bot.handle_update(
            {"message": {"chat": {"id": 123}, "text": "/limits"}}, "token", "123", "codex", "limits"
        )
        reply = telegram.call_args.args[2]["text"]
        self.assertIn("📊 <b>Лимиты Codex сейчас</b>", reply)
        self.assertIn("⏱️ <b>Пятичасовой лимит</b>\n\nОсталось: <b>25%</b>", reply)
        self.assertIn("📅 <b>Недельный лимит</b>\n\nОсталось: <b>60%</b>", reply)
        self.assertIn("────────────────────", reply)
        self.assertEqual(reply.count('<tg-time unix='), 2)
        self.assertEqual(telegram.call_args.args[2]["parse_mode"], "HTML")
        self.assertEqual(len(json.loads(telegram.call_args.args[2]["reply_markup"])["keyboard"]), 2)
        self.assertNotIn("disable_notification", telegram.call_args.args[2])

    @patch("bot.telegram_request")
    @patch("bot.read_rate_limits")
    def test_other_chat_cannot_request_limits(self, read_limits, telegram):
        bot.handle_update(
            {"message": {"chat": {"id": 999}, "text": "/limits"}}, "token", "123", "codex", "limits"
        )
        read_limits.assert_not_called()
        telegram.assert_not_called()


class QuietHoursTests(unittest.TestCase):
    @patch("bot.telegram_request")
    def test_button_location_saves_zone_for_quiet_hours(self, telegram):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "timezone.json"
            bot.handle_update(
                {"message": {"chat": {"id": 123}, "message_id": 42,
                             "location": {"longitude": 13.4, "latitude": 52.52}}},
                "token", "123", "codex", "limits", path,
            )
            state = bot.read_timezone_state(path)
            self.assertEqual(state["zone"], "Europe/Berlin")
            self.assertEqual(set(state), {"zone"})
            self.assertNotIn("latitude", path.read_text(encoding="utf-8"))
            self.assertIn("При следующем переезде нажмите кнопку", telegram.call_args.args[2]["text"])
            self.assertIn("reply_markup", telegram.call_args.args[2])
            self.assertTrue(bot.is_quiet_hours(
                datetime(2026, 9, 20, 0, 30, tzinfo=timezone.utc), path,
            ))

    def test_quiet_hours_boundaries(self):
        for hour, minute, expected in (
            (1, 59, False), (2, 0, True), (9, 59, True), (10, 0, False)
        ):
            with self.subTest(hour=hour, minute=minute):
                now = datetime(2026, 9, 20, hour, minute, tzinfo=bot.MINSK)
                self.assertEqual(bot.is_quiet_hours(now), expected)

    @patch("bot.telegram_request")
    @patch("bot.is_quiet_hours", return_value=True)
    def test_automatic_message_is_silent_at_night(self, quiet, telegram):
        bot.send_automatic_message("token", "123", "Тест", parse_mode="HTML")
        self.assertTrue(telegram.call_args.args[2]["disable_notification"])
        self.assertEqual(telegram.call_args.args[2]["parse_mode"], "HTML")

    @patch("bot.telegram_request")
    @patch("bot.is_quiet_hours", return_value=False)
    def test_automatic_message_is_normal_by_day(self, quiet, telegram):
        bot.send_automatic_message("token", "123", "Тест")
        self.assertNotIn("disable_notification", telegram.call_args.args[2])

    def test_local_quiet_hours_follow_zone_and_daylight_saving(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "timezone.json"
            bot.save_state(path, {"zone": "Europe/Berlin"})
            summer = datetime(2026, 9, 20, 0, 30, tzinfo=timezone.utc)
            winter = datetime(2026, 12, 20, 0, 30, tzinfo=timezone.utc)
            self.assertTrue(bot.is_quiet_hours(summer, path))
            self.assertFalse(bot.is_quiet_hours(winter, path))
            bot.save_state(path, {"zone": "Europe/Minsk"})
            self.assertTrue(bot.is_quiet_hours(winter, path))

    @patch("bot.telegram_request")
    def test_live_location_does_not_update_zone(self, telegram):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "timezone.json"
            bot.save_state(path, {"zone": "Europe/Minsk"})
            initial = {"message": {
                "chat": {"id": 123}, "message_id": 7, "date": 1790000000,
                "location": {"longitude": 13.4, "latitude": 52.52,
                             "live_period": 0x7FFFFFFF},
            }}
            bot.handle_update(initial, "token", "123", "codex", "limits", path)
            self.assertEqual(bot.read_timezone_state(path)["zone"], "Europe/Minsk")
            self.assertIn("Живая геолокация не используется", telegram.call_args.args[2]["text"])
            edited = {"edited_message": {
                "chat": {"id": 123}, "message_id": 7, "date": 1790000000,
                "location": {"longitude": 13.4, "latitude": 52.52,
                             "live_period": 0x7FFFFFFF},
            }}
            bot.handle_update(edited, "token", "123", "codex", "limits", path)
            self.assertEqual(bot.read_timezone_state(path)["zone"], "Europe/Minsk")
            self.assertNotIn("longitude", path.read_text(encoding="utf-8"))
            self.assertNotIn("latitude", path.read_text(encoding="utf-8"))
            self.assertEqual(telegram.call_count, 1)

    @patch("bot.telegram_request")
    def test_location_from_another_chat_cannot_change_zone(self, telegram):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "timezone.json"
            bot.handle_update({"message": {"chat": {"id": 999},
                "location": {"longitude": 13.4, "latitude": 52.52}}},
                "token", "123", "codex", "limits", path)
            self.assertFalse(path.exists())
            telegram.assert_not_called()


if __name__ == "__main__":
    unittest.main()
