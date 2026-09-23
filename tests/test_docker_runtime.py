import importlib.util
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "docker" / "init_data.py"
SPEC = importlib.util.spec_from_file_location("container_init_data", MODULE_PATH)
assert SPEC and SPEC.loader
init_data = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(init_data)


class DockerRuntimeTests(unittest.TestCase):
    def test_initializer_creates_clean_schema_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "data"
            database = init_data.initialize_data(root, os.getuid(), os.getgid())
            self.assertTrue(database.is_file())
            self.assertTrue((root / init_data.MARKER).is_file())
            with sqlite3.connect(database) as db:
                self.assertEqual(
                    db.execute("SELECT version FROM schema_info WHERE id=1").fetchone()[0],
                    12,
                )
                for table in init_data.OPERATIONAL_TABLES:
                    self.assertEqual(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0)
            self.assertEqual(
                init_data.initialize_data(root, os.getuid(), os.getgid()), database
            )

    def test_initializer_refuses_unmarked_nonempty_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "data"
            root.mkdir()
            (root / "old-result.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "non-empty unmarked"):
                init_data.initialize_data(root, os.getuid(), os.getgid())

    def test_initializer_does_not_recreate_missing_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "data"
            database = init_data.initialize_data(root, os.getuid(), os.getgid())
            database.unlink()
            with self.assertRaisesRegex(RuntimeError, "missing pipeline.sqlite3"):
                init_data.initialize_data(root, os.getuid(), os.getgid())


if __name__ == "__main__":
    unittest.main()
