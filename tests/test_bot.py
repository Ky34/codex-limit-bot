import json
import unittest
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import bot


def windows(five_hour_remaining=50, weekly_remaining=60, five_hour_reset=2000, weekly_reset=9000):
    return {
        300: {"usedPercent": 100 - five_hour_remaining, "resetsAt": five_hour_reset},
        10080: {"usedPercent": 100 - weekly_remaining, "resetsAt": weekly_reset},
    }


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


class CommandTests(unittest.TestCase):
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
    def test_one_time_location_changes_offset_without_saving_coordinates(self, telegram):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "timezone.json"
            berlin = {"message": {"chat": {"id": 123},
                "location": {"longitude": 13.4, "latitude": 52.52}}}
            bot.handle_update(berlin, "token", "123", "codex", "limits", path)
            self.assertEqual(bot.read_timezone_state(path)["zone"], "Europe/Berlin")
            self.assertIn("Смещение изменилось", telegram.call_args.args[2]["text"])
            self.assertNotIn("longitude", path.read_text(encoding="utf-8"))
            self.assertNotIn("latitude", path.read_text(encoding="utf-8"))
            self.assertEqual(telegram.call_count, 1)

    @patch("bot.telegram_request")
    def test_same_offset_new_zone_is_saved_without_change_notice(self, telegram):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "timezone.json"
            bot.save_state(path, {"zone": "Europe/Berlin"})
            paris = {"message": {"chat": {"id": 123},
                "location": {"longitude": 2.35, "latitude": 48.86}}}
            bot.handle_update(paris, "token", "123", "codex", "limits", path)
            self.assertEqual(bot.read_timezone_state(path)["zone"], "Europe/Paris")
            self.assertIn("Смещение времени не изменилось", telegram.call_args.args[2]["text"])

    @patch("bot.telegram_request")
    def test_timezone_command_requests_one_time_location(self, telegram):
        bot.handle_update({"message": {"chat": {"id": 123}, "text": "/timezone"}},
                          "token", "123", "codex", "limits")
        reply = telegram.call_args.args[2]
        self.assertTrue(json.loads(reply["reply_markup"])["keyboard"][0][0]["request_location"])

    @patch("bot.telegram_request")
    def test_live_location_edit_does_not_update_zone(self, telegram):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "timezone.json"
            bot.save_state(path, {"zone": "Europe/Minsk"})
            edited = {"edited_message": {"chat": {"id": 123},
                "location": {"longitude": 13.4, "latitude": 52.52}}}
            bot.handle_update(edited, "token", "123", "codex", "limits", path)
            self.assertEqual(bot.read_timezone_state(path)["zone"], "Europe/Minsk")
            telegram.assert_not_called()

    @patch("bot.telegram_request")
    def test_phone_offset_prompt_only_when_shifted_and_once(self, telegram):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "timezone.json"
            bot.save_state(path, {"zone": "Europe/Minsk"})
            current = bot.format_utc_offset(bot.utc_offset("Europe/Minsk"))[3:]
            shifted = "+02:00"
            self.assertNotEqual(current, shifted)
            same = {"message": {"chat": {"id": 123}, "text": f"/tzcheck {current}"}}
            moved = {"message": {"chat": {"id": 123}, "text": f"/tzcheck {shifted}"}}
            bot.handle_update(same, "token", "123", "codex", "limits", path)
            telegram.assert_not_called()
            bot.handle_update(moved, "token", "123", "codex", "limits", path)
            bot.handle_update(moved, "token", "123", "codex", "limits", path)
            self.assertEqual(telegram.call_count, 1)
            self.assertIn("Отправьте /timezone", telegram.call_args.args[2]["text"])
            bot.handle_update(same, "token", "123", "codex", "limits", path)
            bot.handle_update(moved, "token", "123", "codex", "limits", path)
            self.assertEqual(telegram.call_count, 2)

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
