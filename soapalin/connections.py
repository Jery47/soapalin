"""Zimbra SOAP sessions, read-only protection and connection tests."""

import logging
import ssl
import threading
import time
import urllib.error
import urllib.parse
from typing import Any, Dict, Iterable, Optional, Tuple

from zimsoap import zobjects
from zimsoap.client import (
    ZimbraAccountClient,
    ZimbraAdminClient,
    ZimbraMailClient,
    ZimbraSoapServerError,
    ZimbraSoapUnexpectedResponse,
)

from . import config as config_module
from .rest import RestClient, RestError, build_ssl_context

logger = logging.getLogger(__name__)


class ReadOnlyViolation(Exception):
    """Raised when a mutating SOAP command is attempted on the source."""


# Commands that are explicitly safe on a read-only connection.  Zimbra's admin
# API names are quite regular: read commands start with Get/Search/Count/Check.
READ_ONLY_EXACT = {
    "Auth",
    "DelegateAuth",
    "NoOp",
    "FreeBusy",
    "CheckAuthToken",
    "CheckLicense",
    "DiscoverRights",
    "GetVersionInfo",
    "GetServiceStatus",
    "GetLoggerStats",
    "GetMailboxStats",
    "GetQuotaUsage",
    "GetAllConfig",
    "GetConfig",
    "GetAllDomains",
    "GetAllAccounts",
    "GetAllAdminAccounts",
    "GetAllCalendarResources",
    "GetAllCos",
    "GetAllCOS",
    "GetAllDistributionLists",
    "GetAllServers",
    "GetAllVolumes",
    "GetAllMailboxes",
    "GetAllZimlets",
    "SearchDirectory",
    "SearchGal",
    "SearchCalendarResources",
    "SyncGal",
    "GetAccount",
    "GetAccountInfo",
    "GetAccountMembership",
    "GetDomain",
    "GetCos",
    "GetCOS",
    "GetServer",
    "GetDistributionList",
    "GetDistributionListMembers",
    "GetCalendarResource",
    "GetMailbox",
    "GetMailboxStats",
    "GetShareInfo",
    "GetFolder",
    "GetMountpoint",
    "GetPermission",
    "GetPrefs",
    "GetIdentities",
    "GetSignatures",
    "GetFilterRules",
    "GetOutgoingFilterRules",
    "GetWhiteBlackList",
    "GetDataSources",
    "GetRights",
    "GetEffectiveRights",
    "GetLDAPEntries",
    "GetCert",
    "GetCSR",
    "GetMailboxSize",
}

READ_ONLY_PREFIXES = (
    "Get",
    "Search",
    "Count",
    "Check",
    "Browse",
    "Sync",
    "Discover",
    "FreeBusy",
    "NoOp",
    "Auth",
)

MUTATING_PREFIXES = (
    "Add",
    "Apply",
    "Create",
    "Delete",
    "Deploy",
    "Grant",
    "Import",
    "Install",
    "Invalidate",
    "Lock",
    "Migrate",
    "Modify",
    "Move",
    "Provision",
    "Purge",
    "Put",
    "Rename",
    "Reset",
    "Restore",
    "Revoke",
    "Save",
    "Send",
    "Set",
    "Start",
    "Stop",
    "Unlock",
    "Update",
)


def is_read_only_request(name: str) -> bool:
    """Return True when the admin/mail SOAP command only reads data."""
    if name in READ_ONLY_EXACT:
        return True
    if name.startswith(MUTATING_PREFIXES):
        return False
    return name.startswith(READ_ONLY_PREFIXES)


def guard_read_only(client) -> Any:
    """Wrap ``client.request`` so that any mutating command is rejected.

    The wrapper is installed on the *instance* because
    :class:`zimsoap.client.ZimbraAPISession` calls ``self.client.request``.
    """
    original = client.request

    def guarded(name, content=None, namespace=None):
        if not is_read_only_request(name):
            raise ReadOnlyViolation(
                "Refusing to run %r on the source server: source is read-only"
                % name
            )
        return original(name, content, namespace)

    client.request = guarded
    return client


