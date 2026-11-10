import tempfile
import unittest
from contextlib import contextmanager
from unittest import mock

from soaplun import apply as apply_module
from soaplun.apply import (
    _add_alias,
    _add_dl_member,
    _create_account,
    _create_cos,
    _create_dl,
    _modify_account,
    _modify_cos,
    _set_delegated_sender,
    _set_password,
)
from soaplun.config import Config


class FakeSession:
    def __init__(self):
        self.calls = []
        self.single = {}

    def request(self, name, content=None):
        self.calls.append((name, content or {}))
        return {}

    def request_single(self, name, content=None):
        self.calls.append((name, content or {}))
        return self.single.get(name, {})

    def call(self, name):
        for command, content in self.calls:
            if command == name:
                return content
        return None


class ApplyParallelTests(unittest.TestCase):
    def test_parallel_actions_report_all_results(self):
        config = mock.Mock()
        config.current_profile.return_value = "p"

        @contextmanager
        def context():
            yield "p"

        config.profile_context.side_effect = lambda profile: context()
        executed = []
        lock = __import__("threading").Lock()

        def handler(session, config, payload):
            with lock:
                executed.append(payload["name"])
            return "ok"

        actions = [
            {
                "id": "modify_account:u%d@example.com" % index,
                "kind": "modify_account",
                "target": "u%d@example.com" % index,
                "payload": {"name": "u%d@example.com" % index},
            }
            for index in range(4)
        ]
        with mock.patch.object(
            apply_module, "ZimbraSession", side_effect=lambda *a, **k: object()
        ), mock.patch.dict(
            apply_module.HANDLERS, {"modify_account": handler}
        ), mock.patch.object(
            apply_module, "_backup_object", return_value={}
        ), mock.patch.object(
            apply_module, "_backup", return_value=""
        ):
            items, counters, passwords = apply_module._apply_actions_parallel(
                config, actions, 4, None, None, "stamp", {}, 4
            )
        self.assertEqual(len(executed), 4)
        self.assertEqual(counters["ok"], 4)
        self.assertEqual(len(items), 4)
        self.assertEqual(passwords, {})


class ApplyHandlerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(self.tmp.name)
        self.config.sync()["fixed_password"] = "Passw0rd!"
        self.session = FakeSession()

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_account_includes_password_cos_and_attrs(self):
        _create_account(
            self.session,
            self.config,
            {
                "name": "alice@dst.fr",
                "password_source": "fixed",
                "cos_name": "staff",
                "attrs": {"zimbraMailQuota": "1000"},
            },
        )
        content = self.session.call("CreateAccount")
        self.assertEqual(content["name"], "alice@dst.fr")
        self.assertEqual(content["password"], "Passw0rd!")
        self.assertEqual(content["cos"], {"by": "name", "_content": "staff"})
        self.assertIn({"n": "zimbraMailQuota", "_content": "1000"}, content["a"])

    def test_create_account_requires_password(self):
        with self.assertRaises(Exception):
            _create_account(
                self.session,
                self.config,
                {"name": "bob@dst.fr", "password_source": "source_unknown", "attrs": {}},
            )

    def test_modify_account_uses_id_and_cos(self):
        _modify_account(
            self.session,
            self.config,
            {
                "name": "alice@dst.fr",
                "id": "id-1",
                "cos_name": "staff",
                "attrs": {"zimbraPrefDisplayName": "Alice"},
            },
        )
        content = self.session.call("ModifyAccount")
        self.assertEqual(content["id"], "id-1")
        self.assertEqual(content["cos"], {"by": "name", "_content": "staff"})

    def test_set_password_and_must_change(self):
        _set_password(
            self.session,
            self.config,
            {"name": "alice@dst.fr", "id": "id-1", "password_source": "fixed", "must_change": True},
        )
        password = self.session.call("SetPassword")
        self.assertEqual(password["newPassword"], "Passw0rd!")
        modify = self.session.call("ModifyAccount")
        self.assertIn(
            {"n": "zimbraPasswordMustChange", "_content": "TRUE"}, modify["a"]
        )

    def test_add_alias_resolves_account(self):
        self.session.single["GetAccount"] = {"account": {"id": "id-1"}}
        _add_alias(
            self.session, self.config, {"account": "alice@dst.fr", "alias": "a@dst.fr"}
        )
        content = self.session.call("AddAccountAlias")
        self.assertEqual(content["id"], "id-1")
        self.assertEqual(content["alias"], "a@dst.fr")

    def test_create_dl_members_are_dicts(self):
        _create_dl(
            self.session,
            self.config,
            {
                "name": "team@dst.fr",
                "attrs": {},
                "members": ["a@dst.fr", "b@dst.fr"],
                "dynamic": False,
            },
        )
        content = self.session.call("AddDistributionListMember")
        self.assertEqual(
            content["dlm"],
            [{"_content": "a@dst.fr"}, {"_content": "b@dst.fr"}],
        )

    def test_create_dl_chunks_large_member_lists(self):
        members = ["u%d@example.com" % index for index in range(250)]
        _create_dl(
            self.session,
            self.config,
            {"name": "all@example.com", "attrs": {}, "members": members},
        )
        additions = [
            content
            for command, content in self.session.calls
            if command == "AddDistributionListMember"
        ]
        self.assertEqual(len(additions), 3)
        self.assertEqual(sum(len(item["dlm"]) for item in additions), 250)
        self.assertTrue(all(len(item["dlm"]) <= 100 for item in additions))

    def test_add_dl_member_members_are_dicts(self):
        _add_dl_member(
            self.session,
            self.config,
            {"name": "team@dst.fr", "members": ["c@dst.fr"]},
        )
        content = self.session.call("AddDistributionListMember")
        self.assertEqual(content["dlm"], [{"_content": "c@dst.fr"}])

    def test_cos_uses_zimbra_document_names(self):
        _create_cos(
            self.session,
            self.config,
            {"name": "staff", "attrs": {"zimbraMailQuota": "100"}},
        )
        self.assertIsNotNone(self.session.call("CreateCos"))
        self.assertIsNone(self.session.call("CreateCOS"))

        _modify_cos(
            self.session,
            self.config,
            {"name": "staff", "id": "cos-1", "attrs": {"zimbraMailQuota": "200"}},
        )
        self.assertIsNotNone(self.session.call("ModifyCos"))
        self.assertIsNone(self.session.call("ModifyCOS"))

    def test_random_password_is_recorded_for_export(self):
        apply_module._RUN_OPTIONS.value = {}
        try:
            password = apply_module._password(self.config, "random")
        finally:
            apply_module._RUN_OPTIONS.value = None
        self.assertTrue(password)
        self.assertEqual(getattr(apply_module._RUN_OPTIONS, "last_random", ""), password)

    def test_password_prefers_plan_options(self):
        apply_module._RUN_OPTIONS.value = {"fixed_password": "FromPlan!"}
        try:
            _create_account(
                self.session,
                self.config,
                {"name": "bob@dst.fr", "password_source": "fixed", "attrs": {}},
            )
        finally:
            apply_module._RUN_OPTIONS.value = None
        self.assertEqual(self.session.call("CreateAccount")["password"], "FromPlan!")

    def test_check_safety_rejects_invalid_plan(self):
        self.config.connection("source")["store_host"] = "src-host"
        self.config.connection("destination")["store_host"] = "dst-host"
        with self.assertRaises(apply_module.SafetyError):
            apply_module.check_safety(
                self.config,
                {"validation": {"ok": False, "errors": ["plan obsolète"]}},
            )

    def test_plan_options_decrypt_fixed_password(self):
        encrypted = self.config.encrypt_secret("PlanSecret!")
        options = apply_module.plan_options_for_run(
            self.config,
            {
                "options": {
                    "fixed_password": True,
                    "fixed_password_encrypted": encrypted,
                }
            },
        )
        self.assertEqual(options["fixed_password"], "PlanSecret!")

    def test_plan_options_ignore_boolean_password(self):
        options = apply_module.plan_options_for_run(
            self.config, {"options": {"fixed_password": True}}
        )
        self.assertEqual(options["fixed_password"], "")

    def test_set_delegated_sender_updates_account(self):
        self.session.single["GetAccount"] = {"account": {"id": "id-1"}}
        _set_delegated_sender(
            self.session,
            self.config,
            {"name": "a@dst.fr", "values": ["a@dst.fr", "old@dst.fr"]},
        )
        content = self.session.call("ModifyAccount")
        self.assertEqual(content["id"], "id-1")
        self.assertEqual(
            content["a"],
            [
                {
                    "n": "zimbraPrefAllowAddressForDelegatedSender",
                    "_content": "a@dst.fr",
                },
                {
                    "n": "zimbraPrefAllowAddressForDelegatedSender",
                    "_content": "old@dst.fr",
                },
            ],
        )

    def test_apply_plan_maps_source_accounts_to_destination_actions(self):
        self.config.connection("source")["store_host"] = "src-host"
        self.config.connection("destination")["store_host"] = "dst-host"
        plan = {
            "source": {"host": "src-host"},
            "destination": {"host": "dst-host"},
            "validation": {"ok": True, "errors": []},
            "options": {"fixed_password": "Passw0rd!"},
            "accounts": [{"source": "a@src-host", "destination": "a@dst-host"}],
            "actions": [
                {
                    "id": "create_account:a@dst-host",
                    "kind": "create_account",
                    "target": "a@dst-host",
                    "payload": {
                        "name": "a@dst-host",
                        "password_source": "fixed",
                        "attrs": {},
                    },
                }
            ],
        }
        selected = apply_module.apply_plan(
            self.config,
            plan,
            dry_run=True,
            kinds={"create_account"},
            accounts=["a@src-host"],
        )
        self.assertEqual(selected["summary"]["planned"], 1)
        other = apply_module.apply_plan(
            self.config,
            plan,
            dry_run=True,
            kinds={"create_account"},
            accounts=["b@src-host"],
        )
        self.assertEqual(other["summary"]["planned"], 0)


if __name__ == "__main__":
    unittest.main()
