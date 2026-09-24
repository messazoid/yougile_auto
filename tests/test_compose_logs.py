import importlib.util
from pathlib import Path
import unittest


MODULE = Path(__file__).resolve().parents[1] / "docker" / "compose_logs.py"
SPEC = importlib.util.spec_from_file_location("compose_logs", MODULE)
assert SPEC and SPEC.loader
compose_logs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compose_logs)


class ComposeLogsTests(unittest.TestCase):
    def test_http_event_uses_only_safe_fields(self):
        line = (
            'receiver-1  | [YOUGILE-HTTP] '
            '{"direction":"in","method":"POST","route":"/webhooks/yougile/private",'
            '"status":200,"duration_ms":12,"kind":"accepted","authorization":"secret"}\n'
        )
        output = compose_logs.render(line, http_only=True)
        self.assertIn("YouGile -> server", output)
        self.assertIn("/webhooks/yougile/<hidden>", output)
        self.assertNotIn("private", output)
        self.assertNotIn("secret", output)

    def test_generic_log_hides_url_query_and_credentials(self):
        line = "worker-1  | URL https://example.org/file?token=private Authorization: Bearer secret\n"
        output = compose_logs.render(line)
        self.assertNotIn("private", output)
        self.assertNotIn("secret", output)
        self.assertIn("?<hidden>", output)

    def test_http_only_filters_other_messages(self):
        self.assertIsNone(compose_logs.render("receiver-1  | INFO: ready\n", http_only=True))


if __name__ == "__main__":
    unittest.main()
