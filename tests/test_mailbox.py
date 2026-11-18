import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from soaplun import mailbox as mailbox_module
from soaplun.apply import a_list, check_safety, random_password
from soaplun.config import Config
from soaplun.mailbox import (
    CONTENT_SPECS,
    MailboxError,
    backend_info,
    check_zmmailbox_environment,
    human_size,
    is_local_host,
    normalize_content_types,
    use_zmmailbox,
    verify_archive,
)


class ApplyHelperTests(unittest.TestCase):
    def test_a_list_repeats_multi_values(self):
        entries = a_list({"zimbraMailAlias": ["a@x", "b@x"], "zimbraMailQuota": "10"})
        self.assertIn({"n": "zimbraMailAlias", "_content": "a@x"}, entries)
        self.assertIn({"n": "zimbraMailAlias", "_content": "b@x"}, entries)
        self.assertIn({"n": "zimbraMailQuota", "_content": "10"}, entries)

    def test_random_password_length(self):
        password = random_password(32)
        self.assertEqual(len(password), 32)
        self.assertTrue(any(char.isdigit() for char in password))
        self.assertTrue(any(char.isalpha() for char in password))

    def test_safety_allows_same_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(tmp)
            config.connection("source")["store_host"] = "zimbra.example.com"
            config.connection("destination")["store_host"] = "zimbra.example.com"
            check_safety(config)


class MailboxTests(unittest.TestCase):
    def test_cleanup_partial_archives(self):
        import tempfile

        from soaplun.config import Config

        with tempfile.TemporaryDirectory() as tmp:
            config = Config(tmp)
            run_dir = config.storage_path("mailbox_path") / "run-test"
            run_dir.mkdir(parents=True)
            part = run_dir / "alice.tgz.part"
            part.write_bytes(b"partial")
            self.assertEqual(
                mailbox_module.cleanup_partial_archives(config), [str(part)]
            )
            self.assertFalse(part.exists())

    def test_destination_quota_is_read(self):
        class Session:
            def request_single(self, name, content=None):
                return {"a": [{"n": "zimbraMailQuota", "_content": "4096"}]}

        self.assertEqual(
            mailbox_module._destination_quota(Session(), "u@example.com"), 4096
        )

    def test_verify_valid_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mailbox.tgz"
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
                data = b"hello"
                info = tarfile.TarInfo("Inbox/1.msg")
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            path.write_bytes(buffer.getvalue())
            result = verify_archive(path, deep=True)
            self.assertTrue(result["valid"])
            self.assertEqual(result["members"], 1)
            self.assertGreater(result["size"], 0)

    def test_verify_rejects_non_gzip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.tgz"
            path.write_bytes(b"not a gzip")
            with self.assertRaises(MailboxError):
                verify_archive(path)

    def test_verify_rejects_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.tgz"
            path.write_bytes(b"")
            with self.assertRaises(MailboxError):
                verify_archive(path)

    def test_human_size(self):
        self.assertEqual(human_size(0), "0.0 o")
        self.assertEqual(human_size(1024), "1.0 Ko")
        self.assertEqual(human_size(1536), "1.5 Ko")

    def test_zmmailbox_mode_rest(self):
        self.assertFalse(use_zmmailbox({"zmmailbox_mode": "rest"}))

    def test_local_host_detection(self):
        self.assertTrue(is_local_host("localhost"))
        self.assertTrue(is_local_host("127.0.0.1"))
        self.assertFalse(is_local_host(""))
        self.assertFalse(is_local_host("host.invalid"))

    def test_backend_info_forced_rest(self):
        info = backend_info({"host": "remote.example.com", "zmmailbox_mode": "rest"})
        self.assertEqual(info["backend"], "rest")
        self.assertIn("REST", info["reason"])

    def test_windows_uses_rest_and_reports_absence(self):
        with mock.patch.object(mailbox_module.os, "name", "nt"):
            info = backend_info(
                {"host": "remote.example.com", "zmmailbox_mode": "zmmailbox"}
            )
            environment = check_zmmailbox_environment()
        self.assertEqual(info["backend"], "rest")
        self.assertIn("Linux", info["reason"])
        self.assertTrue(info["warning"])
        self.assertIn("Windows", environment["note"])

    def test_content_types_are_normalized(self):
        self.assertEqual(normalize_content_types(None), ["all"])
        self.assertEqual(normalize_content_types(["all", "mail"]), ["all"])
        self.assertEqual(
            normalize_content_types(["tasks", "mail", "contacts"]),
            ["mail", "contacts", "tasks"],
        )
        self.assertEqual(CONTENT_SPECS["mail"]["types"], "message")
        self.assertEqual(CONTENT_SPECS["calendar"]["types"], "appointment")
        self.assertEqual(CONTENT_SPECS["contacts"]["types"], "contact")
        self.assertEqual(CONTENT_SPECS["tasks"]["types"], "task")


