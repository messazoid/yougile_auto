import json
import unittest

from yougile_logs import format_record, readable_message, scrub


class YouGileLogsTests(unittest.TestCase):
    def test_formats_safe_http_event(self):
        message = "[YOUGILE-HTTP] " + json.dumps({
            "direction": "out",
            "method": "POST",
            "route": "/chats/{chat_id}/messages",
            "kind": "notification_message",
            "status": 201,
            "duration_ms": 125,
            "outcome": "ok",
        })
        self.assertEqual(
            readable_message(message),
            "server -> YouGile | POST /chats/{chat_id}/messages | HTTP 201 | 125 ms | notification_message",
        )

    def test_formats_playwright_event(self):
        self.assertEqual(
            readable_message(
                '[CISNET-PLAYWRIGHT] {"step":"search.capture","event":"failed","code":"TIMEOUT"}'
            ),
            "Playwright search.capture | ERROR | TIMEOUT",
        )

    def test_scrubs_webhook_secret_and_bearer_value(self):
        value = scrub(
            "POST /webhooks/yougile/private-value Authorization: Bearer private-token"
        )
        self.assertNotIn("private-value", value)
        self.assertNotIn("private-token", value)
        self.assertIn("/webhooks/yougile/<hidden>", value)

    def test_normal_systemd_noise_is_hidden_but_failure_is_visible(self):
        base = {
            "__REALTIME_TIMESTAMP": "1790123400000000",
            "SYSLOG_IDENTIFIER": "systemd",
            "UNIT": "music-verifier-cisnet.service",
        }
        self.assertIsNone(format_record({**base, "MESSAGE": "Finished music-verifier-cisnet.service"}))
        rendered = format_record({**base, "MESSAGE": "music-verifier-cisnet.service: Failed with result exit-code"})
        self.assertIn("CIS-NET", rendered)
        self.assertIn("Failed", rendered)

    def test_http_only_hides_non_http_events(self):
        record = {
            "MESSAGE": "[QUEUE] task_id=fixture state=queued",
            "SYSLOG_IDENTIFIER": "python",
        }
        self.assertIsNone(format_record(record, http_only=True))


if __name__ == "__main__":
    unittest.main()
