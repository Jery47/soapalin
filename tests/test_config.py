import json
import tempfile
import unittest
from pathlib import Path

from soapalin.config import (
    Config,
    clean_host,
    deep_merge,
    proxy_host,
    proxy_web_port,
    store_admin_port,
    store_host,
)
from soapalin.connections import _merge_test_overrides


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults_and_mapping(self):
        self.assertEqual(self.config.connection("source")["store_admin_port"], 7071)
        self.assertEqual(self.config.map_domain("example.com"), "example.com")
        self.config.sync()["domain_map"] = {"old.fr": "new.fr"}
        self.assertEqual(self.config.map_email("user@old.fr"), "user@new.fr")
        self.assertEqual(self.config.map_email("user@other.fr"), "user@other.fr")

    def test_host_mapping(self):
        sync = self.config.sync()
        sync["mail_host_map"] = {"old-host": "new-host"}
        sync["default_mail_host"] = "fallback-host"
        self.assertEqual(self.config.map_host("old-host"), "new-host")
        self.assertEqual(self.config.map_host("unknown"), "fallback-host")

    def test_secret_roundtrip(self):
        self.config.connection("source")["admin_password"] = "s3cr3t-é"
        self.config.sync()["fixed_password"] = "fixed-pass"
        self.config.save()

        raw = json.loads(Path(self.config.config_path).read_text(encoding="utf-8"))
        self.assertNotEqual(raw["profiles"]["default"]["source"]["admin_password"], "s3cr3t-é")
        self.assertNotEqual(raw["sync"]["fixed_password"], "fixed-pass")

        reloaded = Config(self.tmp.name)
        self.assertEqual(reloaded.connection("source")["admin_password"], "s3cr3t-é")
        self.assertEqual(reloaded.sync()["fixed_password"], "fixed-pass")

    def test_legacy_config_is_migrated_to_default_profile(self):
        import json
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            config_dir = Path(tmp) / "config"
            config_dir.mkdir()
            legacy = {
                "schema": 1,
                "source": {
                    "host": "src.example.com",
                    "admin_login": "admin@src",
                    "admin_port": 7071,
                },
                "destination": {
                    "host": "dst.example.com",
                    "admin_login": "admin@dst",
                    "admin_port": 7071,
                },
                "sync": {},
                "storage": {},
                "web": {"secret_key": "x"},
            }
            (config_dir / "config.json").write_text(
                json.dumps(legacy), encoding="utf-8"
            )
            migrated = Config(tmp)
            self.assertEqual(migrated.default_profile(), "default")
            self.assertEqual(
                migrated.connection("source")["store_host"], "src.example.com"
            )
            self.assertEqual(
                migrated.connection("destination")["store_host"], "dst.example.com"
            )
            self.assertTrue(migrated.connection("source")["same_server"])
            self.assertEqual(
                proxy_host(migrated.connection("source")), "src.example.com"
            )
            self.assertEqual(
                store_admin_port(migrated.connection("source")), 7071
            )
            self.assertEqual(
                proxy_web_port(migrated.connection("source")), 443
            )
            raw = json.loads(
                (config_dir / "config.json").read_text(encoding="utf-8")
            )
            self.assertNotIn("source", raw)
            self.assertIn("default", raw["profiles"])

    def test_profiles_crud_and_thread_local_context(self):
        profile_id = self.config.create_profile("Site 2")
        self.assertIn(profile_id, self.config.profiles())
        self.config.connection("source", profile=profile_id)["store_host"] = "site2"
        self.config.save()
        self.assertEqual(
            self.config.connection("source", profile=profile_id)["store_host"], "site2"
        )

        self.config.deactivate_profile()
        with self.config.profile_context(profile_id):
            self.assertEqual(self.config.current_profile(), profile_id)
            self.assertEqual(
                self.config.connection("source")["store_host"], "site2"
            )
        self.assertEqual(self.config.current_profile(), "default")

        self.config.rename_profile(profile_id, "Site Lyon")
        self.assertEqual(self.config.profile_name(profile_id), "Site Lyon")
        duplicate = self.config.duplicate_profile(profile_id)
        self.assertEqual(
            self.config.connection("source", profile=duplicate)["store_host"], "site2"
        )
        self.config.delete_profile(duplicate)
        self.assertNotIn(duplicate, self.config.profiles())

    def test_default_profile_cannot_be_deleted(self):
        with self.assertRaises(ValueError):
            self.config.delete_profile(self.config.default_profile())

    def test_store_and_proxy_can_differ(self):
        self.config.apply_connection_form(
            "source",
            {
                "same_server_present": "1",
                "store_host": "zmailstore.example.com",
                "store_admin_port": "7071",
                "proxy_host": "zproxy.example.com",
                "proxy_web_port": "8443",
                "admin_login": "admin@example.com",
            },
        )
        connection = self.config.connection("source")
        self.assertFalse(connection["same_server"])
        self.assertEqual(store_host(connection), "zmailstore.example.com")
        self.assertEqual(proxy_host(connection), "zproxy.example.com")
        self.assertEqual(store_admin_port(connection), 7071)
        self.assertEqual(proxy_web_port(connection), 8443)

        self.config.apply_connection_form(
            "source",
            {
                "same_server_present": "1",
                "same_server": "1",
                "store_host": "single.example.com",
                "store_admin_port": "7071",
                "proxy_web_port": "443",
                "admin_login": "admin@example.com",
            },
        )
        connection = self.config.connection("source")
        self.assertTrue(connection["same_server"])
        self.assertEqual(store_host(connection), "single.example.com")
        self.assertEqual(proxy_host(connection), "single.example.com")
        self.assertEqual(proxy_web_port(connection), 443)

    def test_connection_form_keeps_password(self):
        self.config.connection("source")["admin_password"] = "keep-me"
        self.config.apply_connection_form(
            "source", {"host": "zimbra.example.com", "admin_password": ""}
        )
        self.assertEqual(self.config.connection("source")["admin_password"], "keep-me")
        self.config.apply_connection_form(
            "source", {"host": "zimbra.example.com", "admin_password": ""}, keep_password=False
        )
        self.assertEqual(self.config.connection("source")["admin_password"], "")

    def test_ignored_attributes(self):
        self.assertTrue(self.config.ignored_attr("zimbraId"))
        self.assertTrue(self.config.ignored_attr("zimbraTwoFactorAuthEnabled"))
        self.assertTrue(self.config.ignored_attr("zimbraAuthTokens"))
        self.assertTrue(self.config.ignored_attr("zimbraAuthTokenValidityValue"))
        self.assertFalse(self.config.ignored_attr("zimbraMailQuota"))
        self.assertTrue(self.config.ignored_attr("zimbraCreateTimestamp", kind="domain"))

    def test_deep_merge(self):
        base = {"a": {"b": 1, "c": 2}, "d": 3}
        override = {"a": {"b": 9}}
        result = deep_merge(base, override)
        self.assertEqual(result, {"a": {"b": 9, "c": 2}, "d": 3})
        self.assertEqual(base["a"]["b"], 1)

    def test_connection_form_cleans_pasted_url(self):
        self.config.apply_connection_form(
            "source", {"host": " https://192.0.2.10:7071/zimbra/ ", "admin_login": "admin@x.fr"}
        )
        self.assertEqual(self.config.connection("source")["store_host"], "192.0.2.10")
        self.config.apply_connection_form("source", {"host": "zimbra.example.com:443"})
        self.assertEqual(self.config.connection("source")["store_host"], "zimbra.example.com")


