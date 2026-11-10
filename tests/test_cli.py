import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from soaplun import cli
from soaplun import inventory
from soaplun.commands import migrate as migrate_cmd
from soaplun.config import Config


class CliHelperTests(unittest.TestCase):
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

    def test_object_selector_all(self):
        self.assertIsNone(cli.read_object_selection("all"))
        self.assertEqual(cli.read_object_selection(""), [])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "names.csv"
            path.write_text("name\na.fr\n", encoding="utf-8")
            self.assertEqual(cli.read_object_selection(str(path)), ["a.fr"])

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

    def test_retry_users_filters_report_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(tmp)
            report = config.storage_path("report_path") / "apply-20260101.json"
            report.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "source": "a@example.com",
                                "status": "failed",
                                "type": "account",
                            },
                            {
                                "source": "resource1@example.com",
                                "status": "failed",
                                "type": "resource",
                            },
                            {
                                "source": "list@example.com",
                                "status": "failed",
                                "type": "dl",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    cli._retry_users(config, migrate_type="resource"),
                    ["resource1@example.com"],
                )
                self.assertEqual(
                    cli._retry_users(config, migrate_type="dl"),
                    ["list@example.com"],
                )

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


class NewCliParserTests(unittest.TestCase):
    def parser(self):
        return cli.build_parser()

    def parsed(self, argv):
        parser = self.parser()
        args = parser.parse_args(argv)
        cli.validate(args, parser)
        return args

    def test_help_lists_command_examples(self):
        help_text = self.parser().format_help()
        for value in (
            "server add src1 --role source",
            "provision domain domains.csv",
            "migrate all --type all",
            "migrate users.csv mail --type account --differential",
            "job stop all",
        ):
            self.assertIn(value, help_text)

    def test_server_inventory_no_stats(self):
        args = self.parsed(["server", "inventory", "src1", "--no-stats"])
        self.assertEqual(args.server_command, "inventory")
        self.assertTrue(args.no_stats)

    def test_server_add(self):
        args = self.parsed(["server", "add", "src1", "--role", "source"])
        self.assertEqual(args.command, "server")
        self.assertEqual(args.server_command, "add")
        self.assertEqual(args.role, "source")

    def test_provision_account(self):
        args = self.parsed(
            [
                "provision",
                "account",
                "users.csv",
                "--from",
                "src1",
                "--to",
                "dst1",
                "--password",
                "Secret!",
            ]
        )
        self.assertEqual(args.password, "Secret!")
        self.assertFalse(args.random_password)

    def test_parallelism_defaults_to_one(self):
        migrate = self.parsed(
            [
                "migrate",
                "users.csv",
                "mail",
                "--type",
                "account",
                "--from",
                "src1",
                "--to",
                "dst1",
            ]
        )
        self.assertEqual(migrate.parallelism, 1)
        provision = self.parsed(
            [
                "provision",
                "cos",
                "all",
                "--from",
                "src1",
                "--to",
                "dst1",
            ]
        )
        self.assertEqual(provision.parallelism, 1)
        account = self.parsed(
            [
                "provision",
                "account",
                "users.csv",
                "--from",
                "src1",
                "--to",
                "dst1",
                "--password",
                "Secret!",
            ]
        )
        self.assertEqual(account.parallelism, 1)

    def test_parallelism_can_be_overridden(self):
        args = self.parsed(
            [
                "provision",
                "domain",
                "domains.csv",
                "--from",
                "src1",
                "--to",
                "dst1",
                "--parallelism",
                "6",
            ]
        )
        self.assertEqual(args.parallelism, 6)

    def test_valid_migrate_account(self):
        args = self.parsed(
            [
                "migrate",
                "users.csv",
                "mail",
                "calendar",
                "settings",
                "--type",
                "account",
                "--from",
                "src1",
                "--to",
                "dst1",
            ]
        )
        self.assertEqual(args.type, "account")
        self.assertEqual(args.elements, ["mail", "calendar", "settings"])

    def test_briefcase_account_element_is_supported(self):
        args = self.parsed(
            [
                "migrate",
                "users.csv",
                "briefcase",
                "--type",
                "account",
                "--from",
                "src1",
                "--to",
                "dst1",
            ]
        )
        self.assertEqual(args.elements, ["briefcase"])

    def test_valid_migrate_cos(self):
        args = self.parsed(
            [
                "migrate",
                "users.csv",
                "cos",
                "--type",
                "account",
                "--from",
                "src1",
                "--to",
                "dst1",
            ]
        )
        self.assertEqual(args.elements, ["cos"])

    def test_cos_plan_only_modifies_cos(self):
        args = SimpleNamespace(
            from_label="src1",
            to_label="dst1",
            dry_run=True,
            keep_tgz=False,
            keep_tgz_days=0,
            parallelism=1,
            mapping_domain="",
            mapping_host="",
            differential=None,
        )
        plan = {
            "actions": [
                {
                    "id": "modify_account:u@example.com",
                    "kind": "modify_account",
                    "target": "u@example.com",
                    "payload": {
                        "name": "u@example.com",
                        "cos_name": "COS1",
                        "attrs": {"zimbraPrefLocale": "fr"},
                    },
                },
                {
                    "id": "create_account:v@example.com",
                    "kind": "create_account",
                    "target": "v@example.com",
                    "payload": {"name": "v@example.com"},
                },
            ],
            "options": {},
        }
        with mock.patch.object(
            cli,
            "_load_snapshots",
            return_value=({}, {"cos": [{"name": "COS1"}]}),
        ), mock.patch.object(
            cli, "_base_options", return_value={}
        ), mock.patch.object(
            cli, "_build_plan", return_value=plan
        ), mock.patch(
            "soaplun.apply.apply_plan", return_value={"summary": {}}
        ) as apply, mock.patch.object(
            cli, "_print_summary", return_value=0
        ):
            self.assertEqual(
                migrate_cmd._run_cos(mock.Mock(), args, ["u@example.com"]),
                0,
            )
        passed = apply.call_args.args[1]
        self.assertEqual(len(passed["actions"]), 1)
        self.assertEqual(passed["actions"][0]["payload"]["attrs"], {})
        self.assertEqual(passed["actions"][0]["payload"]["cos_name"], "COS1")

    def test_valid_migrate_resource(self):
        args = self.parsed(
            [
                "migrate",
                "resources.csv",
                "mail",
                "calendar",
                "--type",
                "resource",
                "--from",
                "src1",
                "--to",
                "dst1",
            ]
        )
        self.assertEqual(args.elements, ["mail", "calendar"])

    def test_valid_migrate_dl(self):
        args = self.parsed(
            [
                "migrate",
                "lists.csv",
                "members",
                "--type",
                "dl",
                "--from",
                "src1",
                "--to",
                "dst1",
            ]
        )
        self.assertEqual(args.elements, ["members"])

    def test_type_all_requires_csv_all(self):
        with self.assertRaises(SystemExit) as context:
            self.parsed(
                [
                    "migrate",
                    "users.csv",
                    "--type",
                    "all",
                    "--from",
                    "src1",
                    "--to",
                    "dst1",
                ]
            )
        self.assertEqual(context.exception.code, 2)

    def test_json_requires_dry_run(self):
        with self.assertRaises(SystemExit) as context:
            self.parsed(
                [
                    "migrate",
                    "users.csv",
                    "--type",
                    "account",
                    "--from",
                    "src1",
                    "--to",
                    "dst1",
                    "--json",
                ]
            )
        self.assertEqual(context.exception.code, 2)

    def test_unknown_element(self):
        with self.assertRaises(SystemExit) as context:
            self.parsed(
                [
                    "migrate",
                    "users.csv",
                    "unknown",
                    "--type",
                    "account",
                    "--from",
                    "src1",
                    "--to",
                    "dst1",
                ]
            )
        self.assertEqual(context.exception.code, 2)

    def test_inapplicable_element_is_ignored(self):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            args = self.parsed(
                [
                    "migrate",
                    "users.csv",
                    "mail",
                    "settings",
                    "--type",
                    "resource",
                    "--from",
                    "src1",
                    "--to",
                    "dst1",
                ]
            )
        self.assertEqual(args.elements, ["mail"])
        self.assertIn("settings inapplicable", errors.getvalue())

    def test_all_elements_inapplicable_is_error(self):
        with self.assertRaises(SystemExit) as context:
            self.parsed(
                [
                    "migrate",
                    "users.csv",
                    "settings",
                    "--type",
                    "resource",
                    "--from",
                    "src1",
                    "--to",
                    "dst1",
                ]
            )
        self.assertEqual(context.exception.code, 2)

    def test_invalid_date(self):
        with self.assertRaises(SystemExit) as context:
            self.parsed(
                [
                    "migrate",
                    "users.csv",
                    "--type",
                    "account",
                    "--from",
                    "src1",
                    "--to",
                    "dst1",
                    "--differential",
                    "2026-13-01",
                ]
            )
        self.assertEqual(context.exception.code, 2)

    def test_password_options_are_exclusive(self):
        with self.assertRaises(SystemExit) as context:
            self.parsed(
                [
                    "provision",
                    "account",
                    "users.csv",
                    "--from",
                    "src1",
                    "--to",
                    "dst1",
                    "--password",
                    "x",
                    "--random-password",
                ]
            )
        self.assertEqual(context.exception.code, 2)

    def test_missing_from_or_to(self):
        with self.assertRaises(SystemExit) as context:
            self.parsed(
                [
                    "migrate",
                    "users.csv",
                    "--type",
                    "account",
                    "--to",
                    "dst1",
                ]
            )
        self.assertEqual(context.exception.code, 2)

    def test_missing_type(self):
        with self.assertRaises(SystemExit) as context:
            self.parsed(
                [
                    "migrate",
                    "users.csv",
                    "--from",
                    "src1",
                    "--to",
                    "dst1",
                ]
            )
        self.assertEqual(context.exception.code, 2)

    def test_differential_values(self):
        base = [
            "migrate",
            "users.csv",
            "--type",
            "account",
            "--from",
            "src1",
            "--to",
            "dst1",
        ]
        self.assertIsNone(self.parsed(base).differential)
        self.assertIs(self.parsed(base + ["--differential"]).differential, True)
        value = self.parsed(base + ["--differential", "2026-10-01"]).differential
        self.assertEqual(value.isoformat(), "2026-10-01")

    def test_job_commands(self):
        args = self.parsed(["job", "status", "abc123"])
        self.assertEqual(args.command, "job")
        self.assertEqual(args.job_command, "status")
        self.assertEqual(args.job_id, "abc123")

    def test_type_all_handler_order(self):
        args = self.parsed(
            [
                "migrate",
                "all",
                "--type",
                "all",
                "--from",
                "src1",
                "--to",
                "dst1",
            ]
        )
        calls = []
        config = mock.Mock()
        with mock.patch.object(
            cli.config_module, "Config", return_value=config
        ), mock.patch.object(
            migrate_cmd,
            "_run_account",
            side_effect=lambda *a: calls.append("account") or 0,
        ), mock.patch.object(
            migrate_cmd,
            "_run_resource",
            side_effect=lambda *a: calls.append("resource") or 0,
        ), mock.patch.object(
            migrate_cmd,
            "_run_dl",
            side_effect=lambda *a: calls.append("dl") or 0,
        ):
            self.assertEqual(migrate_cmd.run(args), 0)
        self.assertEqual(calls, ["account", "resource", "dl"])


