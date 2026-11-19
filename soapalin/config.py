"""Configuration storage, secret encryption and domain mapping helpers."""

import base64
import copy
import datetime
import json
import os
import shutil
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Union

from . import mapping, storage

try:  # pragma: no cover - exercised when cryptography is installed
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover - fallback for a bare Python install
    Fernet = None
    InvalidToken = Exception


ALWAYS_IGNORE_ATTRS = {
    "userPassword",
    "zimbraCsrfTokenData",
    "zimbraAuthTokens",
    "zimbraAuthTokenValidityValue",
}

DEFAULT_ATTR_IGNORE = [
    "zimbraId",
    "zimbraMailAlias",
    "zimbraMemberOf",
    "zimbraMailHost",
    "zimbraCOSId",
    "zimbraCreateTimestamp",
    "zimbraMailboxId",
    "zimbraLastLogonTimestamp",
    "zimbraPasswordModifiedTime",
    "zimbraAuthTokenValidityValue",
    "zimbraForeignPrincipal",
    "zimbraMailDeliveryAddress",
    "zimbraMailCanonicalAddress",
    "zimbraMailSieveScript",
    "zimbraMailTransport",
    "zimbraTwoFactorAuth*",
    "zimbraAppSpecificPassword*",
    "zimbraPassword*",
    "zimbraDataSource*",
    "zimbraShare*",
    "objectClass",
    "cn",
    "uid",
    "mail",
]

LEGACY_DEFAULT_ATTR_IGNORE = DEFAULT_ATTR_IGNORE + [
    "zimbraIsSystemAccount",
    "zimbraIsSystemResource",
    "zimbraIsAdminAccount",
    "zimbraIsDelegatedAdminAccount",
    "zimbraIsExternalVirtualAccount",
    "zimbraAccountStatus",
    "zimbraMailStatus",
    "sn",
    "givenName",
    "zimbraAuth*",
]


DEFAULT_PREF_IGNORE = [
    "zimbraPrefOutOfOffice*",
    "zimbraPrefMailForwardingAddress",
    "zimbraPrefCalendarForwardInvitesTo",
    "zimbraPrefPassword*",
    "zimbraPrefMailSignature*",
]

DOMAIN_ATTR_IGNORE = [
    "zimbraId",
    "zimbraCreateTimestamp",
    "zimbraPreAuthKey",
    "zimbraDomainAliasTargetId",
    "zimbraDomainDefaultCOSId",
    "zimbraCert*",
    "zimbraSSL*",
    "zimbraSSLDoc*",
    "zimbraDomainName",
]

DL_ATTR_IGNORE = [
    "zimbraId",
    "zimbraCreateTimestamp",
    "zimbraMailAlias",
    "zimbraMemberOf",
    "zimbraIsSystemResource",
]

RESOURCE_ATTR_IGNORE = [
    "zimbraId",
    "zimbraCreateTimestamp",
    "zimbraMailAlias",
    "zimbraMemberOf",
]

COS_ATTR_IGNORE = [
    "zimbraId",
    "zimbraCreateTimestamp",
    "zimbraCOSId",
    "cn",
    "objectClass",
]


CONNECTION_DEFAULTS: Dict[str, Any] = {
    "label": "",
    "same_server": True,
    "store_host": "",
    "store_admin_port": 7071,
    "proxy_host": "",
    "proxy_web_port": 443,
    "admin_login": "",
    "admin_password": "",
    "verify_ssl": False,
    "ca_bundle": "",
    "connect_timeout": 15,
    "read_timeout": 300,
    "zmmailbox_mode": "auto",
}


def store_host(connection: Dict[str, Any]) -> str:
    return str(connection.get("store_host") or connection.get("host") or "")


def proxy_host(connection: Dict[str, Any]) -> str:
    if connection.get("same_server", True):
        return store_host(connection)
    return str(connection.get("proxy_host") or store_host(connection))


def store_admin_port(connection: Dict[str, Any]) -> int:
    return int(
        connection.get("store_admin_port")
        or connection.get("admin_port")
        or 7071
    )


def proxy_web_port(connection: Dict[str, Any]) -> int:
    return int(
        connection.get("proxy_web_port")
        or connection.get("web_port")
        or 443
    )