def describe_error(exc: BaseException) -> str:
    """Turn library/builtin exceptions into a short human-readable string."""
    if isinstance(exc, ZimbraSoapServerError):
        return "%s: %s" % (exc.code or "SOAP error", exc.msg)
    if isinstance(exc, ZimbraSoapUnexpectedResponse):
        return "Unexpected SOAP response: %s" % exc
    if isinstance(exc, ReadOnlyViolation):
        return str(exc)
    if isinstance(exc, RestError):
        text = str(exc)
        lowered = text.lower()
        if "502" in text or "503" in text:
            return (
                "Service Zimbra indisponible (HTTP 5xx via nginx) : "
                "le service de la destination est probablement arrêté. %s" % text
            )
        if "unexpected_eof" in lowered or "ssl" in lowered:
            return (
                "Connexion REST interrompue (TLS/SSL) : le service web Zimbra "
                "est probablement arrêté ou redémarre. %s" % text
            )
        return "REST: %s" % text
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", exc)
        text = str(reason)
        lowered = text.lower()
        if "unexpected_eof" in lowered or "eof occurred" in lowered:
            return (
                "Connexion TLS interrompue (SSL EOF) : le service Zimbra visé est "
                "probablement arrêté ou redémarre (%s)" % text
            )
        if "refused" in lowered or "10061" in lowered or "actively refused" in lowered:
            return (
                "Connexion refusée : le port SOAP/admin Zimbra est fermé ou le "
                "service est arrêté (%s)" % text
            )
        return "Erreur réseau : %s" % text
    if isinstance(exc, ssl.SSLError):
        return "TLS error: %s (check verify_ssl / CA bundle)" % exc
    return "%s: %s" % (type(exc).__name__, exc)


def _ssl_context(connection: Dict[str, Any]) -> ssl.SSLContext:
    return build_ssl_context(
        bool(connection.get("verify_ssl")),
        connection.get("ca_bundle") or "",
    )


def _new_admin_client(connection: Dict[str, Any]) -> ZimbraAdminClient:
    client = ZimbraAdminClient(
        config_module.store_host(connection),
        config_module.store_admin_port(connection),
    )
    client.com.context = _ssl_context(connection)
    client.com.timeout = int(connection.get("read_timeout") or 300)
    return client


