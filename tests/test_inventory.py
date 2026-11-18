import tempfile
import unittest
from pathlib import Path
from unittest import mock

from soaplun import inventory as inventory_module
from soaplun.config import Config
from soaplun.inventory import (
    _domain_entries,
    account_domain,
    as_list,
    attr_entries,
    attrs_to_dict,
    bool_attr,
    cleanup_snapshots,
    collect_account_settings,
    collect_accounts,
    collect_cos,
    collect_folder_bundle,
    entry_name,
    latest_snapshot,
    list_snapshots,
    load_snapshot,
    load_snapshot_index,
    snapshot_index_path,
    unwrap,
    write_snapshot,
)


class InventoryParsingTests(unittest.TestCase):
    def test_attrs_to_dict_repeats_values(self):
        entry = {
            "name": "a@x",
            "a": [
                {"n": "zimbraMailAlias", "_content": "a1@x"},
                {"n": "zimbraMailAlias", "_content": "a2@x"},
                {"n": "zimbraMailQuota", "_content": "100"},
            ],
        }
        attrs = attrs_to_dict(entry)
        self.assertEqual(attrs["zimbraMailAlias"], ["a1@x", "a2@x"])
        self.assertEqual(attrs["zimbraMailQuota"], "100")

    def test_attrs_to_dict_tolerates_bad_entries(self):
        entry = {"a": [None, {"n": None}, {"n": "x"}]}
        self.assertEqual(attrs_to_dict(entry), {"x": ""})
        self.assertEqual(attr_entries(None), [])

    def test_helpers(self):
        self.assertEqual(as_list("x"), ["x"])
        self.assertEqual(as_list(None), [])
        self.assertEqual(unwrap({"domain": {"name": "x"}}, "domain"), {"name": "x"})
        self.assertEqual(entry_name({"name": "a@x"}), "a@x")
        self.assertEqual(account_domain("a@b.fr"), "b.fr")
        self.assertEqual(account_domain("nobody"), "")
        self.assertTrue(bool_attr({"zimbraIsAdminAccount": "TRUE"}, "zimbraIsAdminAccount"))
        self.assertFalse(bool_attr({}, "zimbraIsAdminAccount"))


class _FakeSession:
    def __init__(self, list_responses=None, search_responses=None):
        self.list_responses = list_responses or {}
        self.search_responses = search_responses or {}
        self.calls = []
        self.search_calls = []

    def request_list(self, name, content=None):
        self.calls.append((name, content))
        selector = (content or {}).get("domain") or {}
        value = self.list_responses[(name, selector.get("_content"))]
        if isinstance(value, Exception):
            raise value
        return value

    def request(self, name, content=None):
        self.search_calls.append((name, content))
        query = (content or {}).get("query", "")
        value = self.search_responses.get(query, {"searchTotal": "0", "more": "0"})
        if isinstance(value, Exception):
            raise value
        return value


