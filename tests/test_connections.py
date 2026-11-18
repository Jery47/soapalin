import ssl
import tempfile
import unittest
import urllib.error

from soaplun.config import Config
from soaplun.connections import ZimbraSession, describe_error
from soaplun.rest import RestError


class DescribeErrorTests(unittest.TestCase):
    def test_ssl_eof_is_explained(self):
        error = urllib.error.URLError(
            ssl.SSLError(
                1, "SSL: UNEXPECTED_EOF_WHILE_READING EOF occurred in violation of protocol"
            )
        )
        message = describe_error(error)
        self.assertIn("SSL EOF", message)
        self.assertIn("arrêté", message)

    def test_connection_refused_is_explained(self):
        error = urllib.error.URLError(ConnectionRefusedError(10061, "refused"))
        message = describe_error(error)
        self.assertIn("refusée", message)

    def test_rest_502_is_explained(self):
        message = describe_error(RestError("HTTP 502 on /: bad gateway", status=502))
        self.assertIn("indisponible", message)
        self.assertIn("502", message)


class StoreProxySessionTests(unittest.TestCase):
    def test_admin_uses_store_and_rest_uses_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(tmp)
            connection = {
                "store_host": "store.example.com",
                "store_admin_port": 7071,
                "proxy_host": "proxy.example.com",
                "proxy_web_port": 8443,
                "same_server": False,
                "admin_login": "admin@example.com",
                "admin_password": "secret",
            }
            session = ZimbraSession(
                config, "source", readonly=True, connection=connection
            )
            self.assertEqual(session.host, "store.example.com")
            self.assertEqual(session.proxy_host, "proxy.example.com")
            self.assertEqual(session.web_port, 8443)
            rest = session.rest()
            self.assertEqual(rest.host, "proxy.example.com")
            self.assertEqual(rest.port, 8443)

    def test_same_server_uses_store_as_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(tmp)
            connection = {
                "store_host": "single.example.com",
                "store_admin_port": 7071,
                "proxy_host": "ignored.example.com",
                "proxy_web_port": 443,
                "same_server": True,
            }
            session = ZimbraSession(
                config, "source", readonly=True, connection=connection
            )
            self.assertEqual(session.proxy_host, "single.example.com")
            self.assertEqual(session.rest().host, "single.example.com")


if __name__ == "__main__":
    unittest.main()