def connection_defaults(side: str) -> Dict[str, Any]:
    return dict(
        CONNECTION_DEFAULTS,
        label="Source" if side == "source" else "Destination",
    )


DEFAULTS: Dict[str, Any] = {
    "schema": 2,
    "default_profile": "",
    "profiles": {},
    "sync": {
        "domain_map": {},
        "mail_host_map": {},
        "default_mail_host": "",
        "fixed_password": "",
        "force_password_change": True,
        "reset_existing_passwords": False,
        "include_admin_accounts": False,
        "include_system_accounts": False,
        "copy_mailbox": True,
        "copy_settings": True,
        "copy_shares": True,
        "copy_permissions": True,
        "copy_filters": True,
        "copy_whitelists": True,
        "copy_prefs": False,
        "attr_ignore": list(DEFAULT_ATTR_IGNORE),
        "pref_ignore": list(DEFAULT_PREF_IGNORE),
        "keep_tgz_days": 7,
        "mailbox_resolve": "skip",
        "max_accounts_per_plan": 1000,
    },
    "storage": {
        "mailbox_path": "data/mailboxes",
        "report_path": "data/reports",
        "snapshot_path": "data/snapshots",
        "job_path": "data/jobs",
        "trash_path": "data/trash",
    },
}

def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge override into a copy of base."""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "oui"}


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def clean_host(value: Any) -> str:
    """Reduce a pasted URL or ``host:port`` to a bare host name.

    Users often paste ``https://zimbra.example.com/`` or ``host:7071`` in the
    host field.  Only the host name belongs in the configuration.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if "//" in text:
        text = text.split("//", 1)[1]
    text = text.split("/", 1)[0].strip()
    if text.startswith("[") and "]" in text:
        return text[1 : text.index("]")].strip()
    if text.count(":") == 1:
        name, _, port = text.partition(":")
        if port.isdigit():
            return name.strip()
    return text


