import tempfile
import unittest
from pathlib import Path

from soapalin import cleanup, mailbox


class CheckpointTests(unittest.TestCase):
    def test_checkpoint_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run-test"
            run_dir.mkdir()
            mailbox._write_checkpoint(run_dir, {"a@x"}, {"b@x"}, 2)
            payload = mailbox._load_checkpoint(run_dir)
            self.assertEqual(payload["completed"], ["a@x"])
            self.assertEqual(payload["failed"], ["b@x"])
            self.assertEqual(payload["total"], 2)

            mailbox._clear_checkpoint(run_dir)
            self.assertFalse((run_dir / mailbox.CHECKPOINT_NAME).exists())

    def test_latest_run_dir_prefers_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = root / "run-20260101-000000"
            new = root / "run-20260102-000000"
            old.mkdir()
            new.mkdir()
            mailbox._write_checkpoint(old, set(), set(), 1, profile="site-a")
            mailbox._write_checkpoint(new, set(), set(), 1, profile="site-b")
            self.assertEqual(
                mailbox._latest_run_dir(root, profile="site-a"), old
            )
            self.assertEqual(
                mailbox._latest_run_dir(root, profile="site-b"), new
            )

    def test_safe_filename_matches_cleanup(self):
        self.assertEqual(
            mailbox._safe_filename("a/b@example.com"),
            cleanup.safe_filename("a/b@example.com"),
        )


if __name__ == "__main__":
    unittest.main()
