import unittest

from yougile_webhooks import callback_url, location_filters, matches


class YouGileWebhookTests(unittest.TestCase):
    def test_callback_requires_https_and_encodes_secret(self):
        self.assertEqual(
            callback_url("https://example.test/", "private/value"),
            "https://example.test/webhooks/yougile/private%2Fvalue",
        )
        with self.assertRaises(ValueError):
            callback_url("http://example.test", "private")

    def test_matching_subscription_requires_event_url_and_columns(self):
        columns = {"column-b", "column-a"}
        item = {
            "id": "webhook-1",
            "url": "https://example.test/webhooks/yougile/private",
            "event": "task-moved",
            "filters": location_filters(columns),
        }
        self.assertTrue(matches(item, "task-moved", item["url"], columns))
        self.assertFalse(matches(item, "chat_message-created", item["url"], columns))
        self.assertFalse(matches({**item, "disabled": True}, "task-moved", item["url"], columns))


if __name__ == "__main__":
    unittest.main()