class Config:
    """Load, mutate and persist the application configuration.

    Secrets are encrypted with Fernet when :mod:`cryptography` is available.
    A local key is generated next to the configuration file.  The fallback is
    deliberately obvious (``plain:`` prefix) so callers can warn about it.
    """

    SECRET_PATHS = (
        ("sync", "fixed_password"),
    )

    def __init__(self, base_dir: Optional[Union[Path, str]] = None):
        env_home = os.environ.get("SOAPALIN_HOME")
        self.base_dir = Path(base_dir or env_home or Path(__file__).resolve().parent.parent)
        self.config_dir = self.base_dir / "config"
        self.data_dir = self.base_dir / "data"
        self.config_path = self.config_dir / "config.json"
        self.key_path = self.config_dir / "secret.key"
        self.servers_dir = self.config_dir / "servers"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.servers_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._runtime_profiles: set = set()
        self._fernet = self._load_fernet()
        self._local = threading.local()
        raw_before = storage.read_json_or_none(self.config_path)
        corrupt = raw_before is None
        if corrupt:
            self._backup_corrupt_config()
            raw_before = {}
        raw = self._migrate_raw(raw_before or {})
        needs_save = (
            corrupt
            or not self.config_path.exists()
            or raw is not raw_before
        )
        self.data = deep_merge(DEFAULTS, raw)
        before = copy.deepcopy(self.data)
        self._normalize_profiles()
        self._migrate_attr_ignore_defaults()
        if self.data != before:
            needs_save = True
        self._decrypt_secrets()
        self._ensure_storage_dirs()
        if needs_save:
            self.save()

    def _backup_corrupt_config(self) -> None:
        if not self.config_path.exists():
            return
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = self.config_path.with_name(
            self.config_path.name + ".bak-" + stamp
        )
        try:
            shutil.copy2(self.config_path, backup)
        except OSError:
            pass

    def _migrate_raw(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(raw, dict):
            return {}
        if "profiles" not in raw and ("source" in raw or "destination" in raw):
            source = deep_merge(connection_defaults("source"), raw.get("source") or {})
            destination = deep_merge(
                connection_defaults("destination"), raw.get("destination") or {}
            )
            raw = dict(raw)
            raw["profiles"] = {
                "default": {
                    "id": "default",
                    "name": "Configuration principale",
                    "source": source,
                    "destination": destination,
                }
            }
            raw["default_profile"] = "default"
            raw["schema"] = 2
            raw.pop("source", None)
            raw.pop("destination", None)
        return raw

    def _normalize_connection(
        self, raw: Optional[Dict[str, Any]], side: str
    ) -> Dict[str, Any]:
        raw = dict(raw or {})
        legacy_host = raw.pop("host", None)
        legacy_admin = raw.pop("admin_port", None)
        legacy_web = raw.pop("web_port", None)
        merged = deep_merge(connection_defaults(side), raw)
        if legacy_host is not None:
            if not merged.get("store_host"):
                merged["store_host"] = legacy_host
            if not merged.get("proxy_host"):
                merged["proxy_host"] = legacy_host
        if legacy_admin is not None and "store_admin_port" not in raw:
            merged["store_admin_port"] = legacy_admin
        if legacy_web is not None and "proxy_web_port" not in raw:
            merged["proxy_web_port"] = legacy_web
        merged["same_server"] = bool(merged.get("same_server", True))
        if merged["same_server"] and not merged.get("proxy_host"):
            merged["proxy_host"] = merged.get("store_host", "")
        return merged

    def _normalize_profiles(self) -> None:
        profiles = self.data.setdefault("profiles", {})
        if not isinstance(profiles, dict):
            profiles = {}
            self.data["profiles"] = profiles
        for profile_id, profile in list(profiles.items()):
            if not isinstance(profile, dict):
                profile = {}
                profiles[profile_id] = profile
            profile.setdefault("id", profile_id)
            profile.setdefault("name", profile_id)
            for side in ("source", "destination"):
                profile[side] = self._normalize_connection(
                    profile.get(side), side
                )
        if not profiles and not any(self.servers_dir.glob("*.json")):
            profiles["default"] = {
                "id": "default",
                "name": "Configuration principale",
                "source": connection_defaults("source"),
                "destination": connection_defaults("destination"),
            }
        default = self.data.get("default_profile")
        if profiles and default not in profiles:
            self.data["default_profile"] = sorted(profiles)[0]
        elif not profiles:
            self.data["default_profile"] = ""
        self.data["schema"] = 2

    def _migrate_attr_ignore_defaults(self) -> None:
        sync = self.data.setdefault("sync", {})
        current = sync.get("attr_ignore")
        if not isinstance(current, list):
            return
        legacy = set(LEGACY_DEFAULT_ATTR_IGNORE)
        value = set(str(item) for item in current)
        if value in (legacy, legacy - {"zimbraAuth*"}):
            sync["attr_ignore"] = list(DEFAULT_ATTR_IGNORE)
    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _read_json(self, path: Path) -> Dict[str, Any]:
        return storage.read_json(path)

    def _write_json_atomic(self, path: Path, payload: Dict[str, Any]) -> None:
        storage.write_json_atomic(
            path, payload, indent=2, sort_keys=True, mode=0o600
        )

    def _load_fernet(self):
        if Fernet is None:
            return None
        if self.key_path.exists():
            try:
                key = self.key_path.read_bytes().strip()
                return Fernet(key)
            except (OSError, ValueError):
                pass
        key = Fernet.generate_key()
        storage.write_bytes_atomic(self.key_path, key, mode=0o600)
        return Fernet(key)

    def save(self) -> None:
        payload = copy.deepcopy(self.data)
        profiles = payload.get("profiles", {})
        for profile_id in list(profiles):
            if profile_id in self._runtime_profiles:
                del profiles[profile_id]
        for profile in profiles.values():
            if not isinstance(profile, dict):
                continue
            for side in ("source", "destination"):
                bucket = profile.setdefault(side, {})
                bucket["admin_password"] = self._encrypt(
                    bucket.get("admin_password", "")
                )
        for section, key in self.SECRET_PATHS:
            value = payload.get(section, {}).get(key, "")
            payload.setdefault(section, {})[key] = self._encrypt(value)
        self._write_json_atomic(self.config_path, payload)
        self._ensure_storage_dirs()

    def _ensure_storage_dirs(self) -> None:
        for key in (
            "mailbox_path",
            "report_path",
            "snapshot_path",
            "job_path",
            "trash_path",
        ):
            raw = self.data.get("storage", {}).get(key)
            if not raw:
                continue
            path = Path(raw)
            if not path.is_absolute():
                path = self.base_dir / path
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Secrets
    # ------------------------------------------------------------------
    def _encrypt(self, value: str) -> str:
        if not value:
            return ""
        if value.startswith("enc:") or value.startswith("plain:"):
            return value
        if self._fernet is not None:
            token = self._fernet.encrypt(value.encode("utf-8")).decode("ascii")
            return "enc:" + token
        return "plain:" + base64.b64encode(value.encode("utf-8")).decode("ascii")

    def _decrypt(self, value: str) -> str:
        if not value:
            return ""
        if value.startswith("enc:"):
            if self._fernet is None:
                return ""
            try:
                return self._fernet.decrypt(value[4:].encode("ascii")).decode("utf-8")
            except (InvalidToken, ValueError, TypeError):
                return ""
        if value.startswith("plain:"):
            try:
                return base64.b64decode(value[6:]).decode("utf-8")
            except (ValueError, TypeError):
                return ""
        return value

    def _decrypt_secrets(self) -> None:
        for profile in self.data.get("profiles", {}).values():
            if not isinstance(profile, dict):
                continue
            for side in ("source", "destination"):
                bucket = profile.setdefault(side, {})
                bucket["admin_password"] = self._decrypt(
                    bucket.get("admin_password", "")
                )
        for section, key in self.SECRET_PATHS:
            bucket = self.data.setdefault(section, {})
            bucket[key] = self._decrypt(bucket.get(key, ""))

    def encrypt_secret(self, value: str) -> str:
        """Encrypt a secret for storage outside the config file (plan JSON)."""
        return self._encrypt(value)

    def decrypt_secret(self, value: str) -> str:
        """Decrypt a value produced by :meth:`encrypt_secret`."""
        return self._decrypt(value)

    def _server_path(self, label: str) -> Path:
        return self.servers_dir / ("%s.json" % label)

    def _valid_server_label(self, label: str) -> str:
        value = str(label or "").strip()
        if not value or not all(
            char.isalnum() or char in "._-" for char in value
        ):
            raise ValueError(
                "Label serveur invalide : utilisez lettres, chiffres, '.', '_' "
                "ou '-'."
            )
        return value

    def servers(self) -> Dict[str, Dict[str, Any]]:
        result: Dict[str, Dict[str, Any]] = {}
        for path in sorted(self.servers_dir.glob("*.json")):
            try:
                payload = self._read_json(path)
            except (OSError, ValueError):
                continue
            if not isinstance(payload, dict):
                continue
            label = str(payload.get("label") or path.stem)
            server_type = str(payload.get("type") or "").lower()
            if server_type not in {"source", "destination"}:
                continue
            connection = {
                key: value
                for key, value in payload.items()
                if key not in {"label", "type"}
            }
            normalized = self._normalize_connection(connection, server_type)
            normalized["label"] = label
            normalized["admin_password"] = self._decrypt(
                normalized.get("admin_password", "")
            )
            result[label] = {
                "label": label,
                "type": server_type,
                "connection": normalized,
                "path": str(path),
            }
        return result

    def server(self, label: str) -> Dict[str, Any]:
        servers = self.servers()
        if label not in servers:
            raise KeyError("Serveur inconnu : %s" % label)
        return servers[label]

    def source_servers(self) -> Dict[str, Dict[str, Any]]:
        return {
            label: server
            for label, server in self.servers().items()
            if server["type"] == "source"
        }

    def destination_servers(self) -> Dict[str, Dict[str, Any]]:
        return {
            label: server
            for label, server in self.servers().items()
            if server["type"] == "destination"
        }

    def create_server(self, label: str, server_type: str) -> Dict[str, Any]:
        label = self._valid_server_label(label)
        server_type = str(server_type or "").lower()
        if server_type not in {"source", "destination"}:
            raise ValueError("Type serveur attendu : source ou destination.")
        if label in self.servers():
            raise ValueError("Ce label de serveur existe deja : %s" % label)
        path = self._server_path(label)
        payload = {
            key: value
            for key, value in connection_defaults(server_type).items()
            if key != "label"
        }
        payload.update({"label": label, "type": server_type})
        payload["admin_password"] = ""
        self._write_json_atomic(path, payload)
        return self.server(label)

    def save_server(self, label: str, connection: Dict[str, Any]) -> None:
        label = self._valid_server_label(label)
        existing = self.server(label)
        payload: Dict[str, Any] = dict(connection)
        payload["label"] = label
        payload["type"] = existing["type"]
        payload["admin_password"] = self._encrypt(
            payload.get("admin_password", "")
        )
        self._write_json_atomic(self._server_path(label), payload)

    def rename_server(self, label: str, new_label: str) -> str:
        server = self.server(label)
        new_label = self._valid_server_label(new_label)
        if new_label != label and new_label in self.servers():
            raise ValueError("Ce label de serveur existe deja : %s" % new_label)
        payload: Dict[str, Any] = dict(server["connection"])
        payload["label"] = new_label
        payload["type"] = server["type"]
        payload["admin_password"] = self._encrypt(
            payload.get("admin_password", "")
        )
        self._write_json_atomic(self._server_path(new_label), payload)
        if new_label != label:
            try:
                self._server_path(label).unlink()
            except OSError:
                pass
        return new_label

    def remove_server(self, label: str) -> None:
        self._valid_server_label(label)
        path = self._server_path(label)
        if not path.exists():
            raise KeyError("Serveur inconnu : %s" % label)
        path.unlink()

    def use_server(self, label: str) -> str:
        server = self.server(label)
        profile_id = label
        if profile_id in self.profiles() and profile_id not in self._runtime_profiles:
            raise ValueError(
                "Le label serveur %s entre en conflit avec une configuration existante."
                % label
            )
        source = (
            copy.deepcopy(server["connection"])
            if server["type"] == "source"
            else connection_defaults("source")
        )
        destination = (
            copy.deepcopy(server["connection"])
            if server["type"] == "destination"
            else connection_defaults("destination")
        )
        self.data.setdefault("profiles", {})[profile_id] = {
            "id": profile_id,
            "name": label,
            "source": source,
            "destination": destination,
        }
        self._runtime_profiles.add(profile_id)
        self.activate_profile(profile_id)
        return profile_id

    def use_servers(self, source_label: str, destination_label: str) -> str:
        source = self.server(source_label)
        destination = self.server(destination_label)
        if source["type"] != "source":
            raise ValueError("%s n'est pas un serveur source." % source_label)
        if destination["type"] != "destination":
            raise ValueError(
                "%s n'est pas un serveur destination." % destination_label
            )
        profile_id = "__migration__"
        if profile_id in self.profiles() and profile_id not in self._runtime_profiles:
            raise ValueError(
                "Le profil temporaire __migration__ entre en conflit avec une configuration existante."
            )
        self.data.setdefault("profiles", {})[profile_id] = {
            "id": profile_id,
            "name": "%s -> %s" % (source_label, destination_label),
            "source": copy.deepcopy(source["connection"]),
            "destination": copy.deepcopy(destination["connection"]),
        }
        self._runtime_profiles.add(profile_id)
        self.activate_profile(profile_id)
        return profile_id

    # ------------------------------------------------------------------
    # Profiles
    # ------------------------------------------------------------------
    def profiles(self) -> Dict[str, Dict[str, Any]]:
        return self.data.get("profiles", {})

    def default_profile(self) -> str:
        default = self.data.get("default_profile", "")
        if default in self.profiles():
            return default
        if self.profiles():
            return sorted(self.profiles())[0]
        return "default"

    def current_profile(self) -> str:
        profile = getattr(self._local, "profile", None)
        if profile in self.profiles():
            return profile
        return self.default_profile()

    def activate_profile(self, profile_id: Optional[str]) -> str:
        if profile_id in self.profiles():
            self._local.profile = profile_id
        return self.current_profile()

    def deactivate_profile(self) -> None:
        self._local.profile = None

    @contextmanager
    def profile_context(self, profile_id: Optional[str]):
        previous = getattr(self._local, "profile", None)
        self.activate_profile(profile_id)
        try:
            yield self.current_profile()
        finally:
            self._local.profile = previous

    def profile(self, profile_id: Optional[str] = None) -> Dict[str, Any]:
        profile_id = profile_id or self.current_profile()
        profiles = self.profiles()
        if profile_id in profiles:
            return profiles[profile_id]
        if self.default_profile() in profiles:
            return profiles[self.default_profile()]
        raise KeyError("No profile configured")

    def profile_name(self, profile_id: Optional[str] = None) -> str:
        return str(self.profile(profile_id).get("name") or profile_id or "")

    def _new_profile_id(self) -> str:
        while True:
            candidate = uuid.uuid4().hex[:8]
            if candidate not in self.profiles():
                return candidate

    def create_profile(
        self, name: str, from_profile: Optional[str] = None
    ) -> str:
        profile_id = self._new_profile_id()
        if from_profile and from_profile in self.profiles():
            origin = copy.deepcopy(self.profile(from_profile))
            source = origin.get("source", connection_defaults("source"))
            destination = origin.get(
                "destination", connection_defaults("destination")
            )
        else:
            source = connection_defaults("source")
            destination = connection_defaults("destination")
        self.profiles()[profile_id] = {
            "id": profile_id,
            "name": (name or "Nouvelle configuration").strip(),
            "source": source,
            "destination": destination,
        }
        self.save()
        return profile_id

    def duplicate_profile(self, profile_id: str) -> str:
        origin = self.profile(profile_id)
        name = "%s (copie)" % (origin.get("name") or profile_id)
        return self.create_profile(name, from_profile=profile_id)

    def rename_profile(self, profile_id: str, name: str) -> None:
        self.profile(profile_id)["name"] = (name or profile_id).strip()
        self.save()

    def set_default_profile(self, profile_id: str) -> None:
        if profile_id not in self.profiles():
            raise ValueError("Unknown profile: %s" % profile_id)
        self.data["default_profile"] = profile_id
        self.save()

    def profile_usage(self, profile_id: str) -> Dict[str, int]:
        counts = {"snapshots": 0, "jobs": 0}
        for key, label in (
            ("snapshot_path", "snapshots"),
            ("job_path", "jobs"),
        ):
            directory = self.storage_path(key)
            for path in directory.glob("*.json"):
                try:
                    with path.open("r", encoding="utf-8") as handle:
                        data = json.load(handle)
                except (OSError, ValueError):
                    continue
                if label == "jobs":
                    profile = (data.get("meta") or {}).get(
                        "profile"
                    ) or self.default_profile()
                else:
                    profile = data.get("profile") or self.default_profile()
                if profile == profile_id:
                    counts[label] += 1
        return counts

    def delete_profile(self, profile_id: str) -> None:
        if profile_id == self.default_profile():
            raise ValueError("Le profil par défaut ne peut pas être supprimé.")
        if profile_id not in self.profiles():
            raise KeyError("Unknown profile: %s" % profile_id)
        usage = self.profile_usage(profile_id)
        if any(usage.values()):
            raise ValueError(
                "Ce profil possede des donnees (inventaires : %d, taches : %d)."
                % (usage["snapshots"], usage["jobs"])
            )
        del self.profiles()[profile_id]
        self.save()

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------
    def connection(
        self, side: str, profile: Optional[str] = None
    ) -> Dict[str, Any]:
        if side not in ("source", "destination"):
            raise ValueError("side must be source or destination")
        return self.profile(profile)[side]

    def sync(self) -> Dict[str, Any]:
        return self.data["sync"]

    def storage_path(self, key: str) -> Path:
        raw = self.data.get("storage", {}).get(key, "")
        path = Path(raw)
        if not path.is_absolute():
            path = self.base_dir / path
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ------------------------------------------------------------------
    # Mapping helpers
    # ------------------------------------------------------------------
    def map_domain(self, domain: str) -> str:
        return mapping.map_domain(self.sync().get("domain_map"), domain)

    def map_email(self, email: str) -> str:
        return mapping.map_email(self.sync().get("domain_map"), email)

    def map_host(self, host: str) -> str:
        return mapping.map_host(
            self.sync().get("mail_host_map"),
            self.sync().get("default_mail_host") or "",
            host,
        )

    def ignored_attr(self, name: str, kind: str = "account") -> bool:
        if kind == "domain":
            patterns: Iterable[str] = DOMAIN_ATTR_IGNORE
        elif kind == "dl":
            patterns = DL_ATTR_IGNORE
        elif kind == "resource":
            patterns = RESOURCE_ATTR_IGNORE
        elif kind == "cos":
            patterns = COS_ATTR_IGNORE
        else:
            if name in ALWAYS_IGNORE_ATTRS:
                return True
            patterns = self.sync().get("attr_ignore") or DEFAULT_ATTR_IGNORE
        return mapping.matches(name, patterns)

    def ignored_pref(self, name: str) -> bool:
        patterns = self.sync().get("pref_ignore") or DEFAULT_PREF_IGNORE
        return mapping.matches(name, patterns)

    # ------------------------------------------------------------------
    # Form helpers
    # ------------------------------------------------------------------
    def _apply_connection_bucket(
        self,
        bucket: Dict[str, Any],
        form: Dict[str, str],
        keep_password: bool = True,
        side: str = "source",
    ) -> None:
        bucket["label"] = (form.get("label") or bucket.get("label") or side).strip()
        if "same_server_present" in form:
            same_server = _as_bool(form.get("same_server"))
        elif "same_server" in form:
            same_server = _as_bool(form.get("same_server"))
        else:
            same_server = bool(bucket.get("same_server", True))
        store = clean_host(
            form.get("store_host")
            or form.get("host")
            or bucket.get("store_host")
            or bucket.get("host")
            or ""
        )
        admin_port = _as_int(
            form.get("store_admin_port") or form.get("admin_port"),
            int(bucket.get("store_admin_port") or bucket.get("admin_port") or 7071),
        )
        if "proxy_host" in form:
            proxy = clean_host(form.get("proxy_host"))
        else:
            proxy = clean_host(bucket.get("proxy_host") or bucket.get("host") or "")
        web_port = _as_int(
            form.get("proxy_web_port") or form.get("web_port"),
            int(bucket.get("proxy_web_port") or bucket.get("web_port") or 443),
        )
        bucket["same_server"] = same_server
        bucket["store_host"] = store
        bucket["store_admin_port"] = admin_port
        bucket["proxy_host"] = proxy or (store if same_server else "")
        bucket["proxy_web_port"] = web_port
        bucket.pop("host", None)
        bucket.pop("admin_port", None)
        bucket.pop("web_port", None)
        bucket["admin_login"] = (form.get("admin_login") or "").strip()
        bucket["verify_ssl"] = _as_bool(form.get("verify_ssl"))
        bucket["ca_bundle"] = (form.get("ca_bundle") or "").strip()
        bucket["connect_timeout"] = _as_int(form.get("connect_timeout"), 15)
        bucket["read_timeout"] = _as_int(form.get("read_timeout"), 300)
        bucket["zmmailbox_mode"] = form.get("zmmailbox_mode") or "auto"
        password = form.get("admin_password") or ""
        if password:
            bucket["admin_password"] = password
        elif not keep_password:
            bucket["admin_password"] = ""

    def apply_connection_form(
        self,
        side: str,
        form: Dict[str, str],
        keep_password: bool = True,
        profile: Optional[str] = None,
    ) -> None:
        self._apply_connection_bucket(
            self.profile(profile).setdefault(side, {}),
            form,
            keep_password,
            side,
        )

    def apply_sync_form(self, form: Dict[str, str], keep_password: bool = True) -> None:
        sync = self.data.setdefault("sync", {})
        sync["default_mail_host"] = (form.get("default_mail_host") or "").strip()
        sync["force_password_change"] = _as_bool(form.get("force_password_change"))
        sync["reset_existing_passwords"] = _as_bool(form.get("reset_existing_passwords"))
        sync["include_admin_accounts"] = _as_bool(form.get("include_admin_accounts"))
        sync["include_system_accounts"] = _as_bool(form.get("include_system_accounts"))
        sync["copy_mailbox"] = _as_bool(form.get("copy_mailbox"))
        sync["copy_settings"] = _as_bool(form.get("copy_settings"))
        sync["copy_shares"] = _as_bool(form.get("copy_shares"))
        sync["copy_permissions"] = _as_bool(form.get("copy_permissions"))
        sync["copy_filters"] = _as_bool(form.get("copy_filters"))
        sync["copy_whitelists"] = _as_bool(form.get("copy_whitelists"))
        sync["copy_prefs"] = _as_bool(form.get("copy_prefs"))
        sync["keep_tgz_days"] = _as_int(form.get("keep_tgz_days"), 7)
        sync["mailbox_resolve"] = form.get("mailbox_resolve") or "skip"
        sync["max_accounts_per_plan"] = _as_int(form.get("max_accounts_per_plan"), 1000)
        password = form.get("fixed_password") or ""
        if password:
            sync["fixed_password"] = password
        elif not keep_password:
            sync["fixed_password"] = ""
