"""Copy per-account settings, signatures, filters, shares and permissions.

Everything here is additive: missing objects are created, existing destination
objects are kept.  Incoming share mounts are reported but not recreated in V1
because they depend on folder identifiers that differ between servers.
"""

import datetime
import fnmatch
import json
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import config as config_module
from . import inventory as inventory_module
from .connections import ZimbraSession, describe_error, preflight
from .inventory import as_list, attrs_to_dict, now_iso, unwrap, zobject_data

Progress = Optional[Callable[[int, int, str], None]]

SETTING_PARTS = (
    "settings",
    "prefs",
    "folders",
    "attributes",
    "tags",
    "cos",
    "filters",
    "lists",
    "shares",
    "permissions",
)


def _options_with_defaults(
    config: config_module.Config,
    plan: Dict[str, Any],
    options: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    merged = dict(config.sync())
    merged.update(plan.get("options") or {})
    merged.update(options or {})
    return merged


def normalize_parts(
    parts: Optional[List[str]], options: Dict[str, Any]
) -> List[str]:
    if parts:
        return [part for part in parts if part in SETTING_PARTS]
    if any(
        key in options
        for key in (
            "copy_settings",
            "copy_prefs",
            "copy_filters",
            "copy_whitelists",
            "copy_shares",
            "copy_permissions",
        )
    ):
        selected: List[str] = []
        if options.get("copy_settings", True):
            selected.append("settings")
            selected.append("folders")
        if options.get("copy_prefs", False):
            selected.append("prefs")
        if options.get("copy_filters", True):
            selected.append("filters")
        if options.get("copy_whitelists", True):
            selected.append("lists")
        if options.get("copy_shares", True):
            selected.append("shares")
        if options.get("copy_permissions", True):
            selected.append("permissions")
        return selected
    return list(SETTING_PARTS)


def _map_email(options: Dict[str, Any], email: str) -> str:
    if not email or "@" not in email:
        return email
    mapping = {
        str(key).lower(): value
        for key, value in (options.get("domain_map") or {}).items()
    }
    local, domain = email.rsplit("@", 1)
    return "%s@%s" % (local, mapping.get(str(domain).lower(), domain) or domain)


def _ignored_pref(options: Dict[str, Any], name: str) -> bool:
    patterns = (
        options.get("settings_attr_ignore")
        or options.get("settings_pref_ignore")
        or options.get("pref_ignore")
        or config_module.DEFAULT_PREF_IGNORE
    )
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


class SettingsError(Exception):
    pass


def a_entries(attrs: Dict[str, Any]) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    for name, value in (attrs or {}).items():
        if isinstance(value, (list, tuple)):
            for item in value:
                entries.append({"n": name, "_content": item})
        else:
            entries.append({"n": name, "_content": value})
    return entries


def _pref_value(value: Any) -> Any:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return value


def pref_entries(prefs: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build ``<pref name="...">value</pref>`` entries for ModifyPrefs."""
    entries: List[Dict[str, Any]] = []
    for name, value in (prefs or {}).items():
        if isinstance(value, (list, tuple)):
            for item in value:
                entries.append({"name": name, "_content": _pref_value(item)})
        else:
            entries.append({"name": name, "_content": _pref_value(value)})
    return entries


def _norm(value: Any) -> Any:
    if isinstance(value, list):
        return sorted(str(item) for item in value)
    return str(value).strip()


# ----------------------------------------------------------------------
# Individual copy helpers
# ----------------------------------------------------------------------
def copy_signatures(source_settings: Dict[str, Any], destination, dry_run: bool) -> Dict[str, Any]:
    result = {"created": 0, "skipped": 0, "errors": [], "id_map": {}}
    try:
        destination_signatures = [
            zobject_data(item) for item in destination.get_signatures()
        ]
    except Exception as exc:  # noqa: BLE001
        result["errors"].append("signatures: %s" % describe_error(exc))
        return result
    existing = {
        str(item.get("name") or ""): item
        for item in destination_signatures
        if item.get("name")
    }

    for signature in source_settings.get("signatures", []):
        data = zobject_data(signature)
        name = data.get("name") or ""
        source_id = str(data.get("id") or "")
        if not name:
            continue
        current = existing.get(name)
        if current is not None:
            if source_id and current.get("id"):
                result["id_map"][source_id] = str(current.get("id"))
            result["skipped"] += 1
            continue
        content = data.get("content")
        if isinstance(content, list):
            content = content[-1] if content else {}
        if not isinstance(content, dict):
            content = {}
        text = content.get("_content", "")
        content_type = content.get("type", "text/html")
        if dry_run:
            result["created"] += 1
            continue
        try:
            created = destination.create_signature(name, text, content_type)
            created_data = zobject_data(created)
            existing[name] = created_data
            if source_id and created_data.get("id"):
                result["id_map"][source_id] = str(created_data.get("id"))
            result["created"] += 1
        except Exception as exc:  # noqa: BLE001
            result["errors"].append("signature %s: %s" % (name, describe_error(exc)))
    return result


def copy_identities(source_settings: Dict[str, Any], destination, dry_run: bool) -> Dict[str, Any]:
    result = {"created": 0, "skipped": 0, "errors": []}
    try:
        existing = {
            zobject_data(item).get("name")
            for item in destination.get_identities()
        }
    except Exception as exc:  # noqa: BLE001
        result["errors"].append("identities: %s" % describe_error(exc))
        return result

    for identity in source_settings.get("identities", []):
        data = zobject_data(identity)
        name = data.get("name") or ""
        if not name or name.upper() == "DEFAULT":
            continue
        if name in existing:
            result["skipped"] += 1
            continue
        payload = {"identity": {"name": name, "a": data.get("a") or []}}
        if dry_run:
            result["created"] += 1
            continue
        try:
            destination.request("CreateIdentity", payload)
            existing.add(name)
            result["created"] += 1
        except Exception as exc:  # noqa: BLE001
            result["errors"].append("identité %s: %s" % (name, describe_error(exc)))
    return result


def copy_preferences(
    config: config_module.Config,
    source_settings: Dict[str, Any],
    destination,
    dry_run: bool,
    options: Optional[Dict[str, Any]] = None,
    signature_id_map: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    result = {"updated": 0, "skipped": 0, "errors": []}
    effective = options or config.sync()
    source_prefs = {
        name: value
        for name, value in (source_settings.get("prefs") or {}).items()
        if not _ignored_pref(effective, name)
    }
    if signature_id_map:
        for key in (
            "zimbraPrefDefaultSignatureId",
            "zimbraPrefForwardReplySignatureId",
        ):
            value = source_prefs.get(key)
            if value and str(value) in signature_id_map:
                source_prefs[key] = signature_id_map[str(value)]
    try:
        dest_prefs = destination.get_preferences() or {}
    except Exception as exc:  # noqa: BLE001
        result["errors"].append("préférences: %s" % describe_error(exc))
        return result

    changes = {
        name: value
        for name, value in source_prefs.items()
        if name not in dest_prefs or _norm(dest_prefs[name]) != _norm(value)
    }
    if not changes:
        return result
    if dry_run:
        result["updated"] = len(changes)
        return result

    def apply_preferences(items: List[Tuple[str, Any]]) -> None:
        if not items:
            return
        try:
            destination.request(
                "ModifyPrefs", {"pref": pref_entries(dict(items))}
            )
            result["updated"] += len(items)
        except Exception as exc:  # noqa: BLE001
            if len(items) > 1:
                middle = len(items) // 2
                apply_preferences(items[:middle])
                apply_preferences(items[middle:])
            else:
                result["errors"].append(
                    "preference %s: %s"
                    % (items[0][0], describe_error(exc))
                )

    apply_preferences(list(changes.items()))
    return result


FOLDER_RGB_COLORS = {
    "#0000CC": "1",
    "#00CCCC": "2",
    "#00CC00": "3",
    "#CC00CC": "4",
    "#CC0000": "5",
    "#CCCC00": "6",
    "#FF00FF": "7",
    "#666666": "8",
    "#CC6600": "9",
}


def _rgb_to_folder_color(value: str) -> str:
    text = (value or "").strip().upper()
    if text in FOLDER_RGB_COLORS:
        return FOLDER_RGB_COLORS[text]
    if not (len(text) == 7 and text.startswith("#")):
        return ""
    try:
        red, green, blue = (
            int(text[1:3], 16),
            int(text[3:5], 16),
            int(text[5:7], 16),
        )
    except ValueError:
        return ""
    best = ""
    best_distance = None
    for candidate, color in FOLDER_RGB_COLORS.items():
        cr, cg, cb = (
            int(candidate[1:3], 16),
            int(candidate[3:5], 16),
            int(candidate[5:7], 16),
        )
        distance = (red - cr) ** 2 + (green - cg) ** 2 + (blue - cb) ** 2
        if best_distance is None or distance < best_distance:
            best = color
            best_distance = distance
    return best


def copy_account_attributes(
    source_settings: Dict[str, Any],
    destination_session: ZimbraSession,
    destination_email: str,
    dry_run: bool,
    ignore: Optional[List[str]] = None,
    keep_admin_permissions: bool = False,
) -> Dict[str, Any]:
    result = {"updated": 0, "skipped": 0, "errors": []}
    patterns = [str(name).strip() for name in (ignore or []) if str(name).strip()]
    source_attrs = source_settings.get("account_attrs") or {}
    wanted = []
    for name, value in source_attrs.items():
        lowered = str(name).lower()
        if lowered in config_module.ALWAYS_IGNORE_ATTRS:
            continue
        if lowered.startswith("zimbrapref"):
            continue
        if lowered in {
            "zimbraisadminaccount",
            "zimbraisdelegatedadminaccount",
            "zimbraisexternalvirtualaccount",
        } and not keep_admin_permissions:
            continue
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        if any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns):
            continue
        wanted.append(str(name))
    try:
        payload = destination_session.request_single(
            "GetAccount",
            {
                "account": {"by": "name", "_content": destination_email},
                "applyCos": "0",
            },
        )
        account = unwrap(payload, "account")
        if not isinstance(account, dict):
            account = payload if isinstance(payload, dict) else {}
        destination_attrs = attrs_to_dict(account)
        account_id = str(account.get("id") or "")
    except Exception as exc:  # noqa: BLE001
        result["errors"].append("attributs: %s" % describe_error(exc))
        return result

    changes: Dict[str, Any] = {}
    destination_by_lower = {
        str(name).lower(): value for name, value in destination_attrs.items()
    }
    for name in wanted:
        value = source_attrs.get(name)
        if (
            name.lower() in destination_by_lower
            and _norm(destination_by_lower[name.lower()]) == _norm(value)
        ):
            result["skipped"] += 1
            continue
        changes[name] = value

    if not changes:
        return result
    if dry_run:
        result["updated"] = len(changes)
        return result
    if not account_id:
        result["errors"].append("attributs: identifiant destination introuvable")
        return result
    try:
        destination_session.request(
            "ModifyAccount",
            {"id": account_id, "a": a_entries(changes)},
        )
        result["updated"] = len(changes)
    except Exception as exc:  # noqa: BLE001
        result["errors"].append("attributs: %s" % describe_error(exc))
    return result


def copy_cos(
    source_settings: Dict[str, Any],
    destination_session: ZimbraSession,
    destination_email: str,
    dry_run: bool,
) -> Dict[str, Any]:
    result = {"updated": 0, "skipped": 0, "errors": []}
    cos_name = str(source_settings.get("cos_name") or "").strip()
    if not cos_name:
        result["skipped"] += 1
        return result
    try:
        cos_payload = destination_session.request_single(
            "GetCos",
            {"cos": {"by": "name", "_content": cos_name}},
        )
        cos_obj = unwrap(cos_payload, "cos")
        if not isinstance(cos_obj, dict) or not cos_obj.get("id"):
            result["errors"].append("COS destination introuvable : %s" % cos_name)
            return result
        destination_cos_id = str(cos_obj.get("id"))
        payload = destination_session.request_single(
            "GetAccount",
            {
                "account": {"by": "name", "_content": destination_email},
                "applyCos": "0",
            },
        )
        account = unwrap(payload, "account")
        if not isinstance(account, dict):
            account = payload if isinstance(payload, dict) else {}
        account_id = str(account.get("id") or "")
        current_cos_id = str(attrs_to_dict(account).get("zimbraCOSId") or "")
        if not account_id:
            result["errors"].append("compte destination introuvable : %s" % destination_email)
            return result
        if current_cos_id == destination_cos_id:
            result["skipped"] += 1
            return result
        if dry_run:
            result["updated"] += 1
            return result
        destination_session.request(
            "ModifyAccount",
            {
                "id": account_id,
                "a": [{"n": "zimbraCOSId", "_content": destination_cos_id}],
            },
        )
        result["updated"] += 1
    except Exception as exc:  # noqa: BLE001
        result["errors"].append("cos %s: %s" % (cos_name, describe_error(exc)))
    return result


def copy_folder_colors(
    source_settings: Dict[str, Any],
    destination,
    dry_run: bool,
) -> Dict[str, Any]:
    result = {"updated": 0, "skipped": 0, "errors": []}
    source_folders = source_settings.get("folders") or []
    if not source_folders:
        return result
    try:
        destination_folders = inventory_module.collect_folders(
            destination, colored_only=False
        )
    except Exception as exc:  # noqa: BLE001
        result["errors"].append("couleurs dossiers: %s" % describe_error(exc))
        return result

    existing = {
        folder.get("path"): folder
        for folder in destination_folders
        if folder.get("path")
    }
    for folder in source_folders:
        path = folder.get("path") or ""
        target = existing.get(path)
        if not target or not target.get("id"):
            result["skipped"] += 1
            continue
        rgb = folder.get("rgb") or ""
        color = folder.get("color") or ""
        if not color and rgb:
            color = _rgb_to_folder_color(rgb)
        if not color:
            result["skipped"] += 1
            continue
        if str(target.get("color") or "") == color:
            result["skipped"] += 1
            continue
        if dry_run:
            result["updated"] += 1
            continue
        action: Dict[str, Any] = {
            "id": target["id"],
            "op": "update",
            "color": color,
        }
        try:
            destination.request("FolderAction", {"action": action})
            result["updated"] += 1
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(
                "couleur dossier %s: %s" % (path, describe_error(exc))
            )
    return result


def copy_tags(
    source_settings: Dict[str, Any],
    destination,
    dry_run: bool,
) -> Dict[str, Any]:
    result = {"created": 0, "updated": 0, "skipped": 0, "errors": []}
    source_tags = source_settings.get("tags") or []
    if not source_tags:
        return result
    try:
        destination_tags = inventory_module.collect_tags(destination)
    except Exception as exc:  # noqa: BLE001
        result["errors"].append("tags: %s" % describe_error(exc))
        return result
    existing = {
        str(tag.get("name") or "").lower(): tag
        for tag in destination_tags
        if tag.get("name")
    }
    for tag in source_tags:
        name = str(tag.get("name") or "")
        if not name:
            continue
        key = name.lower()
        current = existing.get(key)
        if current is not None:
            source_color = str(tag.get("color") or "").strip()
            current_color = str(current.get("color") or "").strip()
            if (
                source_color
                and current_color
                and source_color != current_color
                and current.get("id")
            ):
                if dry_run:
                    result["updated"] += 1
                else:
                    try:
                        destination.request(
                            "TagAction",
                            {
                                "action": {
                                    "op": "color",
                                    "id": current.get("id"),
                                    "color": source_color,
                                }
                            },
                        )
                        current["color"] = source_color
                        result["updated"] += 1
                    except Exception as exc:  # noqa: BLE001
                        result["errors"].append(
                            "tag %s (couleur): %s" % (name, describe_error(exc))
                        )
            else:
                result["skipped"] += 1
            continue
        color = str(tag.get("color") or "").strip()
        if dry_run:
            result["created"] += 1
            existing[key] = {"name": name, "color": color}
            continue
        payload: Dict[str, Any] = {"name": name}
        if color:
            payload["color"] = color
        try:
            destination.request("CreateTag", {"tag": payload})
            existing[key] = {"name": name, "color": color}
            result["created"] += 1
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(
                "tag %s: %s" % (name, describe_error(exc))
            )
    return result


def copy_filters(
    source_settings: Dict[str, Any],
    destination,
    dry_run: bool,
    way: str = "in",
) -> Dict[str, Any]:
    result = {"updated": 0, "skipped": 0, "errors": []}
    key = "filters_in" if way == "in" else "filters_out"
    source_rules = [zobject_data(rule) for rule in source_settings.get(key, [])]
    if not source_rules:
        return result
    try:
        dest_rules = [zobject_data(rule) for rule in destination.get_filter_rules(way)]
    except Exception as exc:  # noqa: BLE001
        result["errors"].append("filtres %s: %s" % (way, describe_error(exc)))
        return result

    merged = list(dest_rules)
    positions = {
        rule.get("name"): index for index, rule in enumerate(merged) if rule.get("name")
    }
    changed = 0
    for rule in source_rules:
        name = rule.get("name")
        if not name:
            continue
        if name in positions:
            index = positions[name]
            if json.dumps(merged[index], sort_keys=True) == json.dumps(rule, sort_keys=True):
                result["skipped"] += 1
                continue
            merged[index] = rule
        else:
            positions[name] = len(merged)
            merged.append(rule)
        changed += 1

    if not changed:
        return result
    if dry_run:
        result["updated"] = changed
        return result
    command = "ModifyFilterRules" if way == "in" else "ModifyOutgoingFilterRules"
    try:
        destination.request(command, {"filterRules": {"filterRule": merged}})
        result["updated"] = changed
    except Exception as exc:  # noqa: BLE001
        result["errors"].append("filtres %s: %s" % (way, describe_error(exc)))
    return result


def copy_white_black_lists(
    source_settings: Dict[str, Any], destination, dry_run: bool
) -> Dict[str, Any]:
    result = {"white_added": 0, "black_added": 0, "errors": []}
    try:
        dest_lists = destination.get_white_black_lists() or {}
    except Exception as exc:  # noqa: BLE001
        result["errors"].append("listes blanches/noires: %s" % describe_error(exc))
        return result

    for source_key, dest_key, counter in (
        ("whitelist", "whiteList", "white_added"),
        ("blacklist", "blackList", "black_added"),
    ):
        current = {
            str(entry).lower()
            for entry in _address_values(dest_lists.get(dest_key))
        }
        additions = [
            address
            for address in source_settings.get(source_key, [])
            if str(address).lower() not in current
        ]
        if not additions:
            continue
        if dry_run:
            result[counter] += len(additions)
            continue
        try:
            destination.request(
                "ModifyWhiteBlackList",
                {
                    dest_key: {
                        "addr": [
                            {"op": "+", "_content": address}
                            for address in additions
                        ]
                    }
                },
            )
            result[counter] += len(additions)
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(
                "%s: %s" % (dest_key, describe_error(exc))
            )
    return result


def _address_values(node: Any) -> List[str]:
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


def resolve_folder_id(destination, path: str) -> str:
    payload = destination.request("GetFolder", {"folder": {"path": path}})
    folder = payload.get("folder") if isinstance(payload, dict) else None
    if isinstance(folder, dict):
        return str(folder.get("id") or "")
    return ""


def _grantee_exists(
    session: Optional[ZimbraSession], grantee: str, grantee_type: str
) -> bool:
    if session is None or grantee_type not in {"usr", "grp", "dom"}:
        return True
    selector = {"by": "name", "_content": grantee}
    try:
        if grantee_type == "usr":
            session.request_single("GetAccount", {"account": selector})
        elif grantee_type == "grp":
            session.request_single("GetDistributionList", {"dl": selector})
        else:
            session.request_single("GetDomain", {"domain": selector})
        return True
    except Exception:  # noqa: BLE001 - missing grantee must not break the copy
        return False


def copy_outgoing_shares(
    config: config_module.Config,
    source_settings: Dict[str, Any],
    destination,
    destination_email: str,
    dry_run: bool,
    options: Optional[Dict[str, Any]] = None,
    destination_session: Optional[ZimbraSession] = None,
    destination_mail=None,
) -> Dict[str, Any]:
    result = {
        "created": 0,
        "skipped": 0,
        "ignored": 0,
        "ignored_shares": [],
        "errors": [],
    }
    effective = options or config.sync()
    source_shares = source_settings.get("shares_out") or []
    if not source_shares:
        return result
    try:
        dest_shares = (
            inventory_module.collect_outgoing_shares(
                destination_mail, destination_email
            )
            if destination_mail is not None
            else []
        )
    except Exception as exc:  # noqa: BLE001
        result["errors"].append("partages sortants: %s" % describe_error(exc))
        return result

    existing = {
        (
            share.get("folderPath") or "",
            (share.get("granteeName") or "").lower(),
            share.get("rights") or "",
        )
        for share in dest_shares
    }

    for share in source_shares:
        path = share.get("folderPath") or ""
        grantee = share.get("granteeName") or ""
        grantee_type = share.get("granteeType") or "usr"
        rights = share.get("rights") or ""
        if not path or not rights:
            continue
        if grantee and "@" in grantee:
            grantee = _map_email(effective, grantee)
        if not _grantee_exists(destination_session, grantee, grantee_type):
            result["ignored"] += 1
            result["ignored_shares"].append(
                {"folder": path, "grantee": grantee, "type": grantee_type}
            )
            continue
        key = (path, grantee.lower(), rights)
        if key in existing:
            result["skipped"] += 1
            continue
        if dry_run:
            result["created"] += 1
            continue
        try:
            folder_id = resolve_folder_id(destination_mail or destination, path)
            if not folder_id:
                raise SettingsError("dossier destination introuvable : %s" % path)
            grant: Dict[str, Any] = {"perm": rights, "gt": grantee_type}
            if grantee_type not in {"pub", "all"} and grantee:
                grant["d"] = grantee
            (destination_mail or destination).request(
                "FolderAction",
                {"action": {"id": folder_id, "op": "grant", "grant": grant}},
            )
            existing.add(key)
            result["created"] += 1
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(
                "partage %s → %s : %s" % (path, grantee or grantee_type, describe_error(exc))
            )
    return result


def copy_permissions(
    source_settings: Dict[str, Any], destination, dry_run: bool
) -> Dict[str, Any]:
    result = {"created": 0, "skipped": 0, "errors": []}
    source_aces = [ace for ace in as_list(source_settings.get("permissions")) if isinstance(ace, dict)]
    if not source_aces:
        return result
    try:
        dest_payload = destination.request("GetPermission", {}) or {}
    except Exception as exc:  # noqa: BLE001
        result["errors"].append("droits: %s" % describe_error(exc))
        return result
    dest_aces = [ace for ace in as_list(dest_payload.get("ace")) if isinstance(ace, dict)]
    existing = {
        (
            ace.get("right") or "",
            (ace.get("d") or ace.get("zid") or "").lower(),
        )
        for ace in dest_aces
    }

    for ace in source_aces:
        right = ace.get("right") or ""
        grantee = ace.get("d") or ""
        grantee_type = ace.get("gt") or "usr"
        if not right:
            continue
        if not grantee:
            # An ACE identified only by zid cannot be mapped safely.
            result["skipped"] += 1
            continue
        key = (right, grantee.lower())
        if key in existing:
            result["skipped"] += 1
            continue
        if dry_run:
            result["created"] += 1
            continue
        try:
            destination.request(
                "GrantPermission",
                {"ace": {"gt": grantee_type, "right": right, "d": grantee}},
            )
            existing.add(key)
            result["created"] += 1
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(
                "droit %s → %s : %s" % (right, grantee, describe_error(exc))
            )
    return result


# ----------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------
def copy_account_settings(
    config: config_module.Config,
    plan: Dict[str, Any],
    progress: Progress = None,
    cancel: Optional[threading.Event] = None,
    only: Optional[List[str]] = None,
    dry_run: bool = False,
    options: Optional[Dict[str, Any]] = None,
    parts: Optional[List[str]] = None,
) -> Dict[str, Any]:
    sync = _options_with_defaults(config, plan, options)
    wanted_parts = set(normalize_parts(parts, sync))
    if parts:
        check = preflight(config, ("source", "destination"), check_rest=False)
        if not check.get("ok"):
            details = []
            for side, entry in check.get("sides", {}).items():
                for error in entry.get("errors", []):
                    details.append("%s : %s" % (side, error))
            raise SettingsError(
                "Preflight impossible avant la copie des reglages : "
                + " | ".join(details)
            )

    pairs = plan.get("accounts") or [
        {
            "source": mailbox.get("source", ""),
            "destination": mailbox.get("destination", ""),
        }
        for mailbox in plan.get("mailboxes", [])
    ]
    pairs = [
        pair
        for pair in pairs
        if pair.get("source")
        and pair.get("destination")
        and pair.get("status") != "conflict"
    ]
    if only:
        wanted = {value.lower() for value in only}
        pairs = [
            pair
            for pair in pairs
            if pair.get("source", "").lower() in wanted
            or pair.get("destination", "").lower() in wanted
        ]

    source = ZimbraSession(config, "source", readonly=True)
    destination = ZimbraSession(config, "destination", readonly=False)
    total = len(pairs)
    items: List[Dict[str, Any]] = []
    counters = {"ok": 0, "partial": 0, "failed": 0, "skipped": 0}

    for index, pair in enumerate(pairs, start=1):
        if cancel is not None and cancel.is_set():
            items.append(
                {
                    "source": pair.get("source"),
                    "destination": pair.get("destination"),
                    "status": "skipped",
                    "error": "Annulé par l'utilisateur",
                }
            )
            counters["skipped"] += 1
            continue

        source_email = pair.get("source", "")
        destination_email = pair.get("destination", "")
        if progress:
            progress(
                index - 1,
                total,
                "Réglages %d/%d : %s" % (index, total, source_email),
            )
        record: Dict[str, Any] = {
            "source": source_email,
            "destination": destination_email,
            "counts": {},
            "errors": [],
            "warnings": [],
        }

        try:
            settings = inventory_module.collect_account_settings(source, source_email)
            if settings.get("error"):
                raise SettingsError(settings["error"])
            destination_client = destination.account_client(destination_email)
            destination_mail = None
            if wanted_parts.intersection(
                {"filters", "permissions", "folders", "shares", "tags"}
            ):
                destination_mail = destination.mail_client(destination_email)

            counts: Dict[str, int] = {}
            errors: List[str] = []
            ignored_shares: List[Dict[str, Any]] = []
            signature_ids: Dict[str, str] = {}

            def merge(result: Dict[str, Any], prefix: str) -> None:
                for key, value in result.items():
                    if key == "errors":
                        errors.extend(value)
                    elif key == "ignored_shares":
                        ignored_shares.extend(value)
                    elif key == "id_map":
                        signature_ids.update(value)
                    elif isinstance(value, int):
                        counts["%s_%s" % (prefix, key)] = (
                            counts.get("%s_%s" % (prefix, key), 0) + value
                        )

            if "settings" in wanted_parts:
                merge(copy_signatures(settings, destination_client, dry_run), "signatures")
                merge(copy_identities(settings, destination_client, dry_run), "identities")
            if "prefs" in wanted_parts:
                merge(
                    copy_preferences(
                        config,
                        settings,
                        destination_client,
                        dry_run,
                        options=sync,
                        signature_id_map=signature_ids,
                    ),
                    "prefs",
                )
            if "folders" in wanted_parts:
                merge(
                    copy_folder_colors(
                        settings, destination_mail or destination_client, dry_run
                    ),
                    "folders",
                )
            if "tags" in wanted_parts:
                merge(
                    copy_tags(settings, destination_mail or destination_client, dry_run),
                    "tags",
                )
            if "cos" in wanted_parts:
                merge(
                    copy_cos(
                        settings,
                        destination,
                        destination_email,
                        dry_run,
                    ),
                    "cos",
                )
            if "attributes" in wanted_parts:
                merge(
                    copy_account_attributes(
                        settings,
                        destination,
                        destination_email,
                        dry_run,
                        ignore=sync.get("settings_attr_ignore") or [],
                        keep_admin_permissions=bool(
                            sync.get("keep_admin_permissions", False)
                        ),
                    ),
                    "account_attrs",
                )
            if "filters" in wanted_parts:
                filter_client = destination_mail or destination_client
                merge(copy_filters(settings, filter_client, dry_run, "in"), "filters_in")
                merge(copy_filters(settings, filter_client, dry_run, "out"), "filters_out")
            if "lists" in wanted_parts:
                merge(
                    copy_white_black_lists(settings, destination_client, dry_run),
                    "lists",
                )
            if "shares" in wanted_parts:
                merge(
                    copy_outgoing_shares(
                        config,
                        settings,
                        destination_client,
                        destination_email,
                        dry_run,
                        options=sync,
                        destination_session=destination,
                        destination_mail=destination_mail,
                    ),
                    "shares",
                )
            if "permissions" in wanted_parts:
                merge(
                    copy_permissions(
                        settings, destination_mail or destination_client, dry_run
                    ),
                    "permissions",
                )

            mountpoints = len(settings.get("mountpoints") or [])
            if mountpoints:
                record["warnings"].append(
                    "%d partage(s) entrant(s) non recréé(s) : à remonter manuellement "
                    "sur la destination." % mountpoints
                )
            record["counts"] = counts
            record["errors"] = errors
            record["ignored_shares"] = ignored_shares
            record["status"] = "partial" if errors else ("planned" if dry_run else "ok")
            counters["partial" if errors else "ok"] += 1
        except Exception as exc:  # noqa: BLE001
            record["status"] = "failed"
            record["error"] = describe_error(exc)
            counters["failed"] += 1
        record["finished_at"] = now_iso()
        items.append(record)
        if progress:
            progress(index, total, "%s : %s" % (destination_email, record.get("status")))

    ignored_share_count = sum(
        len(item.get("ignored_shares") or []) for item in items
    )
    report = {
        "created_at": now_iso(),
        "dry_run": dry_run,
        "ignored_shares": ignored_share_count,
        "profile": config.current_profile(),
        "profile_name": config.profile_name(),
        "source_host": config_module.store_host(config.connection("source")),
        "source_proxy_host": config_module.proxy_host(config.connection("source")),
        "destination_host": config_module.store_host(config.connection("destination")),
        "destination_proxy_host": config_module.proxy_host(config.connection("destination")),
        "total": total,
        "items": items,
        "summary": counters,
    }
    if not dry_run:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = config.storage_path("report_path") / ("settings-%s.json" % stamp)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=1, default=str)
            handle.write("\n")
        report["report_path"] = str(path)
    return report