class DuplicateFilterTests(unittest.TestCase):
    @staticmethod
    def _tgz(path, entries):
        with tarfile.open(path, "w:gz") as archive:
            for name, data in entries.items():
                if isinstance(data, str):
                    data = data.encode("utf-8")
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))

    class _Client:
        def __init__(self, existing=None, appointments=None, contacts=None):
            self.existing = {value.lower() for value in (existing or set())}
            self.items = []
            self.appointments = appointments or []
            self.contacts = contacts or []

        def request(self, name, params):
            self.assertions = name
            if name == "GetFolder":
                return {
                    "folder": {
                        "absFolderPath": "/",
                        "id": "1",
                        "folder": [
                            {
                                "absFolderPath": "/Inbox",
                                "id": "2",
                                "n": "2",
                                "view": "message",
                            }
                        ],
                    }
                }
            query = params["query"]["_content"]
            types = params["types"]
            if types == "message":
                if "msgid:" not in query:
                    if self.items:
                        return {"m": self.items, "more": "0"}
                    return {"m": [{"id": "0"}] if self.existing else []}
                ids = [
                    value.lower()
                    for value in __import__("re").findall(r'msgid:"([^"]+)"', query)
                ]
                hits = [value for value in ids if value in self.existing]
                if params.get("fetch") == "all":
                    return {
                        "m": [
                            {"mid": {"_content": "<%s>" % value}}
                            for value in hits
                        ]
                    }
                return {"m": [{"id": str(index)} for index, _ in enumerate(hits)]}
            if types == "appointment":
                return {
                    "appt": [{"uid": value} for value in self.appointments],
                    "more": "0",
                }
            if types == "contact":
                return {
                    "cn": [
                        {"a": [{"n": "email", "_content": value}]}
                        for value in self.contacts
                    ],
                    "more": "0",
                }
            if types == "task":
                return {"task": [], "more": "0"}
            return {"m": [], "more": "0"}

    class _Session:
        def __init__(self, client):
            self.client = client

        def mail_client(self, email):
            return self.client

    def test_collects_message_calendar_and_contact_keys(self):
        uid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "all.tgz"
            self._tgz(
                path,
                {
                    "Inbox/1.eml": "Message-ID: <mail-one@example.com>\n\nbody",
                    "Calendar/1.appt.meta": json.dumps(
                        {"metadata": "d1:u36:%se" % uid}
                    ),
                    "Emails/jane.meta": json.dumps(
                        {
                            "metadata": "d5:email16:jane@example.com"
                            "8:fullName4:Janee"
                        }
                    ),
                },
            )
            keys, kinds = mailbox_module._collect_archive_keys(path)
            values = set().union(*keys.values())
        self.assertEqual(kinds, {"mail", "calendar", "contacts"})
        self.assertIn("mail:mail-one@example.com", values)
        self.assertIn("uid:" + uid, values)
        self.assertIn("email:jane@example.com", values)
        self.assertIn("name:jane", values)

    def test_filter_archive_removes_message_and_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.tgz"
            target = Path(tmp) / "filtered.tgz"
            self._tgz(
                source,
                {
                    "Inbox/1.eml.meta": "{}",
                    "Inbox/1.eml": "Message-ID: <one@example.com>\n\nbody",
                    "Inbox/2.eml.meta": "{}",
                    "Inbox/2.eml": "Message-ID: <two@example.com>\n\nbody",
                },
            )
            _path, remaining = mailbox_module._filter_archive(
                source, target, {"Inbox/1"}
            )
            verify_archive(target)
            with tarfile.open(target, "r:gz") as archive:
                names = archive.getnames()
        self.assertEqual(remaining, 1)
        self.assertNotIn("Inbox/1.eml", names)
        self.assertNotIn("Inbox/1.eml.meta", names)
        self.assertIn("Inbox/2.eml", names)

    def test_destination_mail_keys_uses_batches(self):
        client = self._Client(existing={"one@example.com"})
        found = mailbox_module._destination_mail_keys(
            client, {"mail:one@example.com", "mail:two@example.com"}
        )
        self.assertEqual(found, {"mail:one@example.com"})

    def test_deduplicate_archive_skips_all_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mail.tgz"
            self._tgz(
                path,
                {
                    "Inbox/1.eml": "Message-ID: <one@example.com>\n\nbody",
                },
            )
            info = mailbox_module.deduplicate_archive(
                self._Session(self._Client(existing={"one@example.com"})),
                "dest@example.com",
                path,
                content="mail",
            )
            self.assertTrue(info["all_duplicates"])
            self.assertEqual(info["duplicates"], 1)
            self.assertTrue(Path(info["archive"]).exists())

    def test_deduplicate_archive_structured_keys(self):
        uid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "structured.tgz"
            self._tgz(
                path,
                {
                    "Calendar/1.appt.meta": json.dumps(
                        {"metadata": "d1:u36:%se" % uid}
                    ),
                    "Emails/jane.meta": json.dumps(
                        {"metadata": "d5:email16:jane@example.come"}
                    ),
                },
            )
            info = mailbox_module.deduplicate_archive(
                self._Session(
                    self._Client(
                        appointments=[uid],
                        contacts=["JANE@example.com"],
                    )
                ),
                "dest@example.com",
                path,
            )
        self.assertTrue(info["all_duplicates"])
        self.assertEqual(info["duplicates"], 2)


