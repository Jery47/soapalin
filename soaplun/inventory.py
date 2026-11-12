"""Read-only inventory snapshots of a Zimbra server."""

import datetime
import logging
import threading
from typing import Any, Callable, Dict, List, Optional

from . import config as config_module
from .connections import ZimbraSession, describe_error
from .inventory_store import (
    SNAPSHOT_KEEP,
    _is_index_file,
    build_snapshot_index,
    cleanup_snapshots,
    latest_snapshot,
    latest_snapshot_index,
    list_snapshots,
    load_snapshot,
    load_snapshot_index,
    snapshot_dir,
    snapshot_index_path,
    snapshot_path,
    write_snapshot,
    write_snapshot_index,
)
from .models import Snapshot
from .parallel import map_ordered

__all__ = [
    "SNAPSHOT_KEEP",
    "_is_index_file",
    "build_snapshot_index",
    "cleanup_snapshots",
    "latest_snapshot",
    "latest_snapshot_index",
    "list_snapshots",
    "load_snapshot",
    "load_snapshot_index",
    "snapshot_dir",
    "snapshot_index_path",
    "snapshot_path",
    "write_snapshot",
    "write_snapshot_index",
]

logger = logging.getLogger(__name__)

Progress = Optional[Callable[[str, int, int, str], None]]


# ----------------------------------------------------------------------
# Small parsing helpers
# ----------------------------------------------------------------------
def as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def unwrap(payload: Any, key: str) -> Any:
    """Return ``payload[key]`` when payload is the usual ``{key: value}`` dict."""
    if isinstance(payload, dict) and key in payload:
        return payload[key]
    return payload


def attr_entries(entry: Any) -> List[Dict[str, Any]]:
    if not isinstance(entry, dict):
        return []
    return [item for item in as_list(entry.get("a")) if isinstance(item, dict)]


def attrs_to_dict(entry: Any) -> Dict[str, Any]:
    """Convert Zimbra ``<a n="...">value</a>`` entries into a dict.

    Repeated attribute names (aliases, for instance) become lists.
    """
    result: Dict[str, Any] = {}
    for item in attr_entries(entry):
        name = item.get("n")
        if not name:
            continue
        value = item.get("_content", "")
        if name in result:
            existing = result[name]
            if not isinstance(existing, list):
                existing = [existing]
            existing.append(value)
            result[name] = existing
        else:
            result[name] = value
    return result


def entry_name(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("name") or entry.get("_content") or "")
    return str(entry or "")


def entry_id(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("id") or "")
    return ""


def bool_attr(attrs: Dict[str, Any], name: str) -> bool:
    value = attrs.get(name)
    if isinstance(value, list):
        value = value[-1] if value else ""
    return str(value).strip().lower() in {"1", "true", "yes"}


def account_domain(email: str) -> str:
    return email.rsplit("@", 1)[1] if "@" in email else ""


def now_iso() -> str:
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def zobject_data(obj: Any) -> Dict[str, Any]:
    """Return the raw dict behind a zimsoap zobject (or the object itself)."""
    if hasattr(obj, "get_full_data"):
        data = obj.get_full_data()
        if isinstance(data, dict):
            return data
    if isinstance(obj, dict):
        return obj
    return {}


# ----------------------------------------------------------------------
# Collectors
# ----------------------------------------------------------------------
def collect_domains(session: ZimbraSession) -> List[Dict[str, Any]]:
    domains: List[Dict[str, Any]] = []
    for entry in session.request_list("GetAllDomains", {}):
        name = entry_name(entry)
        if not name:
            continue
        try:
            full = unwrap(
                session.request_single(
                    "GetDomain", {"domain": {"by": "name", "_content": name}}
                ),
                "domain",
            )
        except Exception:
            full = entry
        attrs = attrs_to_dict(full if isinstance(full, dict) else entry)
        domains.append(
            {
                "name": name,
                "id": entry_id(full) or entry_id(entry),
                "attrs": attrs,
                "is_alias": bool_attr(attrs, "zimbraDomainType")
                or bool(attrs.get("zimbraDomainAliasTargetId")),
            }
        )
    return domains