class CleanHostTests(unittest.TestCase):
    def test_variants(self):
        cases = {
            "zimbra.example.com": "zimbra.example.com",
            "  Zimbra.Example.com  ": "Zimbra.Example.com",
            "https://zimbra.example.com/": "zimbra.example.com",
            "http://zimbra.example.com:7071/service": "zimbra.example.com",
            "zimbra.example.com:7071": "zimbra.example.com",
            "": "",
            None: "",
            "[::1]:7071": "::1",
            "192.0.2.10": "192.0.2.10",
        }
        for raw, expected in cases.items():
            self.assertEqual(clean_host(raw), expected, raw)


class ConnectionOverrideTests(unittest.TestCase):
    def test_unsaved_values_do_not_touch_config(self):
        tmp = tempfile.TemporaryDirectory()
        config = Config(tmp.name)
        config.connection("source")["admin_password"] = "saved-pass"
        merged = _merge_test_overrides(
            config.connection("source"),
            {
                "host": "https://192.0.2.10/",
                "admin_port": "7071",
                "admin_login": "admin@example.com",
                "admin_password": "",
                "verify_ssl": "1",
            },
        )
        self.assertEqual(merged["store_host"], "192.0.2.10")
        self.assertEqual(merged["store_admin_port"], 7071)
        self.assertTrue(merged["verify_ssl"])
        self.assertEqual(merged["admin_password"], "saved-pass")
        self.assertEqual(config.connection("source")["store_host"], "")
        tmp.cleanup()

class ServerRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_server_registry_crud_and_runtime_profiles(self):
        self.config.create_server("src1", "source")
        self.config.create_server("dst1", "destination")
        self.assertEqual(self.config.server("src1")["type"], "source")
        self.assertEqual(self.config.server("dst1")["type"], "destination")
        with self.assertRaises(ValueError):
            self.config.create_server("src1", "source")
        with self.assertRaises(ValueError):
            self.config.create_server("bad", "other")

        connection = self.config.server("src1")["connection"]
        connection["store_host"] = "src.example.com"
        connection["admin_password"] = "secret"
        self.config.save_server("src1", connection)
        self.assertEqual(
            self.config.server("src1")["connection"]["admin_password"], "secret"
        )

        self.config.use_servers("src1", "dst1")
        self.assertEqual(self.config.current_profile(), "__migration__")
        self.assertEqual(
            self.config.connection("source")["store_host"], "src.example.com"
        )
        self.assertEqual(
            self.config.connection("destination")["store_host"], ""
        )
        self.config.save()
        reloaded = Config(self.tmp.name)
        self.assertNotIn("__migration__", reloaded.profiles())

        with self.assertRaises(ValueError):
            self.config.use_servers("dst1", "src1")
        self.config.rename_server("dst1", "dst2")
        self.assertNotIn("dst1", self.config.servers())
        self.assertIn("dst2", self.config.servers())
        self.config.remove_server("dst2")
        self.assertNotIn("dst2", self.config.servers())


if __name__ == "__main__":
    unittest.main()
