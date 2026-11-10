import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from soaplun import cli
from soaplun import inventory
from types import SimpleNamespace
from soaplun.config import Config


class CliParsingTests(unittest.TestCase):
    def test_read_users_skips_header_and_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "users.csv"
            path.write_text(
                "email\nu1@example.com\nu2@example.com\nu1@example.com\n",
                encoding="utf-8",
            )
            self.assertEqual(
                cli.read_users(str(path)),
                ["u1@example.com", "u2@example.com"],
            )

    def test_read_users_warns_for_invalid_and_duplicate_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "users.csv"
            path.write_text(
                "email\nu1@example.com\nnot-an-email\n u1@example.com \n",
                encoding="utf-8",
            )
            errors = io.StringIO()
            with contextlib.redirect_stderr(errors):
                users = cli.read_users(str(path))
            self.assertEqual(users, ["u1@example.com"])
            message = errors.getvalue()
            self.assertIn("ligne 3 ignoree", message)
            self.assertIn("ligne 4 : doublon", message)
            self.assertIn("espaces ignores", message)

    def test_target_domain_validation_blocks_missing_domains(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(tmp)
            inventory.write_snapshot(
                config,
                "destination",
                {
                    "version": 1,
                    "side": "destination",
                    "host": "dst",
                    "created_at": "2026-01-01T10:00:00",
                    "counts": {},
                    "domains": [{"name": "dst.fr", "attrs": {}}],
                    "accounts": [],
                },
                profile="dst1",
            )
            with self.assertRaises(RuntimeError):
                cli._validate_target_domains(
                    config, ["u@missing.fr"], {}, "dst1"
                )
            cli._validate_target_domains(
                config,
                ["u@missing.fr"],
                {"domain_map": {"missing.fr": "dst.fr"}},
                "dst1",
            )

    def test_interrupt_report_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(tmp)
            path = cli._write_interrupt_report(config, ["a.part"])
            self.assertTrue(path)
            report = json.loads(Path(path).read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "interrupted")
            self.assertEqual(report["cleaned_partials"], 1)

    def test_generated_passwords_are_exported(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(tmp)
            report = {"_generated_passwords": {"alice@example.com": "Secret!"}}
            with contextlib.redirect_stdout(io.StringIO()):
                cli._write_passwords(config, report)
            self.assertNotIn("_generated_passwords", report)
            files = list(config.storage_path("report_path").glob("passwords-*.csv"))
            self.assertEqual(len(files), 1)
            content = files[0].read_text(encoding="utf-8")
            self.assertIn("alice@example.com", content)
            self.assertIn("Secret!", content)

    def test_retry_users_reads_latest_failed_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(tmp)
            report = config.storage_path("report_path") / "mailboxes-20260101.json"
            report.write_text(
                json.dumps(
                    {
                        "items": [
                            {"source": "ok@example.com", "status": "ok"},
                            {"source": "failed@example.com", "status": "failed"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(cli._retry_users(config), ["failed@example.com"])
            self.assertEqual(
                cli._retry_users(config, str(report)), ["failed@example.com"]
            )

    def test_create_objects_dry_run_builds_category_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(tmp)
            config.create_server("src1", "source")
            config.create_server("dst1", "destination")
            for label, host in (("src1", "src"), ("dst1", "dst")):
                connection = config.server(label)["connection"]
                connection["store_host"] = host
                connection["proxy_host"] = host
                config.save_server(label, connection)
            source = {
                "version": 1,
                "side": "source",
                "host": "src",
                "created_at": "2026-01-01T10:00:00",
                "counts": {},
                "domains": [{"name": "a.fr", "attrs": {}}],
                "cos": [],
                "servers": [],
                "accounts": [],
                "distribution_lists": [],
                "resources": [],
                "warnings": [],
            }
            destination = dict(source)
            destination.update(
                {
                    "side": "destination",
                    "host": "dst",
                    "created_at": "2026-01-01T10:05:00",
                    "domains": [],
                }
            )
            inventory.write_snapshot(config, "source", source, profile="src1")
            inventory.write_snapshot(config, "destination", destination, profile="dst1")
            config.use_servers("src1", "dst1")
            domains = Path(tmp) / "domains.csv"
            domains.write_text("name\na.fr\n", encoding="utf-8")
            args = SimpleNamespace(
                server_source="src1",
                server_destination="dst1",
                create_domain=str(domains),
                create_cos="",
                create_dl="",
                create_resources="",
                dry_run=True,
            )
            with mock.patch.object(cli, "_print_summary", return_value=0) as summary:
                failures = cli._run_objects(config, args, {})
            self.assertEqual(failures, 0)
            self.assertEqual(summary.call_args[0][0], "configuration")

    def test_quiet_suppresses_stdout_but_not_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(tmp)
            output = io.StringIO()
            with mock.patch.object(cli.config_module, "Config", lambda: config):
                with contextlib.redirect_stdout(output):
                    self.assertEqual(
                        cli.main(["--server-action", "list", "--quiet"]), 0
                    )
            self.assertEqual(output.getvalue(), "")

    def test_read_mapping_supports_csv_and_equals(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "map.csv"
            path.write_text(
                "source,destination\na.fr,b.fr\nc.fr = d.fr\n",
                encoding="utf-8",
            )
            self.assertEqual(
                cli.read_mapping(str(path)),
                {"a.fr": "b.fr", "c.fr": "d.fr"},
            )

    def test_normalize_imports_aliases_and_order(self):
        contents, steps = cli.normalize_imports(
            [["mail", "task", "calendar", "whitelsit", "sendas", "settings"]]
        )
        self.assertEqual(contents, ["mail", "tasks", "calendar"])
        self.assertEqual(steps, ["settings", "lists", "permissions"])

    def test_object_selector_all(self):
        self.assertIsNone(cli.read_object_selection("all"))
        self.assertEqual(cli.read_object_selection(""), [])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "names.csv"
            path.write_text("name\na.fr\n", encoding="utf-8")
            self.assertEqual(cli.read_object_selection(str(path)), ["a.fr"])

    def test_force_sync_tags_option(self):
        parser = cli.build_parser()
        args = parser.parse_args(["--diff-user", "--force-sync-tags"])
        self.assertTrue(args.force_sync_tags)

    def test_import_cos_alias(self):
        parser = cli.build_parser()
        self.assertEqual(
            parser.parse_args(["--import-cos", "all"]).create_cos, "all"
        )

    def test_import_aliases_for_resources_and_dl(self):
        parser = cli.build_parser()
        self.assertEqual(
            parser.parse_args(["--import-resources", "resources.csv"]).import_resources,
            "resources.csv",
        )
        self.assertEqual(
            parser.parse_args(["--import-dl", "lists.csv"]).create_dl,
            "lists.csv",
        )

    def test_all_alias_expands_contents_and_steps(self):
        contents, steps = cli.normalize_imports([["all"]])
        self.assertEqual(
            contents,
            ["mail", "calendar", "tasks", "contacts", "briefcase"],
        )
        self.assertEqual(
            steps,
            ["settings", "tags", "cos", "filters", "lists", "shares", "permissions"],
        )

    def test_briefcase_is_an_import_content(self):
        contents, steps = cli.normalize_imports([["briefcase"]])
        self.assertEqual(contents, ["briefcase"])
        self.assertEqual(steps, [])

    def test_tags_are_an_import_step(self):
        contents, steps = cli.normalize_imports([["tags"]])
        self.assertEqual(contents, [])
        self.assertEqual(steps, ["tags"])

    def test_adminrights_is_not_an_import(self):
        with self.assertRaises(ValueError):
            cli.normalize_imports([["adminrights"]])

    def test_import_requires_list_user(self):
        with self.assertRaises(SystemExit) as context:
            cli.main([
                "--import-user", "mail",
                "--server-source", "src",
                "--server-destination", "dst",
            ])
        self.assertEqual(context.exception.code, 2)

    def test_create_requires_list_user(self):
        with self.assertRaises(SystemExit) as context:
            cli.main([
                "--create-user",
                "--server-source", "src",
                "--server-destination", "dst",
            ])
        self.assertEqual(context.exception.code, 2)

    def test_diff_and_import_are_exclusive(self):
        with self.assertRaises(SystemExit) as context:
            cli.main([
                "--diff-user", "--import-user", "mail",
                "--server-source", "src", "--server-destination", "dst",
                "--list-user", "users.csv",
            ])
        self.assertEqual(context.exception.code, 2)

    def test_migration_requires_server_labels(self):
        with self.assertRaises(SystemExit) as context:
            cli.main(["--create-user", "--list-user", "users.csv"])
        self.assertEqual(context.exception.code, 2)

    def test_server_actions_create_rename_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(tmp)
            with mock.patch.object(cli.config_module, "Config", lambda: config):
                self.assertEqual(
                    cli.main([
                        "--server-action", "create",
                        "--server-type", "source",
                        "--server-label", "src1",
                    ]),
                    0,
                )
                self.assertEqual(
                    cli.main([
                        "--server-action", "rename",
                        "--server-label", "src1",
                        "--new-label", "src2",
                    ]),
                    0,
                )
            self.assertIn("src2", config.servers())
            self.assertNotIn("src1", config.servers())
            with mock.patch.object(cli.config_module, "Config", lambda: config):
                self.assertEqual(
                    cli.main([
                        "--server-action", "remove",
                        "--server-label", "src2",
                    ]),
                    0,
                )
            self.assertNotIn("src2", config.servers())

    def test_progress_accepts_detail_argument(self):
        with contextlib.redirect_stdout(io.StringIO()):
            cli._progress(1, 2, "test", {"account": "u@example.com"})

    def test_bool_parser(self):
        self.assertTrue(cli._parse_bool("TRUE"))
        self.assertFalse(cli._parse_bool("false"))
        with self.assertRaises(Exception):
            cli._parse_bool("peut-etre")


if __name__ == "__main__":
    unittest.main()