def collect_cos(session: ZimbraSession) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for entry in session.request_list("GetAllCos", {}):
        name = entry_name(entry)
        if not name:
            continue
        try:
            full = unwrap(
                session.request_single(
                    "GetCos", {"cos": {"by": "name", "_content": name}}
                ),
                "cos",
            )
        except Exception:
            full = entry
        items.append(
            {
                "name": name,
                "id": entry_id(full) or entry_id(entry),
                "attrs": attrs_to_dict(full if isinstance(full, dict) else entry),
            }
        )
    return items


def collect_servers(session: ZimbraSession) -> List[Dict[str, Any]]:
    servers: List[Dict[str, Any]] = []
    for entry in session.request_list("GetAllServers", {}):
        name = entry_name(entry)
        if not name:
            continue
        try:
            full = unwrap(
                session.request_single(
                    "GetServer", {"server": {"by": "name", "_content": name}}
                ),
                "server",
            )
        except Exception:
            full = entry
        servers.append(
            {
                "name": name,
                "id": entry_id(full) or entry_id(entry),
                "attrs": attrs_to_dict(full if isinstance(full, dict) else entry),
            }
        )
    return servers


def _fetch_account(
    session: ZimbraSession, name: str, with_stats: bool = False
) -> Dict[str, Any]:
    selector = {"by": "name", "_content": name}
    payload = session.request_single(
        "GetAccount", {"account": selector, "applyCos": "0"}
    )
    account = unwrap(payload, "account")
    if not isinstance(account, dict):
        account = payload if isinstance(payload, dict) else {}
    attrs = attrs_to_dict(account)

    effective: Dict[str, Any] = {}
    try:
        eff_payload = session.request_single(
            "GetAccount",
            {
                "account": selector,
                "applyCos": "1",
                "attrs": "zimbraCOSId,zimbraMailHost,zimbraMailQuota",
            },
        )
        eff_account = unwrap(eff_payload, "account")
        if isinstance(eff_account, dict):
            effective = attrs_to_dict(eff_account)
    except Exception:
        effective = {}

    stats: Dict[str, Any] = {}
    if with_stats:
        try:
            mbox = unwrap(
                session.request_single("GetMailbox", {"account": selector}), "mbox"
            )
            if not isinstance(mbox, dict):
                mbox = unwrap(mbox, "mailbox")
            if isinstance(mbox, dict):
                stats = {
                    "size": mbox.get("size") or mbox.get("s"),
                    "messages": mbox.get("msgCount") or mbox.get("m"),
                    "unread": mbox.get("unreadCount") or mbox.get("u"),
                }
        except Exception:
            stats = {}

    aliases = attrs.get("zimbraMailAlias", [])
    if not isinstance(aliases, list):
        aliases = [aliases] if aliases else []

    return {
        "name": name,
        "id": entry_id(account),
        "domain": account_domain(name),
        "attrs": attrs,
        "aliases": [alias for alias in aliases if alias],
        "effective": effective,
        "cos_id": effective.get("zimbraCOSId", ""),
        "mail_host": effective.get("zimbraMailHost", ""),
        "is_admin": bool_attr(attrs, "zimbraIsAdminAccount"),
        "is_system": bool_attr(attrs, "zimbraIsSystemAccount"),
        "is_virtual": bool_attr(attrs, "zimbraIsExternalVirtualAccount"),
        "mailbox": stats,
    }


_SEARCH_TYPES = {
    "GetAllAccounts": "accounts",
    "GetAllCalendarResources": "resources",
}
_SEARCH_RESULT_KEYS = {
    "accounts": "account",
    "resources": "calresource",
}


def _search_by_mail_domain(
    session: ZimbraSession, search_types: str, domain: str
) -> List[Any]:
    """Fallback listing that does not depend on the domain's LDAP subtree.

    Some domains have no ``ou=people`` subtree (e.g. a domain created outside
    Zimbra or left in a broken state).  Every request scoped to that domain
    then fails with ``unable to list all objects``.  A global ``SearchDirectory``
    filtered on the mail domain uses the existing people tree instead.
    """
    query = "(mail=*@%s)" % domain
    key = _SEARCH_RESULT_KEYS[search_types]
    page = 1000
    offset = 0
    entries: List[Any] = []
    while True:
        response = session.request(
            "SearchDirectory",
            {
                "types": search_types,
                "query": query,
                "limit": page,
                "offset": offset,
            },
        )
        batch = response.get(key)
        if isinstance(batch, dict):
            batch = [batch]
        batch = batch or []
        entries.extend(batch)
        if str(response.get("more", "0")).lower() not in {"1", "true"} or not batch:
            break
        offset += len(batch)
    return entries


