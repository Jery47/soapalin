import tempfile
import unittest
from unittest import mock

from soaplun import inventory as inventory_module
from soaplun import settings as settings_module
from soaplun.config import Config


class FakeDestination:
    def __init__(self, **kwargs):
        self.calls = []
        self._lists = kwargs.get("lists", {})
        self._permissions = kwargs.get("permissions", {})
        self._filters = kwargs.get("filters", {})
        self._signatures = kwargs.get("signatures", [])

    def request(self, name, content=None):
        self.calls.append((name, content or {}))
        if name == "GetPermission":
            return self._permissions
        return {}

    def get_white_black_lists(self):
        return self._lists

    def get_permissions(self):
        return self._permissions

    def get_filter_rules(self, way="in"):
        return self._filters.get(way, [])

    def get_signatures(self):
        return self._signatures

    def create_signature(self, name, content, contenttype="text/html"):
        self.calls.append(("create_signature", (name, content, contenttype)))


class SettingsTests(unittest.TestCase):
    def test_normalize_parts_follows_options_and_explicit_parts(self):
        self.assertEqual(
            settings_module.normalize_parts(["settings", "shares"], {}),
            ["settings", "shares"],
        )
        parts = settings_module.normalize_parts(
            None,
            {
                "copy_settings": True,
                "copy_prefs": False,
                "copy_filters": False,
                "copy_whitelists": True,
                "copy_shares": False,
                "copy_permissions": True,
            },
        )
        self.assertEqual(parts, ["settings", "folders", "lists", "permissions"])

    def test_a_entries_repeats_lists(self):
        entries = settings_module.a_entries({"zimbraPrefMailSignature": ["a", "b"]})
        self.assertEqual(len(entries), 2)

    def test_pref_entries_use_name_attribute(self):
        entries = settings_module.pref_entries({"zimbraPrefLocale": "fr_FR"})
        self.assertEqual(entries, [{"name": "zimbraPrefLocale", "_content": "fr_FR"}])
        booleans = settings_module.pref_entries({"zimbraPrefMailFlashIcon": True})
        self.assertEqual(
            booleans, [{"name": "zimbraPrefMailFlashIcon", "_content": "TRUE"}]
        )
        repeated = settings_module.pref_entries({"zimbraPrefMailForwardingAddress": ["a@x", "b@x"]})
        self.assertEqual([entry["name"] for entry in repeated], ["zimbraPrefMailForwardingAddress"] * 2)

    def test_white_black_lists_add_only_missing(self):
        destination = FakeDestination(
            lists={
                "whiteList": {"addr": [{"_content": "keep@x.fr"}]},
                "blackList": {"addr": []},
            }
        )
        result = settings_module.copy_white_black_lists(
            {
                "whitelist": ["keep@x.fr", "new@x.fr"],
                "blacklist": ["bad@x.fr"],
            },
            destination,
            dry_run=False,
        )
        self.assertEqual(result["white_added"], 1)
        self.assertEqual(result["black_added"], 1)
        call = destination.calls[0]
        self.assertEqual(call[0], "ModifyWhiteBlackList")
        self.assertIn("new@x.fr", str(call[1]))

    def test_permissions_skip_existing(self):
        destination = FakeDestination(
            permissions={"ace": [{"right": "sendAs", "d": "a@x.fr"}]}
        )
        result = settings_module.copy_permissions(
            {
                "permissions": [
                    {"right": "sendAs", "d": "a@x.fr", "gt": "usr"},
                    {"right": "sendOnBehalfOf", "d": "b@x.fr", "gt": "usr"},
                ]
            },
            destination,
            dry_run=False,
        )
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["created"], 1)
        grants = [call for call in destination.calls if call[0] == "GrantPermission"]
        self.assertEqual(len(grants), 1)
        self.assertEqual(grants[0][1]["ace"]["d"], "b@x.fr")

    def test_filters_preserve_destination_rules(self):
        destination = FakeDestination(
            filters={
                "in": [
                    {"name": "dest-only", "active": "1"},
                    {"name": "shared", "active": "0"},
                ]
            }
        )
        result = settings_module.copy_filters(
            {
                "filters_in": [
                    {"name": "shared", "active": "1"},
                    {"name": "source-only", "active": "1"},
                ]
            },
            destination,
            dry_run=False,
            way="in",
        )
        self.assertEqual(result["updated"], 2)
        rules = destination.calls[0][1]["filterRules"]["filterRule"]
        names = [rule["name"] for rule in rules]
        self.assertIn("dest-only", names)
        self.assertIn("source-only", names)
        self.assertEqual(
            [rule for rule in rules if rule["name"] == "shared"][0]["active"], "1"
        )

    def test_signatures_created_only_when_missing(self):
        class Signature:
            def __init__(self, name):
                self._name = name

            def get_full_data(self):
                return {"name": self._name, "content": {"_content": "x", "type": "text/html"}}

        destination = FakeDestination(signatures=[Signature("existing")])
        result = settings_module.copy_signatures(
            {
                "signatures": [
                    {"name": "existing", "content": {"_content": "old"}},
                    {"name": "new", "content": {"_content": "new", "type": "text/plain"}},
                ]
            },
            destination,
            dry_run=False,
        )
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["created"], 1)
        self.assertEqual(destination.calls[0][0], "create_signature")
        self.assertEqual(destination.calls[0][1][0], "new")

    def test_dry_run_writes_nothing(self):
        destination = FakeDestination(
            lists={"whiteList": {"addr": []}, "blackList": {"addr": []}}
        )
        result = settings_module.copy_white_black_lists(
            {"whitelist": ["a@x.fr"], "blacklist": []}, destination, dry_run=True
        )
        self.assertEqual(result["white_added"], 1)
        self.assertEqual(destination.calls, [])