class ZimbraSession:
    """Authenticated session against one Zimbra server.

    A session lazily authenticates its admin client and can create delegated
    account/mail clients.  When ``readonly`` is true every SOAP request is
    checked against :func:`is_read_only_request`.
    """

    def __init__(
        self,
        config: config_module.Config,
        side: str,
        readonly: bool = False,
        connection: Optional[Dict[str, Any]] = None,
    ):
        self.config = config
        self.side = side
        self.readonly = readonly
        # ``connection`` lets callers test unsaved values without touching
        # the stored configuration.
        self.connection = dict(connection) if connection is not None else config.connection(side)
        self._admin: Optional[ZimbraAdminClient] = None
        self._lock = threading.RLock()
        self._last_login = 0.0

    # ------------------------------------------------------------------
    @property
    def host(self) -> str:
        return config_module.store_host(self.connection)

    @property
    def proxy_host(self) -> str:
        return config_module.proxy_host(self.connection)

    @property
    def web_port(self) -> int:
        return config_module.proxy_web_port(self.connection)

    def _check_configured(self) -> None:
        label = "la source" if self.side == "source" else "la destination"
        if not config_module.store_host(self.connection):
            raise ValueError("Hôte STORE non configuré pour %s (enregistrez la connexion)." % label)
        if not bool(self.connection.get("same_server", True)) and not self.connection.get("proxy_host"):
            raise ValueError("Hôte PROXY non configuré pour %s (enregistrez la connexion)." % label)
        if not self.connection.get("admin_login"):
            raise ValueError("Compte administrateur non configuré pour %s." % label)
        if not self.connection.get("admin_password"):
            raise ValueError("Mot de passe administrateur non configuré pour %s." % label)

    # ------------------------------------------------------------------
    @property
    def admin(self) -> ZimbraAdminClient:
        with self._lock:
            if self._admin is None:
                self._check_configured()
                client = _new_admin_client(self.connection)
                client.com.timeout = int(self.connection.get("read_timeout") or 300)
                if self.readonly:
                    guard_read_only(client)
                try:
                    client.login(
                        self.connection["admin_login"],
                        self.connection["admin_password"],
                    )
                except Exception as exc:  # noqa: BLE001 - normalized upstream
                    raise ConnectionError(
                        "Authentication failed on %s (%s): %s"
                        % (self.side, self.host, describe_error(exc))
                    ) from exc
                self._admin = client
                self._last_login = time.time()
            return self._admin

    def relogin(self) -> None:
        """Drop the cached admin client so the next access re-authenticates."""
        with self._lock:
            self._admin = None

    def request(self, name: str, content: Optional[Dict[str, Any]] = None):
        """Run an admin request with one automatic re-authentication retry."""
        logger.debug("SOAP %s %s", self.side, name)
        try:
            return self.admin.request(name, content)
        except ZimbraSoapServerError as exc:
            if (exc.code or "").lower() in {
                "service.auth_expired",
                "service.auth_required",
                "service.invalid_auth_token",
            }:
                self.relogin()
                return self.admin.request(name, content)
            raise

    def request_list(self, name: str, content: Optional[Dict[str, Any]] = None):
        response = self.request(name, content)
        for value in response.values():
            if isinstance(value, list):
                return value
            if isinstance(value, dict):
                return [value]
        return []

    def request_single(self, name: str, content: Optional[Dict[str, Any]] = None):
        response = self.request(name, content)
        for value in response.values():
            if isinstance(value, list):
                return value[0] if value else None
            if isinstance(value, dict):
                return value
        return None

    # ------------------------------------------------------------------
    def account_client(self, email: str) -> ZimbraAccountClient:
        """Return a delegated account client, guarded on the source."""
        client = ZimbraAccountClient(self.proxy_host, self.web_port)
        client.com.context = _ssl_context(self.connection)
        client.com.timeout = int(self.connection.get("read_timeout") or 300)
        if self.readonly:
            guard_read_only(client)
        client.delegated_login(email, self.admin)
        return client

    def mail_client(self, email: str) -> ZimbraMailClient:
        """Return a delegated mail client, guarded on the source."""
        client = ZimbraMailClient(self.proxy_host, self.web_port)
        client.com.context = _ssl_context(self.connection)
        client.com.timeout = int(self.connection.get("read_timeout") or 300)
        if self.readonly:
            guard_read_only(client)
        client.delegated_login(email, self.admin)
        return client

    def auth_token(self, email: str) -> Tuple[str, int]:
        """Return a delegated auth token and its lifetime for ``email``."""
        selector = zobjects.Account(name=email).to_selector()
        response = self.request("DelegateAuth", {"account": selector})
        return str(response["authToken"]), int(response.get("lifetime") or 0)

    # ------------------------------------------------------------------
    def rest(self) -> RestClient:
        return RestClient(
            self.proxy_host,
            self.web_port,
            verify_ssl=bool(self.connection.get("verify_ssl")),
            ca_bundle=self.connection.get("ca_bundle") or "",
            timeout=int(self.connection.get("read_timeout") or 300),
        )

    def export_url(
        self, email: str, query: str = "is:anywhere", types: str = ""
    ) -> str:
        base = self.rest().home_url(email)
        url = "%s?fmt=tgz&meta=1&query=%s" % (
            base,
            urllib.parse.quote(query, safe=":"),
        )
        if types:
            url += "&types=%s" % urllib.parse.quote(types, safe=",")
        return url

    def import_url(self, email: str, resolve: str = "skip") -> str:
        base = self.rest().home_url(email)
        return "%s?fmt=tgz&resolve=%s" % (base, resolve)

    # ------------------------------------------------------------------
    def test(self) -> Dict[str, Any]:
        """Authenticate and return a small, non-destructive health report."""
        started = time.time()
        version = ""
        try:
            payload = self.request_single("GetVersionInfo", {})
            if isinstance(payload, dict):
                version = payload.get("version") or ""
        except Exception:
            version = ""

        domains = self.request_list("GetAllDomains", {})
        servers = []
        try:
            servers = [
                entry.get("name", "")
                for entry in self.request_list("GetAllServers", {})
                if isinstance(entry, dict)
            ]
        except Exception:
            servers = []

        client = self.admin
        try:
            session_valid = bool(client.is_session_valid())
        except Exception:  # noqa: BLE001 - a version/auth quirk must not fail the test
            session_valid = False
        try:
            rest_ok, rest_detail = _probe_rest(self)
        except Exception as exc:  # noqa: BLE001
            rest_ok, rest_detail = False, describe_error(exc)
        response = {
            "ok": True,
            "side": self.side,
            "host": self.host,
            "store_host": config_module.store_host(self.connection),
            "store_admin_port": config_module.store_admin_port(self.connection),
            "proxy_host": config_module.proxy_host(self.connection),
            "proxy_web_port": config_module.proxy_web_port(self.connection),
            "admin_port": config_module.store_admin_port(self.connection),
            "web_port": self.web_port,
            "version": version,
            "domains": len(domains),
            "domain_names": [
                entry.get("name", "")
                for entry in domains
                if isinstance(entry, dict)
            ][:50],
            "servers": servers[:20],
            "latency_ms": int((time.time() - started) * 1000),
            "session_valid": session_valid,
            "rest_ok": rest_ok,
            "rest_detail": rest_detail,
        }
        return response


def _as_form_bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "oui"}


