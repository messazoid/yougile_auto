import importlib.util
from pathlib import Path
import tempfile
import unittest


MODULE_PATH = Path(__file__).resolve().parents[1] / "bin" / "update-allowed-columns.py"
SPEC = importlib.util.spec_from_file_location("update_allowed_columns", MODULE_PATH)
assert SPEC and SPEC.loader
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


class UpdateAllowedColumnsTests(unittest.TestCase):
    def test_merges_unique_ids_and_keeps_other_settings(self):
        first = "22222222-2222-4222-8222-222222222222"
        second = "88888888-8888-4888-8888-888888888888"
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / "music-verifier.env"
            env_file.write_text(
                f"WEBHOOK_SECRET=fixture\nYOUGILE_ALLOWED_COLUMN_IDS={first}\n",
                encoding="utf-8",
            )
            env_file.chmod(0o600)
            self.assertEqual(
                helper.update(env_file, f"{first},{second}"),
                f"YOUGILE_ALLOWED_COLUMN_IDS={first},{second}",
            )
            self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(env_file.read_text(),
                f"WEBHOOK_SECRET=fixture\nYOUGILE_ALLOWED_COLUMN_IDS={first},{second}\n")

    def test_rejects_invalid_id_without_changing_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / "music-verifier.env"
            env_file.write_text("YOUGILE_ALLOWED_COLUMN_IDS=\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid"):
                helper.update(env_file, "not-an-id")
            self.assertEqual(env_file.read_text(), "YOUGILE_ALLOWED_COLUMN_IDS=\n")


if __name__ == "__main__":
    unittest.main()