class IncomingShareTests(unittest.TestCase):
    class Destination:
        def __init__(self, shares=None):
            self.calls = []
            self.shares = shares or []

        def request(self, name, content=None):
            self.calls.append((name, content or {}))
            if name == "GetFolder":
                return {"folder": {"id": "1"}}
            return {}

        def get_share_info(self):
            return self.shares

        def create_mountpoint(self, **kwargs):
            self.calls.append(("create_mountpoint", kwargs))
            return {"id": "mp-1"}

    class Session:
        def __init__(self):
            self.owners = []

        def request_single(self, name, content=None):
            if name == "GetAccount":
                return {"account": {"id": "owner-id"}}
            return {}

        def account_client(self, email):
            self.owners.append(email)
            owner = self

            class OwnerClient:
                def request(self, name, content=None):
                    return {"folder": {"id": "folder-id"}}

            return OwnerClient()

    def test_accepted_incoming_share_creates_mountpoint(self):
        destination = self.Destination()
        session = self.Session()
        result = settings_module.copy_incoming_shares(
            {
                "mountpoints": [
                    {
                        "mid": "12",
                        "ownerEmail": "owner@example.com",
                        "folderPath": "/Calendar",
                        "view": "appointment",
                        "rights": "r",
                    }
                ]
            },
            session,
            destination,
            "user@example.com",
            dry_run=False,
        )
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["errors"], [])
        call = [item for item in destination.calls if item[0] == "create_mountpoint"]
        self.assertEqual(len(call), 1)
        payload = call[0][1]
        self.assertEqual(payload["rid"], "folder-id")
        self.assertEqual(payload["zid"], "owner-id")
        self.assertEqual(payload["view"], "appointment")
        self.assertEqual(payload["perm"], "r")

    def test_unaccepted_incoming_share_is_skipped(self):
        destination = self.Destination()
        result = settings_module.copy_incoming_shares(
            {
                "mountpoints": [
                    {
                        "ownerEmail": "owner@example.com",
                        "folderPath": "/Calendar",
                    }
                ]
            },
            self.Session(),
            destination,
            "user@example.com",
            dry_run=False,
        )
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertFalse(
            any(item[0] == "create_mountpoint" for item in destination.calls)
        )