class IncrementalHelpersTests(unittest.TestCase):
    class _Client:
        def __init__(self, items):
            self.items = items

        def request(self, name, params):
            if name == "GetFolder":
                return {
                    "folder": {
                        "absFolderPath": "/",
                        "id": "1",
                        "folder": [
                            {
                                "absFolderPath": "/Inbox",
                                "id": "2",
                                "n": "2",
                                "view": "message",
                            }
                        ],
                    }
                }
            types = params["types"]
            if types == "appointment":
                return {"appt": self.items, "more": "0"}
            if types == "contact":
                return {"cn": self.items, "more": "0"}
            if types == "task":
                return {"task": self.items, "more": "0"}
            return {"m": self.items, "more": "0"}

    def test_missing_entry_ids_keeps_unknown_and_missing(self):
        entries = [
            {"id": "1", "keys": {"uid:a"}},
            {"id": "2", "keys": {"uid:b"}},
            {"id": "3", "keys": set()},
            {"id": "", "keys": {"uid:c"}},
        ]
        missing = mailbox_module._missing_entry_ids(entries, {"uid:a"})
        self.assertEqual(missing, ["2", "3"])

    def test_source_structured_entries_include_ids(self):
        client = self._Client(
            [
                {"id": "10", "uid": "uid-one"},
                {"id": "11", "uid": "uid-two"},
            ]
        )
        entries = mailbox_module._source_structured_entries(client, "calendar")
        self.assertEqual(
            entries,
            [
                {"id": "10", "size": 0, "keys": {"uid:uid-one"}},
                {"id": "11", "size": 0, "keys": {"uid:uid-two"}},
            ],
        )

    def test_incremental_content_skips_when_all_present(self):
        client = self._Client([{"id": "10", "uid": "uid-one"}])
        session = type("Session", (), {"mail_client": lambda self, email: client})()
        with tempfile.TemporaryDirectory() as tmp:
            record = mailbox_module._incremental_content_record(
                None,
                session,
                session,
                client,
                client,
                "source@example.com",
                "dest@example.com",
                "calendar",
                "Agenda",
                Path(tmp),
                [],
                None,
                1,
                1,
            )
        self.assertEqual(record["status"], "skipped")
        self.assertEqual(record["mode"], "incremental")

    def test_incremental_content_exports_only_missing_chunks(self):
        source_client = self._Client(
            [
                {"id": "10", "uid": "uid-one"},
                {"id": "11", "uid": "uid-two"},
            ]
        )
        destination_client = self._Client([{"id": "99", "uid": "uid-one"}])
        session = type("Session", (), {"mail_client": lambda self, email: source_client})()

        def fake_export(*args, **kwargs):
            return {"empty": False, "bytes": 10, "seconds": 0.1, "backend": "rest"}

        def fake_import(*args, **kwargs):
            return {"seconds": 0.2}

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            mailbox_module, "export_mailbox", side_effect=fake_export
        ), mock.patch.object(
            mailbox_module, "import_mailbox", side_effect=fake_import
        ):
            record = mailbox_module._incremental_content_record(
                None,
                session,
                session,
                source_client,
                destination_client,
                "source@example.com",
                "dest@example.com",
                "calendar",
                "Agenda",
                Path(tmp),
                [],
                None,
                1,
                1,
            )
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["items"], 1)
        self.assertEqual(record["skipped_duplicates"], 1)
        self.assertEqual(len(record["chunks"]), 1)