def _merge_test_overrides(
    connection: Dict[str, Any], overrides: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Apply unsaved form values on top of the stored connection."""
    merged = dict(connection)
    if not overrides:
        return merged
    store = overrides.get("store_host")
    if store is None:
        store = overrides.get("host")
    if store is not None:
        merged["store_host"] = config_module.clean_host(store)
    if "proxy_host" in overrides:
        merged["proxy_host"] = config_module.clean_host(overrides.get("proxy_host"))
    if "same_server_present" in overrides:
        merged["same_server"] = _as_form_bool(overrides.get("same_server"))
    elif "same_server" in overrides:
        merged["same_server"] = _as_form_bool(overrides.get("same_server"))
    for field in ("admin_login", "admin_password", "ca_bundle", "zmmailbox_mode"):
        value = overrides.get(field)
        if isinstance(value, str) and value.strip():
            merged[field] = value.strip()
    for field, legacy in (
        ("store_admin_port", "admin_port"),
        ("proxy_web_port", "web_port"),
        ("connect_timeout", "connect_timeout"),
        ("read_timeout", "read_timeout"),
    ):
        raw = overrides.get(field)
        if raw is None or str(raw).strip() == "":
            raw = overrides.get(legacy)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            number = int(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if number > 0:
            merged[field] = number
    if "verify_ssl" in overrides:
        merged["verify_ssl"] = _as_form_bool(overrides.get("verify_ssl"))
    return merged


def test_connection(
    config: config_module.Config,
    side: str,
    readonly: Optional[bool] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Test one side without raising: always returns a dict with ``ok``.

    ``overrides`` carries unsaved values so a connection test can run
    before the configuration is saved.
    """
    if readonly is None:
        readonly = side == "source"
    connection = _merge_test_overrides(config.connection(side), overrides)
    session = ZimbraSession(config, side, readonly=readonly, connection=connection)
    try:
        return session.test()
    except Exception as exc:  # noqa: BLE001 - reported to the caller
        return {
            "ok": False,
            "side": side,
            "host": session.host,
            "store_host": config_module.store_host(connection),
            "store_admin_port": config_module.store_admin_port(connection),
            "proxy_host": config_module.proxy_host(connection),
            "proxy_web_port": config_module.proxy_web_port(connection),
            "error": describe_error(exc),
        }
    finally:
        session.relogin()

def _probe_rest(session: ZimbraSession) -> Tuple[bool, str]:
    """Check that the REST/web port answers, distinguishing 5xx from auth errors."""
    rest = session.rest()
    try:
        response = rest._open(rest.base_url("/"), method="GET")
        try:
            return True, "HTTP %s" % getattr(response, "status", "?")
        finally:
            response.close()
    except RestError as exc:
        status = exc.status
        if status in (400, 401, 403, 404, 405, 302):
            return True, "HTTP %s" % status
        return False, describe_error(exc)
    except Exception as exc:  # noqa: BLE001
        return False, describe_error(exc)


def preflight(
    config: config_module.Config,
    sides: Iterable[str] = ("source", "destination"),
    check_rest: bool = True,
) -> Dict[str, Any]:
    """Verify that both Zimbra servers answer before a write-heavy step."""
    report: Dict[str, Any] = {"ok": True, "sides": {}}
    for side in sides:
        connection = config.connection(side)
        entry: Dict[str, Any] = {
            "side": side,
            "host": config_module.store_host(connection),
            "store_host": config_module.store_host(connection),
            "store_admin_port": config_module.store_admin_port(connection),
            "proxy_host": config_module.proxy_host(connection),
            "proxy_web_port": config_module.proxy_web_port(connection),
            "admin_port": config_module.store_admin_port(connection),
            "web_port": config_module.proxy_web_port(connection),
            "admin_ok": False,
            "rest_ok": False,
            "version": "",
            "errors": [],
        }
        session = ZimbraSession(config, side, readonly=(side == "source"))
        try:
            payload = session.request_single("GetVersionInfo", {}) or {}
            entry["admin_ok"] = True
            entry["version"] = payload.get("version") or ""
        except Exception as exc:  # noqa: BLE001
            entry["errors"].append("SOAP admin STORE : %s" % describe_error(exc))
        if check_rest:
            try:
                entry["rest_ok"], entry["rest_detail"] = _probe_rest(session)
                if not entry["rest_ok"]:
                    entry["errors"].append("REST PROXY : %s" % entry["rest_detail"])
            except Exception as exc:  # noqa: BLE001
                entry["errors"].append("REST PROXY : %s" % describe_error(exc))
        entry["ok"] = not entry["errors"]
        if not entry["ok"]:
            report["ok"] = False
        report["sides"][side] = entry
    return report
