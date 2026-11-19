import json
import tempfile
import unittest
from pathlib import Path

from soapalin.config import Config
from soapalin.inventory import load_snapshot
from soapalin.schema import SchemaError, validate_plan, validate_snapshot


class SchemaTests(unittest.TestCase):
    def test_snapshot_version(self):
        validate_snapshot({"version": 1, "side": "source", "accounts": []})
        with self.assertRaises(SchemaError):
            validate_snapshot({"version": 99})

    def test_plan_version(self):
        validate_plan({"version": 2, "actions": []})
        with self.assertRaises(SchemaError):
            validate_plan({"version": 1})

    def test_load_snapshot_rejects_unknown_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source-default-20260101.json"
            path.write_text(
                json.dumps({"version": 99, "side": "source"}),
                encoding="utf-8",
            )
            with self.assertRaises(SchemaError):
                load_snapshot(path)


class CorruptConfigTests(unittest.TestCase):
    def test_corrupt_config_is_backed_up_before_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "config"
            config_dir.mkdir()
            (config_dir / "config.json").write_text("{invalid", encoding="utf-8")

            Config(tmp)

            backups = list(config_dir.glob("config.json.bak-*"))
            self.assertEqual(len(backups), 1)
            payload = json.loads(
                (config_dir / "config.json").read_text(encoding="utf-8")
            )
            self.assertIn("profiles", payload)


if __name__ == "__main__":
    unittest.main()
