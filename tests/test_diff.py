import tempfile
import unittest

from soaplun.config import Config
from soaplun.diff import RESOURCE_HANDLED, dl_attrs, generate_plan, changed_attrs, filtered_attrs, resource_attrs


def source_snapshot():
    return {
        "host": "src-zimbra",
        "created_at": "2026-01-01T10:00:00",
        "counts": {"accounts": 2, "domains": 1},
        "domains": [{"name": "src.fr", "id": "d-src", "attrs": {"zimbraDomainType": "local"}}],
        "cos": [
            {"name": "default", "id": "c-default", "attrs": {}},
            {
                "name": "staff",
                "id": "c-staff",
                "attrs": {"zimbraMailQuota": "2147483648", "zimbraFeatureMailEnabled": "TRUE"},
            },
        ],
        "servers": [{"name": "src-zimbra", "id": "s-src", "attrs": {}}],
        "accounts": [
            {
                "name": "alice@src.fr",
                "id": "a-alice",
                "domain": "src.fr",
                "attrs": {
                    "zimbraMailQuota": "1073741824",
                    "zimbraMailAlias": ["alice.old@src.fr"],
                    "zimbraPrefDisplayName": "Alice",
                    "zimbraId": "a-alice",
                },
                "aliases": ["alice.old@src.fr"],
                "cos_name": "staff",
                "mail_host": "src-zimbra",
                "is_admin": False,
                "is_system": False,
                "mailbox": {"size": "1024", "messages": "10"},
            },
            {
                "name": "bob@src.fr",
                "id": "a-bob",
                "domain": "src.fr",
                "attrs": {"zimbraMailQuota": "2000", "zimbraPrefDisplayName": "Bob"},
                "aliases": [],
                "cos_name": "default",
                "mail_host": "src-zimbra",
                "is_admin": False,
                "is_system": False,
                "mailbox": {"size": "4096", "messages": "40"},
            },
        ],
        "distribution_lists": [
            {
                "name": "team@src.fr",
                "id": "dl-team",
                "attrs": {"zimbraDLType": "static"},
                "members": ["alice@src.fr", "external@other.com"],
                "is_dynamic": False,
            }
        ],
        "resources": [
            {
                "name": "room@src.fr",
                "id": "r-room",
                "attrs": {"displayName": "Salle", "zimbraCalResType": "Location"},
            }
        ],
    }


def destination_snapshot():
    return {
        "host": "dst-zimbra",
        "created_at": "2026-01-01T11:00:00",
        "counts": {"accounts": 2, "domains": 1},
        "domains": [{"name": "dst.fr", "id": "d-dst", "attrs": {"zimbraDomainType": "local"}}],
        "cos": [{"name": "default", "id": "c-default-dst", "attrs": {}}],
        "servers": [{"name": "dst-zimbra", "id": "s-dst", "attrs": {}}],
        "accounts": [
            {
                "name": "bob@dst.fr",
                "id": "a-bob-dst",
                "domain": "dst.fr",
                "attrs": {"zimbraMailQuota": "1000", "zimbraPrefDisplayName": "Bob"},
                "aliases": [],
                "cos_name": "default",
                "mail_host": "dst-zimbra",
                "is_admin": False,
                "is_system": False,
            },
            {
                "name": "extra@dst.fr",
                "id": "a-extra",
                "domain": "dst.fr",
                "attrs": {},
                "aliases": [],
                "cos_name": "default",
                "mail_host": "dst-zimbra",
                "is_admin": False,
                "is_system": False,
            },
        ],
        "distribution_lists": [],
        "resources": [],
    }


class DiffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(self.tmp.name)
        self.config.sync()["domain_map"] = {"src.fr": "dst.fr"}
        self.config.sync()["mail_host_map"] = {"src-zimbra": "dst-zimbra"}
        self.config.sync()["fixed_password"] = "Passw0rd!"
        self.plan = generate_plan(
            self.config, source_snapshot(), destination_snapshot()
        )

    def test_dl_immutable_attributes_are_not_copied(self):
        attrs = dl_attrs(
            self.config,
            {
                "mail": "list@example.com",
                "uid": "list",
                "objectClass": ["zimbraGroup"],
                "displayName": "Liste",
                "zimbraMailStatus": "enabled",
                "zimbraDistributionListSubscriptionPolicy": "APPROVAL",
            },
        )
        self.assertEqual(
            attrs,
            {
                "displayName": "Liste",
                "zimbraMailStatus": "enabled",
                "zimbraDistributionListSubscriptionPolicy": "APPROVAL",
            },
        )

    def test_resource_uid_is_not_copied(self):
        attrs = filtered_attrs(
            self.config,
            {"uid": "immutable", "displayName": "Salle"},
            kind="resource",
            handled=RESOURCE_HANDLED,
        )
        self.assertEqual(attrs, {"displayName": "Salle"})
        self.assertEqual(
            resource_attrs(
                self.config,
                {
                    "uid": "immutable",
                    "zimbraMailDeliveryAddress": "room@example.com",
                    "description": "Salle",
                    "zimbraCalResType": "Location",
                    "zimbraPrefLocale": "fr",
                },
            ),
            {"description": "Salle", "zimbraCalResType": "Location"},
        )

    def test_random_password_marks_new_accounts(self):
        plan = generate_plan(
            self.config,
            source_snapshot(),
            destination_snapshot(),
            options={"random_password": True, "fixed_password": ""},
            selection={"accounts": ["alice@src.fr"]},
            category="account",
        )
        action = next(
            item for item in plan["actions"] if item["kind"] == "create_account"
        )
        self.assertEqual(action["payload"]["password_source"], "random")
        self.assertFalse(
            any("mot de passe fixe" in warning for warning in plan["warnings"])
        )

    def tearDown(self):
        self.tmp.cleanup()

    def kinds(self):
        return [action["kind"] for action in self.plan["actions"]]

    def target(self, kind, needle):
        return [
            action
            for action in self.plan["actions"]
            if action["kind"] == kind and needle in action["target"]
        ]

    def test_domain_already_present(self):
        self.assertNotIn("create_domain", self.kinds())

    def test_missing_cos_created_with_mapping(self):
        actions = self.target("create_cos", "staff")
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["payload"]["attrs"]["zimbraMailQuota"], "2147483648")

    def test_missing_account_created_with_mapped_domain(self):
        actions = self.target("create_account", "alice@dst.fr")
        self.assertEqual(len(actions), 1)
        payload = actions[0]["payload"]
        self.assertEqual(payload["name"], "alice@dst.fr")
        self.assertEqual(payload["cos_name"], "staff")
        self.assertEqual(payload["mail_host"], "dst-zimbra")
        self.assertEqual(payload["attrs"]["zimbraMailQuota"], "1073741824")
        self.assertNotIn("zimbraId", payload["attrs"])
        self.assertNotIn("zimbraMailAlias", payload["attrs"])

    def test_alias_mapped(self):
        actions = self.target("add_alias", "alice.old@dst.fr")
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["payload"]["account"], "alice@dst.fr")

    def test_existing_account_modified_without_deletion(self):
        actions = self.target("modify_account", "bob@dst.fr")
        self.assertEqual(len(actions), 1)
        attrs = actions[0]["payload"]["attrs"]
        self.assertEqual(attrs["zimbraMailQuota"], "2000")
        self.assertNotIn("zimbraPrefDisplayName", attrs)

    def test_distribution_list_created_with_mapped_member(self):
        actions = self.target("create_dl", "team@dst.fr")
        self.assertEqual(len(actions), 1)
        members = actions[0]["payload"]["members"]
        self.assertIn("alice@dst.fr", members)
        self.assertIn("external@other.com", members)

    def test_resource_created(self):
        actions = self.target("create_resource", "room@dst.fr")
        self.assertEqual(len(actions), 1)
        self.assertEqual(
            actions[0]["payload"]["attrs"]["zimbraCalResType"], "Location"
        )

    def test_no_delete_action_and_extras_kept(self):
        for kind in self.kinds():
            self.assertNotIn("delete", kind)
            self.assertNotIn("remove", kind)
        self.assertTrue(any("conserv" in item for item in self.plan["info"]))
        self.assertEqual(self.plan["source"]["host"], "src-zimbra")
        extras = [a for a in self.plan["accounts"] if a["destination"] == "extra@dst.fr"]
        self.assertEqual(extras, [])

    def test_mailboxes_listed(self):
        self.assertEqual(self.plan["mailbox_count"], 2)
        destinations = {item["destination"] for item in self.plan["mailboxes"]}
        self.assertEqual(destinations, {"alice@dst.fr", "bob@dst.fr"})

    def test_accounts_pairs_always_present(self):
        self.assertEqual(len(self.plan["accounts"]), 2)

    def test_password_reference_not_stored(self):
        actions = self.target("create_account", "alice@dst.fr")
        payload = actions[0]["payload"]
        self.assertEqual(payload["password_source"], "fixed")
        self.assertNotIn("Passw0rd!", str(self.plan))

    def test_ignore_lists_are_applied(self):
        filtered = filtered_attrs(
            self.config,
            {
                "zimbraId": "x",
                "zimbraMailHost": "mail.example.com",
                "zimbraMailQuota": "1",
                "zimbraAccountStatus": "active",
            },
        )
        self.assertEqual(
            filtered,
            {"zimbraMailQuota": "1", "zimbraAccountStatus": "active"},
        )
        changes = changed_attrs(
            self.config, {"zimbraId": "x", "zimbraMailQuota": "2"}, {"zimbraMailQuota": "2"}
        )
        self.assertEqual(changes, {})


class PlanOptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(self.tmp.name)
        self.config.connection("source")["store_host"] = "src-zimbra"
        self.config.connection("destination")["store_host"] = "dst-zimbra"
        self.config.save()

    def tearDown(self):
        self.tmp.cleanup()

    def test_selection_filters_accounts_and_groups(self):
        plan = generate_plan(
            self.config,
            source_snapshot(),
            destination_snapshot(),
            options={
                "fixed_password": "Passw0rd!",
                "content_types": ["mail", "contacts"],
            },
            selection={
                "accounts": ["alice@src.fr"],
                "include_domains": False,
                "include_cos": False,
                "include_distribution_lists": False,
                "include_resources": False,
            },
        )
        self.assertEqual([pair["source"] for pair in plan["accounts"]], ["alice@src.fr"])
        self.assertEqual(plan["mailbox_count"], 1)
        self.assertEqual(plan["options"]["content_types"], ["mail", "contacts"])
        self.assertFalse(plan["selection"]["all_accounts"])
        kinds = {action["kind"] for action in plan["actions"]}
        self.assertNotIn("create_domain", kinds)
        self.assertNotIn("create_cos", kinds)
        self.assertNotIn("create_dl", kinds)
        self.assertIn("create_account", kinds)
        self.assertTrue(plan["options"]["fixed_password"])
        self.assertNotIn("Passw0rd!", str(plan))
        self.assertTrue(plan["options"]["fixed_password_encrypted"])

    def test_validation_allows_same_host_snapshots(self):
        plan = generate_plan(
            self.config,
            source_snapshot(),
            source_snapshot(),
        )
        self.assertTrue(plan["validation"]["ok"])
        self.assertTrue(plan["validation"]["same_host"])
        self.assertEqual(plan["validation"]["hard_errors"], [])

    def test_validation_blocks_wrong_destination_host(self):
        destination = destination_snapshot()
        destination["host"] = "other-host"
        plan = generate_plan(
            self.config, source_snapshot(), destination
        )
        self.assertFalse(plan["validation"]["ok"])
        self.assertFalse(plan["validation"]["same_host"])
        self.assertTrue(
            any("ne correspond pas" in error for error in plan["validation"]["hard_errors"])
        )

    def test_delegated_sender_is_deferred_and_addresses_are_mapped(self):
        source = source_snapshot()
        alice = source["accounts"][0]
        alice["attrs"]["zimbraPrefAllowAddressForDelegatedSender"] = [
            "alice@src.fr",
            "alice.old@src.fr",
        ]
        alice["attrs"]["zimbraPrefFromAddress"] = "alice@src.fr"
        alice["attrs"]["zimbraPrefReplyToAddress"] = "external@other.com"
        plan = generate_plan(
            self.config,
            source,
            destination_snapshot(),
            options={"domain_map": {"src.fr": "dst.fr"}},
        )
        create = [
            action
            for action in plan["actions"]
            if action["kind"] == "create_account"
            and action["target"] == "alice@dst.fr"
        ][0]
        attrs = create["payload"]["attrs"]
        self.assertNotIn("zimbraPrefAllowAddressForDelegatedSender", attrs)
        self.assertEqual(attrs["zimbraPrefFromAddress"], "alice@dst.fr")
        self.assertEqual(attrs["zimbraPrefReplyToAddress"], "external@other.com")
        delegated = [
            action
            for action in plan["actions"]
            if action["kind"] == "set_delegated_sender"
            and action["target"] == "alice@dst.fr"
        ][0]
        self.assertEqual(
            delegated["payload"]["values"],
            ["alice@dst.fr", "alice.old@dst.fr"],
        )
        alias_targets = [
            action["target"]
            for action in plan["actions"]
            if action["kind"] == "add_alias"
        ]
        self.assertNotIn("alice@dst.fr", alias_targets)

    def test_explicit_object_selection_filters_domains_cos_lists_resources(self):
        plan = generate_plan(
            self.config,
            source_snapshot(),
            destination_snapshot(),
            options={"domain_map": {"src.fr": "new.fr"}},
            selection={
                "accounts": None,
                "domains": ["src.fr"],
                "cos": ["staff"],
                "distribution_lists": ["team@src.fr"],
                "resources": ["room@src.fr"],
            },
        )
        kinds = {action["kind"] for action in plan["actions"]}
        self.assertIn("create_domain", kinds)
        self.assertIn("create_cos", kinds)
        self.assertIn("create_dl", kinds)
        self.assertIn("create_resource", kinds)
        cos_targets = [
            action["target"]
            for action in plan["actions"]
            if action["kind"] == "create_cos"
        ]
        self.assertEqual(cos_targets, ["staff"])
        self.assertEqual(plan["selection"]["domains"], ["src.fr"])
        self.assertEqual(plan["selection"]["cos"], ["staff"])
        self.assertTrue(plan["id_map"])

    def test_empty_domain_selection_creates_no_domain(self):
        plan = generate_plan(
            self.config,
            source_snapshot(),
            destination_snapshot(),
            selection={"accounts": None, "domains": []},
        )
        self.assertFalse(
            any(action["kind"] == "create_domain" for action in plan["actions"])
        )

    def test_keep_admin_permissions_copies_admin_attributes(self):
        source = source_snapshot()
        source["accounts"][0]["attrs"]["zimbraIsAdminAccount"] = "TRUE"
        source["accounts"][0]["attrs"]["zimbraIsDelegatedAdminAccount"] = "TRUE"
        source["accounts"][0]["attrs"]["zimbraIsExternalVirtualAccount"] = "TRUE"
        without = generate_plan(
            self.config,
            source,
            destination_snapshot(),
            options={"domain_map": {"src.fr": "dst.fr"}, "keep_admin_permissions": False},
        )
        attrs_without = [
            action
            for action in without["actions"]
            if action["kind"] == "create_account"
            and action["target"] == "alice@dst.fr"
        ][0]["payload"]["attrs"]
        self.assertNotIn("zimbraIsAdminAccount", attrs_without)
        self.assertNotIn("zimbraIsDelegatedAdminAccount", attrs_without)
        self.assertNotIn("zimbraIsExternalVirtualAccount", attrs_without)
        kept = generate_plan(
            self.config,
            source,
            destination_snapshot(),
            options={"domain_map": {"src.fr": "dst.fr"}, "keep_admin_permissions": True},
        )
        attrs_kept = [
            action
            for action in kept["actions"]
            if action["kind"] == "create_account"
            and action["target"] == "alice@dst.fr"
        ][0]["payload"]["attrs"]
        self.assertEqual(attrs_kept["zimbraIsAdminAccount"], "TRUE")
        self.assertEqual(attrs_kept["zimbraIsDelegatedAdminAccount"], "TRUE")
        self.assertEqual(attrs_kept["zimbraIsExternalVirtualAccount"], "TRUE")

    def test_password_and_csrf_tokens_are_never_copied(self):
        source = source_snapshot()
        source["accounts"][0]["attrs"]["userPassword"] = "VALUE-BLOCKED"
        source["accounts"][0]["attrs"]["zimbraCsrfTokenData"] = ["token"]
        plan = generate_plan(
            self.config,
            source,
            destination_snapshot(),
            options={"domain_map": {"src.fr": "dst.fr"}},
        )
        create = [
            action
            for action in plan["actions"]
            if action["kind"] == "create_account"
            and action["target"] == "alice@dst.fr"
        ][0]
        self.assertNotIn("userPassword", create["payload"]["attrs"])
        self.assertNotIn("zimbraCsrfTokenData", create["payload"]["attrs"])

    def test_target_address_already_used_as_alias_is_a_conflict(self):
        destination = destination_snapshot()
        extra = [
            account
            for account in destination["accounts"]
            if account["name"] == "extra@dst.fr"
        ][0]
        extra["aliases"] = ["alice@dst.fr"]
        plan = generate_plan(
            self.config,
            source_snapshot(),
            destination,
            options={"domain_map": {"src.fr": "dst.fr"}},
        )
        self.assertFalse(
            any(
                action["kind"] == "create_account"
                and action["target"] == "alice@dst.fr"
                for action in plan["actions"]
            )
        )
        pair = [
            item
            for item in plan["accounts"]
            if item["source"] == "alice@src.fr"
        ][0]
        self.assertEqual(pair["status"], "conflict")
        self.assertTrue(any("alias" in warning for warning in plan["warnings"]))