class SettingsParallelTests(unittest.TestCase):
    def test_account_settings_parallelism_reports_all_pairs(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(tmp)
            plan = {
                "accounts": [
                    {"source": "a@example.com", "destination": "a@example.com"},
                    {"source": "b@example.com", "destination": "b@example.com"},
                ]
            }

            class Session:
                def account_client(self, email):
                    return object()

                def mail_client(self, email):
                    return object()

            with mock.patch.object(
                settings_module, "preflight", return_value={"ok": True}
            ), mock.patch.object(
                settings_module, "ZimbraSession", return_value=Session()
            ), mock.patch.object(
                settings_module.inventory_module,
                "collect_account_settings",
                return_value={"all_folders": []},
            ), mock.patch.object(
                settings_module,
                "copy_empty_folders",
                return_value={"created": 0, "skipped": 0, "errors": []},
            ):
                report = settings_module.copy_account_settings(
                    config,
                    plan,
                    parts=["folder"],
                    dry_run=True,
                    parallelism=2,
                )
        self.assertEqual(report["summary"]["ok"], 2)


class PreferencesTests(unittest.TestCase):
    class Destination:
        def __init__(self):
            self.calls = []

        def get_preferences(self):
            return {"zimbraPrefSkin": "bare"}

        def request(self, name, content=None):
            names = [
                pref["name"]
                for pref in (content or {}).get("pref", [])
            ]
            if "zimbraPrefBroken" in names:
                raise ValueError("invalid preference")
            self.calls.append((name, content or {}))

    def test_valid_preferences_are_applied_when_one_is_invalid(self):
        destination = self.Destination()
        result = settings_module.copy_preferences(
            None,
            {
                "prefs": {
                    "zimbraPrefSkin": "carbon",
                    "zimbraPrefLocale": "fr",
                    "zimbraPrefBroken": "bad",
                }
            },
            destination,
            dry_run=False,
            options={"pref_ignore": []},
        )
        self.assertEqual(result["updated"], 2)
        self.assertEqual(len(result["errors"]), 1)
        applied = []
        for name, content in destination.calls:
            if name == "ModifyPrefs":
                applied.extend(
                    pref["name"] for pref in content.get("pref", [])
                )
        self.assertIn("zimbraPrefSkin", applied)
        self.assertIn("zimbraPrefLocale", applied)
        self.assertNotIn("zimbraPrefBroken", applied)


class FolderCreationTests(unittest.TestCase):
    class Destination:
        def __init__(self):
            self.calls = []

        def request(self, name, content=None):
            if name == "GetFolder":
                return {
                    "folder": {
                        "absFolderPath": "/",
                        "id": "1",
                        "folder": [
                            {"absFolderPath": "/Inbox", "id": "10"},
                        ],
                    }
                }
            self.calls.append((name, content or {}))
            if name == "CreateFolder":
                return {"folder": {"id": str(100 + len(self.calls))}}
            return {}

    def test_creates_missing_empty_folders_in_parent_order(self):
        destination = self.Destination()
        result = settings_module.copy_empty_folders(
            {
                "all_folders": [
                    {"path": "/Inbox", "name": "Inbox", "view": "message"},
                    {"path": "/Inbox/Test", "name": "Test", "view": "message"},
                    {
                        "path": "/Inbox/Test/Child",
                        "name": "Child",
                        "view": "message",
                    },
                ]
            },
            destination,
            dry_run=False,
        )
        self.assertEqual(result["created"], 2)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["errors"], [])
        calls = [call for call in destination.calls if call[0] == "CreateFolder"]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1]["folder"]["name"], "Test")
        self.assertEqual(calls[0][1]["folder"]["l"], "10")
        self.assertEqual(calls[1][1]["folder"]["name"], "Child")
        self.assertEqual(calls[1][1]["folder"]["l"], "101")


    def test_dry_run_counts_nested_folders(self):
        destination = FolderCreationTests.Destination()
        result = settings_module.copy_empty_folders(
            {
                "all_folders": [
                    {"path": "/Inbox/Test", "name": "Test", "view": "message"},
                    {
                        "path": "/Inbox/Test/Child",
                        "name": "Child",
                        "view": "message",
                    },
                ]
            },
            destination,
            dry_run=True,
        )
        self.assertEqual(result["created"], 2)
        self.assertEqual(result["errors"], [])
        self.assertEqual(
            [call for call in destination.calls if call[0] == "CreateFolder"],
            [],
        )

class FolderColorTests(unittest.TestCase):
    class Destination:
        def __init__(self):
            self.calls = []

        def request(self, name, content=None):
            if name == "GetFolder":
                return {
                    "folder": {
                        "absFolderPath": "/",
                        "id": "1",
                        "folder": [
                            {"absFolderPath": "/Inbox/Test", "id": "10"},
                            {"absFolderPath": "/Calendar", "id": "11"},
                        ],
                    }
                }
            self.calls.append((name, content or {}))
            return {}

    def test_copies_numeric_folder_color(self):
        destination = self.Destination()
        result = settings_module.copy_folder_colors(
            {
                "folders": [
                    {
                        "path": "/Inbox/Test",
                        "id": "1",
                        "name": "Test",
                        "color": "4",
                        "rgb": "",
                    }
                ]
            },
            destination,
            dry_run=False,
        )
        self.assertEqual(result["updated"], 1)
        self.assertEqual(destination.calls[0][0], "FolderAction")
        self.assertEqual(destination.calls[0][1]["action"]["color"], "4")
        self.assertEqual(destination.calls[0][1]["action"]["id"], "10")

    def test_copies_rgb_folder_color(self):
        destination = self.Destination()
        result = settings_module.copy_folder_colors(
            {
                "folders": [
                    {
                        "path": "/Calendar",
                        "id": "2",
                        "name": "Calendar",
                        "color": "",
                        "rgb": "#00CCCC",
                    }
                ]
            },
            destination,
            dry_run=False,
        )
        self.assertEqual(result["updated"], 1)
        self.assertEqual(
            destination.calls[0][1]["action"]["color"], "2"
        )


