"""Integration tests using a fake SOAP session and a fake HTTPS REST server."""

import datetime
import ipaddress
import ssl
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
except ImportError as exc:  # pragma: no cover - depends on the test env
    raise unittest.SkipTest("cryptography is required for integration tests") from exc
from soaplun import apply as apply_module
from soaplun import rest as rest_module
from soaplun.config import Config
from soaplun.connections import ReadOnlyViolation, guard_read_only, is_read_only_request
from soaplun.rest import RestClient


class FakeZimbraSession:
    calls = []

    def __init__(self, config, side, readonly=False):
        self.config = config
        self.side = side
        self.readonly = readonly

    def request(self, name, content=None):
        FakeZimbraSession.calls.append((name, content))
        return {"authToken": "token", "lifetime": 3600}

    def request_single(self, name, content=None):
        FakeZimbraSession.calls.append((name, content))
        identifiers = {
            "GetAccount": ("account", "account-1"),
            "GetDomain": ("domain", "domain-1"),
            "GetCos": ("cos", "cos-1"),
            "GetDistributionList": ("dl", "dl-1"),
            "GetCalendarResource": ("calresource", "resource-1"),
        }
        key, identifier = identifiers.get(name, ("item", "item-1"))
        return {key: {"id": identifier}}


class ApplyPlanIntegrationTests(unittest.TestCase):
    def setUp(self):
        FakeZimbraSession.calls = []
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(self.tmp.name)
        self.config.connection("source")["store_host"] = "src.example.com"
        self.config.connection("destination")["store_host"] = "dst.example.com"

    def tearDown(self):
        self.tmp.cleanup()

    def test_plan_is_applied_end_to_end(self):
        plan = {
            "validation": {"ok": True, "errors": [], "warnings": []},
            "source": {"host": "src.example.com"},
            "destination": {"host": "dst.example.com"},
            "options": {},
            "actions": [
                {
                    "id": "create_domain:new.fr",
                    "kind": "create_domain",
                    "target": "new.fr",
                    "payload": {"name": "new.fr", "attrs": {}},
                },
                {
                    "id": "create_cos:standard",
                    "kind": "create_cos",
                    "target": "standard",
                    "payload": {"name": "standard", "attrs": {}},
                },
                {
                    "id": "create_account:a@new.fr",
                    "kind": "create_account",
                    "target": "a@new.fr",
                    "payload": {
                        "name": "a@new.fr",
                        "attrs": {},
                        "password_source": "fixed",
                    },
                },
                {
                    "id": "add_alias:alias@new.fr",
                    "kind": "add_alias",
                    "target": "alias@new.fr",
                    "payload": {
                        "account": "a@new.fr",
                        "alias": "alias@new.fr",
                    },
                },
                {
                    "id": "create_dl:list@new.fr",
                    "kind": "create_dl",
                    "target": "list@new.fr",
                    "payload": {
                        "name": "list@new.fr",
                        "attrs": {},
                        "members": [],
                    },
                },
                {
                    "id": "create_resource:room@new.fr",
                    "kind": "create_resource",
                    "target": "room@new.fr",
                    "payload": {
                        "name": "room@new.fr",
                        "attrs": {},
                        "password_source": "fixed",
                    },
                },
            ],
        }
        with mock.patch.object(
            apply_module, "ZimbraSession", FakeZimbraSession
        ):
            report = apply_module.apply_plan(
                self.config,
                plan,
                options={"fixed_password": "Secret!"},
            )

        self.assertEqual(report["summary"]["ok"], 6)
        self.assertTrue(Path(report["report_path"]).exists())
        called = [name for name, _ in FakeZimbraSession.calls]
        for command in (
            "CreateDomain",
            "CreateCos",
            "CreateAccount",
            "AddAccountAlias",
            "CreateDistributionList",
            "CreateCalendarResource",
        ):
            self.assertIn(command, called)

    def test_read_only_guard_rejects_mutations(self):
        class Client:
            def __init__(self):
                self.calls = []

            def request(self, name, content=None, namespace=None):
                self.calls.append(name)
                return {}

        client = Client()
        guard_read_only(client)
        self.assertTrue(is_read_only_request("GetAccount"))
        client.request("GetAccount", {})
        with self.assertRaises(ReadOnlyViolation):
            client.request("CreateAccount", {"name": "a@new.fr"})


class FakeRestHandler(BaseHTTPRequestHandler):
    payload = b"fake-tgz-payload"
    uploaded = b""
    fail_once = False
    lock = threading.Lock()

    def log_message(self, *args):
        return

    def do_GET(self):
        with FakeRestHandler.lock:
            should_fail = FakeRestHandler.fail_once
            FakeRestHandler.fail_once = False
        if should_fail:
            self.send_response(503)
            self.end_headers()
            self.wfile.write(b"retry")
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.payload)))
        self.end_headers()
        self.wfile.write(self.payload)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        FakeRestHandler.uploaded = self.rfile.read(length)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")


def _write_self_signed_cert(directory: Path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]
    )
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "cert.pem"
    key_path = directory / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


class RestClientIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cert_path, key_path = _write_self_signed_cert(Path(cls.tmp.name))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeRestHandler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert_path), str(key_path))
        cls.server.socket = context.wrap_socket(
            cls.server.socket, server_side=True
        )
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(
            target=cls.server.serve_forever, daemon=True
        )
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.tmp.cleanup()

    def setUp(self):
        FakeRestHandler.fail_once = False
        FakeRestHandler.uploaded = b""
        self.client = RestClient(
            "127.0.0.1", self.port, verify_ssl=False, timeout=10
        )

    def test_download_and_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "mailbox.tgz"
            written = self.client.download(
                self.client.base_url("/home/a@example.com/?fmt=tgz"),
                destination,
            )
            self.assertEqual(written, len(FakeRestHandler.payload))
            self.assertEqual(
                destination.read_bytes(), FakeRestHandler.payload
            )

            source = Path(tmp) / "upload.tgz"
            source.write_bytes(b"upload-payload")
            status, body = self.client.upload(
                self.client.base_url("/home/a@example.com/?fmt=tgz"),
                source,
            )
            self.assertEqual(status, 200)
            self.assertEqual(body, "ok")
            self.assertEqual(FakeRestHandler.uploaded, b"upload-payload")

    def test_download_retries_transient_http_errors(self):
        FakeRestHandler.fail_once = True
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            rest_module, "REST_RETRY_DELAY", 0
        ):
            destination = Path(tmp) / "retry.tgz"
            written = self.client.download(
                self.client.base_url("/retry"), destination
            )
        self.assertEqual(written, len(FakeRestHandler.payload))


if __name__ == "__main__":
    unittest.main()