def _domain_entries(
    session: ZimbraSession,
    command: str,
    domains: Optional[List[Dict[str, Any]]],
    warnings: Optional[List[str]],
    label: str,
) -> List[Any]:
    """List accounts/resources domain by domain.

    A global ``GetAllAccounts``/``GetAllCalendarResources`` call makes Zimbra
    iterate over every domain, including alias domains.  Alias domains have no
    LDAP subtree, so the search fails with ``system failure: unable to list all
    objects`` and the whole request is lost.  Querying the real domains one at a
    time avoids that and isolates failures to a single domain.
    """
    names = [
        item.get("name")
        for item in (domains or [])
        if item.get("name") and not item.get("is_alias")
    ]
    if not names:
        return session.request_list(command, {})

    entries: List[Any] = []
    for name in names:
        try:
            entries.extend(
                session.request_list(
                    command, {"domain": {"by": "name", "_content": name}}
                )
            )
        except Exception:  # noqa: BLE001 - one domain must not abort the rest
            search_types = _SEARCH_TYPES.get(command)
            if search_types:
                try:
                    entries.extend(_search_by_mail_domain(session, search_types, name))
                    continue
                except Exception as fallback_exc:  # noqa: BLE001
                    exc = fallback_exc
            if warnings is not None:
                warnings.append("%s (%s): %s" % (label, name, describe_error(exc)))
    return entries


def collect_accounts(
    session: ZimbraSession,
    sync: Optional[Dict[str, Any]] = None,
    progress: Progress = None,
    with_stats: bool = False,
    domains: Optional[List[Dict[str, Any]]] = None,
    warnings: Optional[List[str]] = None,
    cancel=None,
    parallelism: int = 1,
    session_factory=None,
) -> List[Dict[str, Any]]:
    sync = sync or {}
    include_admin = bool(sync.get("include_admin_accounts", False))
    include_system = bool(sync.get("include_system_accounts", False))

    entries = _domain_entries(session, "GetAllAccounts", domains, warnings, "accounts")
    candidates: List[str] = []
    for entry in entries:
        name = entry_name(entry)
        if not name or "@" not in name:
            continue
        attrs = attrs_to_dict(entry)
        if not include_system and bool_attr(attrs, "zimbraIsSystemAccount"):
            continue
        if not include_admin and bool_attr(attrs, "zimbraIsAdminAccount"):
            continue
        if bool_attr(attrs, "zimbraIsExternalVirtualAccount"):
            continue
        candidates.append(name)

    total = len(candidates)
    local = threading.local()
    progress_lock = threading.Lock()
    completed = 0

    def worker_session():
        if session_factory is None:
            return session
        if not hasattr(local, "session"):
            local.session = session_factory()
        return local.session

    def task(index: int, name: str) -> Optional[Dict[str, Any]]:
        if cancel is not None and cancel.is_set():
            return None
        try:
            return _fetch_account(
                worker_session(), name, with_stats=with_stats
            )
        except Exception as exc:  # noqa: BLE001 - keep going, report later
            return {
                "name": name,
                "domain": account_domain(name),
                "attrs": {},
                "aliases": [],
                "effective": {},
                "cos_id": "",
                "mail_host": "",
                "is_admin": False,
                "is_system": False,
                "error": describe_error(exc),
            }

    def completed_task(index: int, item: Optional[Dict[str, Any]]) -> None:
        nonlocal completed
        with progress_lock:
            completed += 1
            if progress:
                progress("accounts", completed, total, candidates[index - 1])

    return map_ordered(
        candidates, task, parallelism, on_complete=completed_task
    )


def collect_distribution_lists(session: ZimbraSession) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for entry in session.request_list("GetAllDistributionLists", {}):
        name = entry_name(entry)
        if not name:
            continue
        try:
            full = unwrap(
                session.request_single(
                    "GetDistributionList", {"dl": {"by": "name", "_content": name}}
                ),
                "dl",
            )
        except Exception:
            full = entry
        if not isinstance(full, dict):
            full = entry
        attrs = attrs_to_dict(full)
        members: List[str] = []
        for member in as_list(full.get("dlm")):
            if isinstance(member, dict):
                value = member.get("_content") or member.get("name") or ""
            else:
                value = str(member or "")
            if value:
                members.append(value)
        items.append(
            {
                "name": name,
                "id": entry_id(full) or entry_id(entry),
                "attrs": attrs,
                "members": members,
                "is_dynamic": bool_attr(attrs, "zimbraDLType")
                or str(attrs.get("zimbraDLType", "")).lower() == "dynamic",
            }
        )
    return items