class OutgoingShareTests(unittest.TestCase):
    class FolderClient:
        def __init__(self):
            self.calls = []

        def request(self, name, content=None):
            self.calls.append((name, content or {}))
            if name == "GetFolder":
                return {
                    "folder": {
                        "absFolderPath": "/",
                        "id": "1",
                        "folder": [{"absFolderPath": "/Calendar", "id": "10"}],
                    }
                }
            return {}

    class DestinationClient:
        def __init__(self):
            self.calls = []

        def request(self, name, content=None):
            self.calls.append((name, content or {}))
            return {}

    class Session:
        def request_single(self, name, content=None):
            grantee = (content or {}).get("account", {}).get("_content")
            if grantee == "missing@example.com":
                raise ValueError("no such account")
            return {"account": {"name": grantee}}

    def test_collects_grants_from_folder_acl(self):
        client = self.FolderClient()
        # Patch the returned tree with an ACL grant.
        original = client.request

        def request(name, content=None):
            payload = original(name, content)
            if name == "GetFolder":
                payload["folder"]["folder"][0]["acl"] = {
                    "grant": {
                        "d": "eric.dhorne@sdis21.org",
                        "gt": "usr",
                        "perm": "r",
                    }
                }
            return payload

        client.request = request
        shares = inventory_module.collect_outgoing_shares(
            client, "claire.genty@sdis21.org"
        )
        self.assertEqual(
            shares,
            [
                {
                    "folderPath": "/Calendar",
                    "granteeName": "eric.dhorne@sdis21.org",
                    "granteeType": "usr",
                    "rights": "r",
                }
            ],
        )

    def test_missing_grantee_is_ignored(self):
        destination_mail = self.FolderClient()
        destination = self.DestinationClient()
        result = settings_module.copy_outgoing_shares(
            None,
            {
                "shares_out": [
                    {
                        "folderPath": "/Calendar",
                        "granteeName": "missing@example.com",
                        "granteeType": "usr",
                        "rights": "r",
                    }
                ]
            },
            destination,
            "claire.genty@sdis21.org",
            dry_run=False,
            options={"domain_map": {}},
            destination_session=self.Session(),
            destination_mail=destination_mail,
        )
        self.assertEqual(result["created"], 0)
        self.assertEqual(result["ignored"], 1)
        self.assertEqual(
            result["ignored_shares"],
            [
                {
                    "folder": "/Calendar",
                    "grantee": "missing@example.com",
                    "type": "usr",
                }
            ],
        )
        self.assertEqual(destination_mail.calls[0][0], "GetFolder")


class AccountAttributeTests(unittest.TestCase):
    class Destination:
        def __init__(self):
            self.calls = []

        def request_single(self, name, content=None):
            return {
                "account": {
                    "id": "123",
                    "a": [{"n": "sn", "_content": "old"}],
                }
            }

        def request(self, name, content=None):
            self.calls.append((name, content or {}))
            return {}

    def test_copies_selected_account_attributes(self):
        destination = self.Destination()
        result = settings_module.copy_account_attributes(
            {
                "account_attrs": {
                    "givenName": "Frederic",
                    "sn": "PERRET",
                    "zimbraAuthLdapExternalDn": "10162@sdis2.priv",
                }
            },
            destination,
            "frederic.perret@sdis21.org",
            dry_run=False,
            ignore=[],
        )
        self.assertEqual(result["updated"], 3)
        self.assertEqual(result["errors"], [])
        self.assertEqual(destination.calls[0][0], "ModifyAccount")
        names = [entry["n"] for entry in destination.calls[0][1]["a"]]
        self.assertIn("givenName", names)
        self.assertIn("sn", names)
        self.assertIn("zimbraAuthLdapExternalDn", names)


