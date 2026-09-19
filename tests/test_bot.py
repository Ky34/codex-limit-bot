import unittest
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
        self.assertIn("⚠️ <b>Пятичасовой лимит Codex</b> — осталось <b>19%</b>", events[0][0])
        self.assertIn("<b>Недельный лимит Codex</b> — осталось <b>60%</b>", events[0][0])
        self.assertEqual(events[0][0].count("Пятичасовой лимит Codex"), 1)
        self.assertNotIn("Другой лимит", events[0][0])
        events, state = bot.notifications(windows(five_hour_remaining=9), state, now=1000)
        self.assertEqual(len(events), 1)
        self.assertIn("🟠 <b>Пятичасовой лимит Codex</b> — осталось <b>9%</b>", events[0][0])
        events, state = bot.notifications(windows(five_hour_remaining=4), state, now=1000)
        self.assertEqual(len(events), 1)
        self.assertIn("🔴 <b>Пятичасовой лимит Codex</b> — осталось <b>4%</b>", events[0][0])
        events, _ = bot.notifications(windows(five_hour_remaining=4), state, now=1000)
        self.assertEqual(events, [])

    def test_skipped_thresholds_make_one_message(self):
        events, _ = bot.notifications(windows(five_hour_remaining=4), {}, now=1000)
        self.assertEqual(len(events), 1)
        self.assertIn("🔴 <b>Пятичасовой лимит Codex</b> — осталось <b>4%</b>", events[0][0])
        self.assertEqual(events[0][0].count("Пятичасовой лимит Codex"), 1)

    def test_reset_alert_and_new_period(self):
        old = {"_thresholds_version": 2, "300": {"resetsAt": 2000, "level": 3, "usedPercent": 96}}
        events, state = bot.notifications(
            windows(five_hour_remaining=100, five_hour_reset=4000), old, now=2001
        )
        self.assertEqual(len(events), 1)
        self.assertIn("🔄 ⏱️ <b>Пятичасовой лимит Codex</b> сброшен", events[0][0])
        self.assertIn("📅 <b>Недельный лимит Codex</b> — осталось <b>60%</b>", events[0][0])
        self.assertEqual(events[0][0].count("Пятичасовой лимит Codex"), 1)
        events, _ = bot.notifications(
            windows(five_hour_remaining=100, five_hour_reset=4000), state, now=2002
        )
        self.assertEqual(events, [])

    def test_weekly_reset_alert_and_new_period(self):
        old = {"_thresholds_version": 2, "10080": {"resetsAt": 9000, "level": 2, "usedPercent": 92}}
        current = windows(weekly_remaining=100, weekly_reset=12000)
        events, state = bot.notifications(current, old, now=9001)
        self.assertEqual(len(events), 1)
        self.assertIn("🔄 📅 <b>Недельный лимит Codex</b> сброшен", events[0][0])
        self.assertIn("⏱️ <b>Пятичасовой лимит Codex</b> — осталось <b>50%</b>", events[0][0])
        self.assertEqual(events[0][0].count("Недельный лимит Codex"), 1)
        events, _ = bot.notifications(current, state, now=9002)
        self.assertEqual(events, [])

    def test_existing_10_percent_alert_does_not_hide_new_5_percent_alert(self):
        old = {"300": {"resetsAt": 2000, "level": 3}}
        events, state = bot.notifications(windows(five_hour_remaining=7), old, now=1000)
        self.assertEqual(events, [])
        events, _ = bot.notifications(windows(five_hour_remaining=4), state, now=1000)
        self.assertEqual(len(events), 1)
        self.assertIn("🔴 <b>Пятичасовой лимит Codex</b> — осталось <b>4%</b>", events[0][0])


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
        self.assertIn("⏱️ <b>Пятичасовой лимит Codex</b> — осталось <b>25%</b>", reply)
        self.assertIn("📅 <b>Недельный лимит Codex</b> — осталось <b>60%</b>", reply)
        self.assertIn("МСК.\n\n📅 <b>Недельный", reply)
        self.assertEqual(telegram.call_args.args[2]["parse_mode"], "HTML")

    @patch("bot.telegram_request")
    @patch("bot.read_rate_limits")
    def test_other_chat_cannot_request_limits(self, read_limits, telegram):
        bot.handle_update(
            {"message": {"chat": {"id": 999}, "text": "/limits"}}, "token", "123", "codex", "limits"
        )
        read_limits.assert_not_called()
        telegram.assert_not_called()


if __name__ == "__main__":
    unittest.main()
