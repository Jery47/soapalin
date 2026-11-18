import contextlib
import datetime
import io
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from soaplun import cleanup, storage
from soaplun.commands import clean as clean_cmd
from soaplun.config import Config
from soaplun.jobs import JobStore


def _set_age(path: Path, days: int) -> None:
    stamp = datetime.datetime.now().timestamp() - days * 86400
    os.utime(path, (stamp, stamp))


class CleanupPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_dry_run_does_not_delete(self):
        root = self.config.storage_path("mailbox_path")
        old = root / "run-old"
        old.mkdir()
        (old / "a.tgz").write_bytes(b"x")
        _set_age(old, 10)

        plan = cleanup.plan_cleanup(self.config, ["mailboxes"], older_than_days=1)
        self.assertEqual(len(plan.items), 1)
        report = cleanup.execute_cleanup(plan, dry_run=True)
        self.assertTrue(old.exists())
        self.assertEqual(report["removed"], [str(old)])
        self.assertGreater(report["freed_bytes"], 0)

        cleanup.execute_cleanup(plan, dry_run=False)
        self.assertFalse(old.exists())

    def test_recent_mailbox_run_is_kept(self):
        root = self.config.storage_path("mailbox_path")
        recent = root / "run-recent"
        recent.mkdir()
        plan = cleanup.plan_cleanup(self.config, ["mailboxes"], older_than_days=1)
        self.assertEqual(plan.items, [])

    def test_active_job_run_is_protected(self):
        store = JobStore(self.config)
        spec = store.create(["migrate", "users.csv", "mail"])
        store.update(spec["id"], status="running")
        root = self.config.storage_path("mailbox_path")
        run = root / "run-active"
        run.mkdir()
        _set_age(run, 10)

        plan = cleanup.plan_cleanup(self.config, ["mailboxes"], older_than_days=1)
        self.assertEqual(plan.items, [])
        self.assertTrue(any("run actif" in item for item in plan.skipped))

    def test_jobs_cleanup_keeps_active(self):
        store = JobStore(self.config)
        done = store.create(["migrate", "a.csv"])
        running = store.create(["migrate", "b.csv"])
        store.update(done["id"], status="succeeded")
        store.update(running["id"], status="running")
        old_stamp = (
            datetime.datetime.now() - datetime.timedelta(days=10)
        ).isoformat(timespec="seconds")
        store.update(done["id"], created_at=old_stamp)
        _set_age(store.directory / done["id"], 10)
        _set_age(store.directory / running["id"], 10)

        plan = cleanup.plan_cleanup(self.config, ["jobs"], older_than_days=1)
        paths = {item.path for item in plan.items}
        self.assertIn(store.directory / done["id"], paths)
        self.assertNotIn(store.directory / running["id"], paths)

    def test_reports_keep_last(self):
        root = self.config.storage_path("report_path")
        paths = []
        for index in range(3):
            path = root / ("apply-2026010%d.json" % index)
            path.write_text("{}", encoding="utf-8")
            _set_age(path, 10 + index)
            paths.append(path)

        plan = cleanup.plan_cleanup(
            self.config, ["reports"], older_than_days=1, keep_last=1
        )
        removed = {item.path for item in plan.items}
        self.assertEqual(len(removed), 2)
        self.assertIn(paths[1], removed)
        self.assertIn(paths[2], removed)
        self.assertNotIn(paths[0], removed)

    def test_snapshots_keep_last_per_side_and_profile(self):
        root = self.config.storage_path("snapshot_path")
        created = []
        for index in range(2):
            base = root / ("source-default-2026010%d-000000.json" % index)
            storage.write_json_atomic(
                base, {"profile": "default", "side": "source"}, indent=1
            )
            index_path = base.with_name(base.name[:-5] + ".index.json")
            storage.write_json_atomic(index_path, {"profile": "default"}, indent=1)
            _set_age(base, 10 + index)
            _set_age(index_path, 10 + index)
            created.append((base, index_path))

        plan = cleanup.plan_cleanup(
            self.config, ["snapshots"], older_than_days=1, keep_last=1
        )
        removed = {item.path for item in plan.items}
        self.assertIn(created[0][0], removed)
        self.assertIn(created[0][1], removed)
        self.assertNotIn(created[1][0], removed)

    def test_move_account_archives(self):
        run_dir = self.config.storage_path("mailbox_path") / "run-test"
        run_dir.mkdir()
        account = "a@example.com"
        first = run_dir / ("%s.mail.tgz" % cleanup.safe_filename(account))
        second = run_dir / ("%s.calendar.tgz" % cleanup.safe_filename(account))
        other = run_dir / "b@example.com.mail.tgz"
        for path in (first, second, other):
            path.write_bytes(b"x")

        moved = cleanup.move_account_archives(self.config, run_dir, account)
        self.assertEqual(set(moved), {str(first), str(second)})
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        self.assertTrue(other.exists())
        for target in moved.values():
            self.assertTrue(Path(target).exists())

    def test_unknown_target_is_rejected(self):
        with self.assertRaises(ValueError):
            cleanup.plan_cleanup(self.config, ["nope"])

    def test_all_targets_combine_sections(self):
        mailbox_root = self.config.storage_path("mailbox_path")
        run = mailbox_root / "run-old"
        run.mkdir()
        _set_age(run, 10)
        report = self.config.storage_path("report_path") / "apply-20260101.json"
        report.write_text("{}", encoding="utf-8")
        _set_age(report, 10)
        snapshot = self.config.storage_path("snapshot_path") / (
            "source-default-20260101-000000.json"
        )
        storage.write_json_atomic(snapshot, {"profile": "default"}, indent=1)
        _set_age(snapshot, 10)

        plan = cleanup.plan_cleanup(self.config, ["all"], older_than_days=1)
        kinds = {item.kind for item in plan.items}
        self.assertEqual(kinds, {"mailboxes", "reports", "snapshots"})

    def test_clean_share_dry_run(self):
        class Mail:
            def delete_mountpoints(self, folder_ids=None):
                raise AssertionError("must not delete in dry-run")

        class Session:
            def mail_client(self, email):
                return Mail()

        args = SimpleNamespace(
            targets=[],
            share="u@example.com",
            type="account",
            to_label="dst1",
            from_label="",
            dry_run=True,
            parallelism=2,
            older_than=0,
            keep_last=0,
        )

        @contextlib.contextmanager
        def context():
            yield "dst1"

        config = mock.Mock()
        config.use_server.return_value = "dst1"
        config.current_profile.return_value = "dst1"
        config.profile_context.side_effect = lambda profile: context()
        config.map_email.side_effect = lambda value: value
        links = [
            {"id": "10", "rid": "1", "path": "/old"},
            {"id": "20", "rid": "", "path": "/Files shared with me"},
        ]
        with mock.patch("soaplun.config.Config", return_value=config), mock.patch(
            "soaplun.cli.read_users", return_value=["u@example.com"]
        ), mock.patch(
            "soaplun.connections.ZimbraSession", return_value=Session()
        ), mock.patch(
            "soaplun.inventory.collect_mountpoints", return_value=links
        ), contextlib.redirect_stdout(io.StringIO()):
            result = clean_cmd.run(args)
        self.assertEqual(result, 0)

    def test_clean_share_deletes_mountpoints(self):
        deleted = []

        class Mail:
            def delete_mountpoints(self, folder_ids=None):
                deleted.append(folder_ids)

        class Session:
            def mail_client(self, email):
                return Mail()

        args = SimpleNamespace(
            targets=[],
            share="u@example.com",
            type="account",
            to_label="dst1",
            from_label="",
            dry_run=False,
            parallelism=2,
            older_than=0,
            keep_last=0,
        )

        @contextlib.contextmanager
        def context():
            yield "dst1"

        config = mock.Mock()
        config.use_server.return_value = "dst1"
        config.current_profile.return_value = "dst1"
        config.profile_context.side_effect = lambda profile: context()
        config.map_email.side_effect = lambda value: value
        links = [
            {"id": "10", "rid": "1", "path": "/old"},
            {"id": "20", "rid": "", "path": "/Files shared with me"},
        ]
        with mock.patch("soaplun.config.Config", return_value=config), mock.patch(
            "soaplun.cli.read_users", return_value=["u@example.com"]
        ), mock.patch(
            "soaplun.connections.ZimbraSession", return_value=Session()
        ), mock.patch(
            "soaplun.inventory.collect_mountpoints", return_value=links
        ), contextlib.redirect_stdout(io.StringIO()):
            result = clean_cmd.run(args)
        self.assertEqual(result, 0)
        self.assertEqual(deleted, [["10"]])

    def test_clean_command_dry_run(self):
        root = self.config.storage_path("mailbox_path")
        old = root / "run-old"
        old.mkdir()
        _set_age(old, 10)
        args = SimpleNamespace(
            targets=["mailboxes"],
            older_than=1,
            keep_last=0,
            dry_run=True,
        )
        with mock.patch("soaplun.config.Config", return_value=self.config):
            result = clean_cmd.run(args)
        self.assertEqual(result, 0)
        self.assertTrue(old.exists())


if __name__ == "__main__":
    unittest.main()