class ChunkTests(unittest.TestCase):
    def test_chunk_entries_respects_size_and_item_limits(self):
        entries = [
            {"id": "1", "size": 600},
            {"id": "2", "size": 600},
            {"id": "3", "size": 100},
        ]
        chunks = mailbox_module._chunk_entries(entries, max_bytes=1000, max_items=50)
        self.assertEqual([len(chunk) for chunk in chunks], [1, 2])
        self.assertEqual([chunk[0]["id"] for chunk in chunks], ["1", "2"])

    def test_chunked_content_exports_all_source_items(self):
        client = IncrementalHelpersTests._Client(
            [{"id": "10", "s": "100"}, {"id": "11", "s": "200"}]
        )
        session = type("Session", (), {"mail_client": lambda self, email: client})()

        def fake_export(*args, **kwargs):
            return {"empty": False, "bytes": 10, "seconds": 0.1, "backend": "rest"}

        def fake_import(*args, **kwargs):
            return {"seconds": 0.2}

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            mailbox_module, "export_mailbox", side_effect=fake_export
        ), mock.patch.object(
            mailbox_module, "import_mailbox", side_effect=fake_import
        ):
            record = mailbox_module._incremental_content_record(
                None,
                session,
                session,
                client,
                None,
                "source@example.com",
                "dest@example.com",
                "mail",
                "Mails",
                Path(tmp),
                [],
                None,
                1,
                1,
                compare_destination=False,
            )
        self.assertEqual(record["mode"], "chunked")
        self.assertEqual(record["items"], 2)
        self.assertEqual(record["skipped_duplicates"], 0)
        self.assertEqual(len(record["chunks"]), 1)


class ParallelismTests(unittest.TestCase):
    def test_run_account_tasks_preserves_plan_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(tmp)
            selected = [
                {"source": "u%d@example.com" % index, "destination": "u%d@example.com" % index}
                for index in range(5)
            ]
            seen = []

            def worker(
                config,
                item,
                source,
                destination,
                contents,
                run_dir,
                keep,
                progress,
                cancel,
                index,
                total,
                compare,
                since="",
            ):
                seen.append(index)
                return {
                    "source": item["source"],
                    "destination": item["destination"],
                    "status": "ok",
                }

            results = mailbox_module._run_account_tasks(
                config,
                selected,
                len(selected),
                None,
                None,
                3,
                worker,
                Path(tmp),
                True,
                [],
                False,
            )
        self.assertEqual(len(seen), 5)
        self.assertEqual(
            [record["source"] for record in results],
            [item["source"] for item in selected],
        )


    def test_since_query_converts_iso_date(self):
        from soaplun.mailbox import since_query
        self.assertEqual(since_query("2026-11-01"), "date:>=11/01/2026")
        self.assertEqual(since_query(""), "")
        with self.assertRaises(Exception):
            since_query("01-11-2026")

class SyncTagsTests(unittest.TestCase):
    def test_sync_tags_matches_messages_by_message_id(self):
        calls = []

        class Mail:
            def __init__(self, role):
                self.role = role

            def request(self, name, content=None):
                content = content or {}
                if name == "GetTag":
                    return {"tag": [{"id": "1" if self.role == "source" else "2", "name": "tag1", "color": "9"}]}
                if name == "Search":
                    if content.get("types") != "message":
                        return {}
                    if self.role == "source":
                        return {"m": [{"id": "s1", "mid": {"_content": "<m1>"}, "tn": "tag1"}]}
                    return {"m": [{"id": "d1", "mid": {"_content": "<m1>"}}]}
                if name == "ItemAction":
                    calls.append(content)
                    return {}
                return {}

        class Session:
            def __init__(self, config, role, readonly=True):
                self.role = role

            def mail_client(self, email):
                return Mail(self.role)

        with mock.patch.object(mailbox_module, "ZimbraSession", Session):
            report = mailbox_module.sync_tags(
                mock.Mock(),
                {"accounts": [{"source": "u@example.com", "destination": "u@example.com"}]},
                only=["u@example.com"],
                contents=["mail"],
                dry_run=False,
            )
        self.assertEqual(report["summary"]["updated"], 1)
        self.assertEqual(calls[0]["action"]["id"], "d1")
        self.assertEqual(calls[0]["action"]["tn"], "tag1")


if __name__ == "__main__":
    unittest.main()
