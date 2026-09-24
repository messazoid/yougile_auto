import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


HEALTHCHECK_PATH = Path(__file__).resolve().parents[1] / "docker" / "healthcheck.py"
SPEC = importlib.util.spec_from_file_location("docker_healthcheck", HEALTHCHECK_PATH)
healthcheck = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(healthcheck)


class CisnetRunnerHealthcheckTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.heartbeat = Path(self.temporary.name) / "runner.heartbeat"
        self.heartbeat.write_text("fresh\n")

    def test_runner_health_requires_reachable_browser_cdp(self):
        with patch.object(healthcheck, "HEARTBEAT", self.heartbeat), \
             patch.object(healthcheck, "check_database"), \
             patch.object(healthcheck, "urlopen", side_effect=ConnectionRefusedError), \
             patch.dict(os.environ, {"CISNET_CDP_ENDPOINT": "http://127.0.0.1:9223"}), \
             patch.object(sys, "argv", ["healthcheck.py", "cisnet-runner"]):
            with self.assertRaises(ConnectionRefusedError):
                healthcheck.main()

    def test_runner_health_passes_with_browser_cdp(self):
        with patch.object(healthcheck, "HEARTBEAT", self.heartbeat), \
             patch.object(healthcheck, "check_database"), \
             patch.object(healthcheck, "urlopen") as request, \
             patch.dict(os.environ, {"CISNET_CDP_ENDPOINT": "http://127.0.0.1:9223"}), \
             patch.object(sys, "argv", ["healthcheck.py", "cisnet-runner"]):
            request.return_value.__enter__.return_value.status = 200
            self.assertEqual(healthcheck.main(), 0)
            request.assert_called_once_with("http://127.0.0.1:9223/json/version", timeout=3)


if __name__ == "__main__":
    unittest.main()