class CosTests(unittest.TestCase):
    def test_cos_is_applied_by_destination_id(self):
        class Destination:
            def __init__(self):
                self.calls = []

            def request_single(self, name, content=None):
                if name == "GetCos":
                    return {"cos": {"id": "dest-cos", "name": "standard-sdis21"}}
                if name == "GetAccount":
                    return {
                        "account": {
                            "id": "account-id",
                            "a": [{"n": "zimbraCOSId", "_content": "old-cos"}],
                        }
                    }
                return {}

            def request(self, name, content=None):
                self.calls.append((name, content or {}))
                return {}

        destination = Destination()
        result = settings_module.copy_cos(
            {"cos_name": "standard-sdis21"},
            destination,
            "u@example.com",
            dry_run=False,
        )
        self.assertEqual(result["updated"], 1)
        call = destination.calls[-1]
        self.assertEqual(call[0], "ModifyAccount")
        self.assertEqual(call[1]["id"], "account-id")
        self.assertEqual(
            call[1]["a"],
            [{"n": "zimbraCOSId", "_content": "dest-cos"}],
        )


class SignatureTests(unittest.TestCase):
    def test_signature_ids_are_mapped_from_source_names(self):
        class Destination:
            def __init__(self):
                self.calls = []

            def get_signatures(self):
                return [{"name": "sign3", "id": "dest-id"}]

            def get_preferences(self):
                return {}

            def request(self, name, content=None):
                self.calls.append((name, content or {}))
                return {}

        destination = Destination()
        result = settings_module.copy_signatures(
            {"signatures": [{"name": "sign3", "id": "source-id"}]},
            destination,
            dry_run=False,
        )
        self.assertEqual(result["id_map"], {"source-id": "dest-id"})
        settings_module.copy_preferences(
            None,
            {
                "prefs": {
                    "zimbraPrefDefaultSignatureId": "source-id",
                    "zimbraPrefForwardReplySignatureId": "source-id",
                }
            },
            destination,
            dry_run=False,
            options={"pref_ignore": []},
            signature_id_map=result["id_map"],
        )
        call = destination.calls[-1]
        self.assertEqual(call[0], "ModifyPrefs")
        prefs = call[1]["pref"]
        values = {
            item["name"]: item["_content"] for item in prefs
        }
        self.assertEqual(
            values["zimbraPrefDefaultSignatureId"], "dest-id"
        )
        self.assertEqual(
            values["zimbraPrefForwardReplySignatureId"], "dest-id"
        )


class TagTests(unittest.TestCase):
    def test_tags_are_created_additively(self):
        class Destination:
            def __init__(self):
                self.calls = []

            def request(self, name, content=None):
                self.calls.append((name, content or {}))
                if name == "GetTag":
                    return {"tag": [{"name": "existing", "color": "1"}]}
                return {}

        destination = Destination()
        result = settings_module.copy_tags(
            {
                "tags": [
                    {"name": "existing", "color": "1"},
                    {"name": "urgent", "color": "6"},
                ]
            },
            destination,
            dry_run=False,
        )
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["skipped"], 1)
        action = destination.calls[-1]
        self.assertEqual(action[0], "CreateTag")
        self.assertEqual(action[1]["tag"]["name"], "urgent")
        self.assertEqual(action[1]["tag"]["color"], "6")

    def test_existing_tag_color_is_updated(self):
        class Destination:
            def __init__(self):
                self.calls = []

            def request(self, name, content=None):
                self.calls.append((name, content or {}))
                if name == "GetTag":
                    return {"tag": [{"id": "12", "name": "tag1", "color": "1"}]}
                return {}

        destination = Destination()
        result = settings_module.copy_tags(
            {"tags": [{"name": "tag1", "color": "6"}]},
            destination,
            dry_run=False,
        )
        self.assertEqual(result["updated"], 1)
        self.assertEqual(result["created"], 0)
        action = destination.calls[-1]
        self.assertEqual(action[0], "TagAction")
        self.assertEqual(action[1]["action"]["op"], "color")
        self.assertEqual(action[1]["action"]["id"], "12")
        self.assertEqual(action[1]["action"]["color"], "6")

    def test_tags_are_dry_run_safe(self):
        class Destination:
            def request(self, name, content=None):
                if name == "GetTag":
                    return {}
                raise AssertionError("CreateTag must not run in dry-run")

        result = settings_module.copy_tags(
            {"tags": [{"name": "urgent"}]}, Destination(), dry_run=True
        )
        self.assertEqual(result["created"], 1)


if __name__ == "__main__":
    unittest.main()