class CliServerCommandTests(unittest.TestCase):
    def test_server_inventory_passes_no_stats(self):
        config = mock.Mock()
        config.server.return_value = {"type": "source"}
        output = io.StringIO()
        with mock.patch.object(
            cli.inventory_module,
            "collect_snapshot",
            return_value={"counts": {"accounts": 1, "domains": 1}, "path": "x"},
        ) as collect:
            with contextlib.redirect_stdout(output):
                result = cli._run_server_inventory(
                    config, "src1", with_stats=False
                )
        self.assertEqual(result, 0)
        self.assertFalse(collect.call_args.kwargs["with_stats"])

    def test_server_add_rename_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(tmp)
            with mock.patch.object(cli.config_module, "Config", lambda: config):
                self.assertEqual(
                    cli.main(["server", "add", "src1", "--role", "source"]),
                    0,
                )
                self.assertEqual(
                    cli.main(["server", "rename", "src1", "src2"]),
                    0,
                )
            self.assertIn("src2", config.servers())
            with mock.patch.object(cli.config_module, "Config", lambda: config):
                self.assertEqual(cli.main(["server", "remove", "src2"]), 0)
            self.assertNotIn("src2", config.servers())

    def test_quiet_suppresses_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(tmp)
            output = io.StringIO()
            with mock.patch.object(cli.config_module, "Config", lambda: config):
                with contextlib.redirect_stdout(output):
                    self.assertEqual(cli.main(["-q", "server", "list"]), 0)
            self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