def collect_resources(
    session: ZimbraSession,
    domains: Optional[List[Dict[str, Any]]] = None,
    warnings: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    entries = _domain_entries(
        session, "GetAllCalendarResources", domains, warnings, "resources"
    )
    for entry in entries:
        name = entry_name(entry)
        if not name:
            continue
        try:
            full = unwrap(
                session.request_single(
                    "GetCalendarResource",
                    {"calresource": {"by": "name", "_content": name}},
                ),
                "calresource",
            )
        except Exception:
            full = entry
        if not isinstance(full, dict):
            full = entry
        items.append(
            {
                "name": name,
                "id": entry_id(full) or entry_id(entry),
                "attrs": attrs_to_dict(full),
            }
        )
    return items


# ----------------------------------------------------------------------
# Deep settings (per account, read from source at apply time)
# ----------------------------------------------------------------------
def _walk_folders(node: Any) -> List[Dict[str, Any]]:
    folders: List[Dict[str, Any]] = []
    if not isinstance(node, dict):
        return folders
    folders.append(node)
    for child in as_list(node.get("folder")):
        folders.extend(_walk_folders(child))
    return folders


def collect_folders(
    client: Any, colored_only: bool = True, include_root: bool = False
) -> List[Dict[str, Any]]:
    payload = client.request(
        "GetFolder", {"folder": {"path": "/"}, "depth": "20"}
    )
    root = payload.get("folder") if isinstance(payload, dict) else None
    folders: List[Dict[str, Any]] = []
    for folder in _walk_folders(root):
        path = str(folder.get("absFolderPath") or "")
        color = folder.get("color")
        rgb = folder.get("rgb")
        if not path:
            continue
        if path == "/" and not include_root:
            continue
        if colored_only and not color and not rgb:
            continue
        folders.append(
            {
                "path": path,
                "id": str(folder.get("id") or ""),
                "name": str(folder.get("name") or ""),
                "view": str(folder.get("view") or ""),
                "color": str(color) if color not in {None, ""} else "",
                "rgb": str(rgb) if rgb not in {None, ""} else "",
                "acl": folder.get("acl") or {},
            }
        )
    return folders


def collect_folder_colors(client: Any) -> List[Dict[str, Any]]:
    return collect_folders(client, colored_only=True)


def collect_outgoing_shares(
    client: Any, owner_email: str = ""
) -> List[Dict[str, Any]]:
    shares: List[Dict[str, Any]] = []
    seen = set()
    for folder in collect_folders(
        client, colored_only=False, include_root=True
    ):
        grants = as_list((folder.get("acl") or {}).get("grant"))
        for grant in grants:
            if not isinstance(grant, dict):
                continue
            grantee = str(grant.get("d") or "").strip()
            rights = str(grant.get("perm") or grant.get("right") or "").strip()
            grantee_type = str(grant.get("gt") or "usr")
            if not rights:
                continue
            if not grantee and grantee_type not in {"pub", "all"}:
                continue
            if owner_email and grantee.lower() == owner_email.lower():
                continue
            key = (folder["path"], grantee.lower(), grantee_type, rights)
            if key in seen:
                continue
            seen.add(key)
            shares.append(
                {
                    "folderPath": folder["path"],
                    "granteeName": grantee,
                    "granteeType": grantee_type,
                    "rights": rights,
                }
            )
    return shares


def collect_tags(client: Any) -> List[Dict[str, Any]]:
    response = client.request("GetTag", {}) or {}
    tags: List[Dict[str, Any]] = []
    for item in as_list(response.get("tag")):
        data = zobject_data(item)
        name = str(data.get("name") or "")
        if not name:
            continue
        tags.append(
            {
                "id": str(data.get("id") or ""),
                "name": name,
                "color": str(data.get("color") or ""),
                "rgb": str(data.get("rgb") or ""),
            }
        )
    return tags


def collect_account_settings(session: ZimbraSession, email: str) -> Dict[str, Any]:
    """Collect signatures, identities, prefs, filters, lists, shares, rights."""
    settings: Dict[str, Any] = {
        "signatures": [],
        "identities": [],
        "prefs": {},
        "filters_in": [],
        "filters_out": [],
        "whitelist": [],
        "blacklist": [],
        "shares_out": [],
        "mountpoints": [],
        "permissions": [],
        "folders": [],
        "all_folders": [],
        "tags": [],
        "account_attrs": {},
        "cos_name": "",
    }
    try:
        account = session.account_client(email)
    except Exception as exc:  # noqa: BLE001
        settings["error"] = describe_error(exc)
        return settings
    try:
        mail = session.mail_client(email)
    except Exception as exc:  # noqa: BLE001
        mail = None
        settings.setdefault("warnings", []).append(
            "mailbox: %s" % describe_error(exc)
        )

    def safe(label, func, default):
        try:
            return func()
        except Exception as exc:  # noqa: BLE001
            settings.setdefault("warnings", []).append("%s: %s" % (label, describe_error(exc)))
            return default

    settings["signatures"] = [
        zobject_data(item) for item in safe("signatures", account.get_signatures, [])
    ]
    settings["identities"] = [
        zobject_data(item) for item in safe("identities", account.get_identities, [])
    ]
    settings["prefs"] = safe("prefs", account.get_preferences, {}) or {}
    try:
        account_payload = session.request_single(
            "GetAccount",
            {
                "account": {"by": "name", "_content": email},
                "applyCos": "0",
            },
        )
        account_obj = unwrap(account_payload, "account")
        attrs = attrs_to_dict(
            account_obj if isinstance(account_obj, dict) else {}
        )
        settings["account_attrs"] = attrs
        cos_id = attrs.get("zimbraCOSId")
        if cos_id:
            try:
                cos_payload = session.request_single(
                    "GetCos",
                    {"cos": {"by": "id", "_content": str(cos_id)}},
                )
                cos_obj = unwrap(cos_payload, "cos")
                if isinstance(cos_obj, dict):
                    settings["cos_name"] = str(cos_obj.get("name") or "")
            except Exception as exc:  # noqa: BLE001
                settings.setdefault("warnings", []).append(
                    "cos: %s" % describe_error(exc)
                )
    except Exception as exc:  # noqa: BLE001
        settings["account_attrs"] = {}
        settings.setdefault("warnings", []).append(
            "attributs de compte: %s" % describe_error(exc)
        )
    settings["folders"] = safe(
        "folders", lambda: collect_folder_colors(mail) if mail else [], []
    )
    settings["all_folders"] = safe(
        "all folders",
        lambda: collect_folders(mail, colored_only=False) if mail else [],
        [],
    )
    settings["tags"] = safe(
        "tags", lambda: collect_tags(mail) if mail else [], []
    )
    settings["filters_in"] = [
        zobject_data(item)
        for item in safe(
            "filters in",
            lambda: mail.get_filter_rules("in") if mail else [],
            [],
        )
    ]
    settings["filters_out"] = [
        zobject_data(item)
        for item in safe(
            "filters out",
            lambda: mail.get_filter_rules("out") if mail else [],
            [],
        )
    ]
    lists = safe("whitelist", account.get_white_black_lists, {}) or {}
    settings["whitelist"] = _address_list(lists.get("whiteList"))
    settings["blacklist"] = _address_list(lists.get("blackList"))
    settings["shares_out"] = safe(
        "shares out",
        lambda: collect_outgoing_shares(mail, email) if mail else [],
        [],
    )
    settings["mountpoints"] = safe("shares in", account.get_share_info, [])
    try:
        permissions = (mail or account).request("GetPermission", {})
        settings["permissions"] = as_list(permissions.get("ace"))
    except Exception as exc:  # noqa: BLE001
        settings.setdefault("warnings", []).append(
            "permissions: %s" % describe_error(exc)
        )
    return settings


def _address_list(node: Any) -> List[str]:
    values: List[str] = []
    if not isinstance(node, dict):
        return values
    for item in as_list(node.get("addr")):
        if isinstance(item, dict):
            value = item.get("_content") or item.get("addr") or ""
        else:
            value = str(item or "")
        if value:
            values.append(value)
    return values


# ----------------------------------------------------------------------
# Snapshot persistence
# ----------------------------------------------------------------------

































# Public entry point
# ----------------------------------------------------------------------
def collect_snapshot(
    config: config_module.Config,
    side: str,
    progress: Progress = None,
    with_stats: bool = True,
    profile: Optional[str] = None,
    cancel=None,
    parallelism: int = 1,
) -> Snapshot:
    """Collect a full, read-only snapshot for ``side`` and persist it."""
    profile = profile or config.current_profile()
    session = ZimbraSession(config, side, readonly=(side == "source"))

    def session_factory():
        with config.profile_context(profile):
            return ZimbraSession(config, side, readonly=(side == "source"))
    snapshot: Dict[str, Any] = {
        "version": 1,
        "created_at": now_iso(),
        "side": side,
        "profile": profile,
        "profile_name": config.profile_name(profile),
        "host": session.host,
        "domains": [],
        "cos": [],
        "servers": [],
        "accounts": [],
        "distribution_lists": [],
        "resources": [],
        "warnings": [],
    }

    steps_total = 6
    done = 0
    progress_lock = threading.Lock()

    def report(label: str) -> None:
        nonlocal done
        with progress_lock:
            done += 1
            current = done
        if progress:
            progress("collect", current, steps_total, "Collecting %s" % label)

    def run_phase(items, use_shared: bool):
        def run(item):
            label, key, collector = item
            if cancel is not None and cancel.is_set():
                return label, key, None
            try:
                active = session if use_shared else session_factory()
                return label, key, collector(active)
            except Exception as exc:  # noqa: BLE001
                snapshot["warnings"].append(
                    "%s: %s" % (label, describe_error(exc))
                )
                return label, key, None

        if parallelism > 1 and len(items) > 1 and not use_shared:
            results = map_ordered(
                items,
                lambda index, item: run(item),
                parallelism,
                on_complete=lambda index, result: report(result[0]),
            )
        else:
            results = []
            for item in items:
                result = run(item)
                report(result[0])
                results.append(result)
        for label, key, value in results:
            if value is not None:
                snapshot[key] = value

    run_phase(
        [
            ("domains", "domains", collect_domains),
            ("cos", "cos", collect_cos),
            ("servers", "servers", collect_servers),
        ],
        use_shared=parallelism <= 1,
    )

    if cancel is not None and cancel.is_set():
        raise RuntimeError("Inventaire interrompu.")
    if progress:
        progress("collect", done, steps_total, "Collecting accounts")
    try:
        snapshot["accounts"] = collect_accounts(
            session,
            sync=config.sync(),
            progress=progress,
            with_stats=with_stats,
            domains=snapshot["domains"],
            warnings=snapshot["warnings"],
            cancel=cancel,
            parallelism=parallelism,
            session_factory=session_factory,
        )
    except Exception as exc:  # noqa: BLE001
        snapshot["warnings"].append("accounts: %s" % describe_error(exc))
    report("accounts")

    run_phase(
        [
            (
                "distribution lists",
                "distribution_lists",
                collect_distribution_lists,
            ),
            (
                "resources",
                "resources",
                lambda active: collect_resources(
                    active,
                    domains=snapshot["domains"],
                    warnings=snapshot["warnings"],
                ),
            ),
        ],
        use_shared=parallelism <= 1,
    )

    if cancel is not None and cancel.is_set():
        raise RuntimeError("Inventaire interrompu.")
    if progress:
        progress("saving", steps_total, steps_total, "Writing snapshot")

    # Enrich accounts with COS names where possible.
    cos_by_id = {item["id"]: item["name"] for item in snapshot["cos"] if item.get("id")}
    for account in snapshot["accounts"]:
        account["cos_name"] = cos_by_id.get(account.get("cos_id", ""), "")

    snapshot["counts"] = {
        "domains": len(snapshot["domains"]),
        "cos": len(snapshot["cos"]),
        "servers": len(snapshot["servers"]),
        "accounts": len(snapshot["accounts"]),
        "distribution_lists": len(snapshot["distribution_lists"]),
        "resources": len(snapshot["resources"]),
    }
    path = write_snapshot(config, side, snapshot, profile=profile)
    snapshot["path"] = str(path)
    return snapshot