class DomainListingTests(unittest.TestCase):
    def test_skips_alias_domains_and_falls_back_to_search(self):
        domains = [
            {"name": "a.fr", "is_alias": False},
            {"name": "alias.fr", "is_alias": True},
            {"name": "b.fr", "is_alias": False},
        ]
        session = _FakeSession(
            {
                ("GetAllAccounts", "a.fr"): [{"name": "u1@a.fr"}],
                ("GetAllAccounts", "b.fr"): RuntimeError("boom"),
            },
            {
                "(mail=*@b.fr)": {
                    "account": [{"name": "u2@b.fr"}],
                    "searchTotal": "1",
                    "more": "0",
                }
            },
        )
        warnings = []
        entries = _domain_entries(
            session, "GetAllAccounts", domains, warnings, "accounts"
        )
        self.assertEqual(
            [item["name"] for item in entries], ["u1@a.fr", "u2@b.fr"]
        )
        self.assertEqual(
            session.calls,
            [
                ("GetAllAccounts", {"domain": {"by": "name", "_content": "a.fr"}}),
                ("GetAllAccounts", {"domain": {"by": "name", "_content": "b.fr"}}),
            ],
        )
        self.assertEqual(
            session.search_calls,
            [
                (
                    "SearchDirectory",
                    {
                        "types": "accounts",
                        "query": "(mail=*@b.fr)",
                        "limit": 1000,
                        "offset": 0,
                    },
                )
            ],
        )
        self.assertEqual(warnings, [])

    def test_warns_when_fallback_also_fails(self):
        domains = [{"name": "b.fr", "is_alias": False}]
        session = _FakeSession(
            {("GetAllAccounts", "b.fr"): RuntimeError("boom")},
            {"(mail=*@b.fr)": RuntimeError("nope")},
        )
        warnings = []
        entries = _domain_entries(
            session, "GetAllAccounts", domains, warnings, "accounts"
        )
        self.assertEqual(entries, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("b.fr", warnings[0])
        self.assertIn("nope", warnings[0])

    def test_cos_uses_correct_zimbra_document_names(self):
        calls = []

        class FakeClient:
            def request_list(self, name, content=None):
                calls.append(name)
                return [{"name": "default", "id": "1"}]

            def request_single(self, name, content=None):
                calls.append(name)
                return {"cos": {"name": "default", "id": "1", "a": []}}

        items = collect_cos(FakeClient())
        self.assertEqual(calls, ["GetAllCos", "GetCos"])
        self.assertEqual(items, [{"name": "default", "id": "1", "attrs": {}}])


class SelectiveSettingsTests(unittest.TestCase):
    def test_tags_part_skips_unrelated_calls(self):
        class Account:
            def get_signatures(self):
                raise AssertionError("signatures must not be collected")

            def get_identities(self):
                raise AssertionError("identities must not be collected")

            def get_preferences(self):
                raise AssertionError("prefs must not be collected")

            def get_white_black_lists(self):
                raise AssertionError("lists must not be collected")

            def get_share_info(self):
                raise AssertionError("shares must not be collected")

        class Mail:
            def request(self, name, content=None):
                if name == "GetTag":
                    return {"tag": [{"name": "Tag1", "color": "1"}]}
                raise AssertionError("unexpected call %s" % name)

            def get_filter_rules(self, way="in"):
                raise AssertionError("filters must not be collected")

        class Session:
            def account_client(self, email):
                return Account()

            def mail_client(self, email):
                return Mail()

            def request_single(self, name, content=None):
                raise AssertionError("GetAccount must not be called")

        result = collect_account_settings(
            Session(), "u@example.com", parts=["tags"]
        )
        self.assertEqual(result["tags"][0]["name"], "Tag1")
        self.assertEqual(result["signatures"], [])
        self.assertEqual(result["mountpoints"], [])


class FolderBundleTests(unittest.TestCase):
    def test_bundle_uses_one_folder_request(self):
        class Client:
            def __init__(self):
                self.calls = 0

            def request(self, name, content=None):
                self.calls += 1
                return {
                    "folder": {
                        "absFolderPath": "/",
                        "id": "1",
                        "acl": {"grant": {"perm": "r", "gt": "pub"}},
                        "folder": [
                            {
                                "absFolderPath": "/Calendar",
                                "id": "10",
                                "name": "Calendar",
                                "color": "5",
                                "view": "appointment",
                            }
                        ],
                        "link": [
                            {
                                "absFolderPath": "/Shared",
                                "id": "20",
                                "name": "Shared",
                                "rid": "10",
                                "zid": "owner",
                            }
                        ],
                    }
                }

        client = Client()
        bundle = collect_folder_bundle(client, "user@example.com")
        self.assertEqual(client.calls, 1)
        self.assertEqual([f["path"] for f in bundle["all_folders"]], ["/Calendar"])
        self.assertEqual([f["path"] for f in bundle["folders"]], ["/Calendar"])
        self.assertEqual(bundle["shares_out"][0]["folderPath"], "/")
        self.assertEqual(bundle["mountpoints"][0]["path"], "/Shared")


class AccountCollectionTests(unittest.TestCase):
    def test_parallel_collection_uses_worker_sessions(self):
        entries = [{"name": "u%d@example.com" % index} for index in range(8)]
        sessions = []

        def factory():
            session = object()
            sessions.append(session)
            return session

        def fetch(session, name, with_stats=False):
            return {"name": name}

        with mock.patch.object(
            inventory_module, "_domain_entries", return_value=entries
        ), mock.patch.object(
            inventory_module, "_fetch_account", side_effect=fetch
        ):
            accounts = collect_accounts(
                object(), parallelism=4, session_factory=factory
            )

        self.assertEqual(
            [item["name"] for item in accounts],
            [item["name"] for item in entries],
        )
        self.assertGreaterEqual(len(sessions), 1)
        self.assertLessEqual(len(sessions), 4)


class SnapshotProfileTests(unittest.TestCase):
    def test_snapshots_are_tagged_and_filtered_by_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(tmp)
            profile_id = config.create_profile("Site 2")
            write_snapshot(
                config,
                "source",
                {
                    "version": 1,
                    "side": "source",
                    "host": "host-default",
                    "created_at": "2026-01-01T10:00:00",
                    "counts": {},
                },
                profile="default",
            )
            write_snapshot(
                config,
                "source",
                {
                    "version": 1,
                    "side": "source",
                    "host": "host-site2",
                    "created_at": "2026-01-02T10:00:00",
                    "counts": {},
                },
                profile=profile_id,
            )
            default_latest = latest_snapshot(config, "source", profile="default")
            site_latest = latest_snapshot(config, "source", profile=profile_id)
            self.assertEqual(default_latest["host"], "host-default")
            self.assertEqual(site_latest["host"], "host-site2")
            self.assertEqual(site_latest["profile"], profile_id)
            self.assertEqual(
                len(list_snapshots(config, "source", profile=profile_id)), 1
            )
            self.assertEqual(
                len(list_snapshots(config, "source", profile="default")), 1
            )
            index_path = snapshot_index_path(site_latest["path"])
            self.assertTrue(index_path.exists())
            index = load_snapshot_index(site_latest["path"])
            self.assertEqual(index["host"], "host-site2")
            self.assertEqual(index["accounts"], [])


class SnapshotCacheTests(unittest.TestCase):
    def test_snapshot_is_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(tmp)
            path = write_snapshot(
                config,
                "source",
                {
                    "version": 1,
                    "side": "source",
                    "host": "src",
                    "created_at": "2026-01-01T10:00:00",
                    "counts": {},
                },
                profile="default",
            )
            first = load_snapshot(path)
            second = load_snapshot(path)
            self.assertIs(first, second)


class SnapshotCleanupTests(unittest.TestCase):
    def test_cleanup_keeps_newest_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(tmp)
            snapshots = config.storage_path("snapshot_path")
            oldest_source = snapshots / "source-20260101-000001.json"
            middle_source = snapshots / "source-20260102-000001.json"
            newest_source = snapshots / "source-20260103-000001.json"
            oldest_destination = snapshots / "destination-20260101-000001.json"
            middle_destination = snapshots / "destination-20260102-000001.json"
            newest_destination = snapshots / "destination-20260103-000001.json"
            for path in (
                oldest_source,
                middle_source,
                newest_source,
                oldest_destination,
                middle_destination,
                newest_destination,
            ):
                path.write_text("{}", encoding="utf-8")

            removed = cleanup_snapshots(config, keep_per_side=1)

            self.assertEqual(
                {Path(path).name for path in removed},
                {
                    "source-20260101-000001.json",
                    "source-20260102-000001.json",
                    "destination-20260101-000001.json",
                    "destination-20260102-000001.json",
                },
            )
            self.assertTrue(newest_source.exists())
            self.assertTrue(newest_destination.exists())
            self.assertFalse(oldest_source.exists())
            self.assertFalse(middle_source.exists())
            self.assertFalse(oldest_destination.exists())
            self.assertFalse(middle_destination.exists())

if __name__ == "__main__":
    unittest.main()
