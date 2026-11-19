"""Per-feature copy helpers for account settings."""

import fnmatch
import json
from typing import Any, Dict, List, Optional, Tuple

from . import config as config_module
from . import inventory as inventory_module
from .connections import ZimbraSession, describe_error
from .inventory import as_list, attrs_to_dict, unwrap, zobject_data
from .settings_common import (
    _ignored_pref,
    _norm,
    a_entries,
    pref_entries,
)


def copy_signatures(source_settings: Dict[str, Any], destination, dry_run: bool) -> Dict[str, Any]:
    result: Dict[str, Any] = {"created": 0, "skipped": 0, "errors": [], "id_map": {}}
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
    result: Dict[str, Any] = {"created": 0, "skipped": 0, "errors": []}
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
    result: Dict[str, Any] = {"updated": 0, "skipped": 0, "errors": []}
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


def copy_account_attributes(
    source_settings: Dict[str, Any],
    destination_session: ZimbraSession,
    destination_email: str,
    dry_run: bool,
    ignore: Optional[List[str]] = None,
    keep_admin_permissions: bool = False,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {"updated": 0, "skipped": 0, "errors": []}
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
    result: Dict[str, Any] = {"updated": 0, "skipped": 0, "errors": []}
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


def copy_tags(
    source_settings: Dict[str, Any],
    destination,
    dry_run: bool,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {"created": 0, "updated": 0, "skipped": 0, "errors": []}
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
        source_color = str(tag.get("color") or "").strip()
        source_rgb = str(tag.get("rgb") or "").strip()
        if current is not None:
            current_color = str(current.get("color") or "").strip()
            current_rgb = str(current.get("rgb") or "").strip()
            changed = (
                (source_rgb and current_rgb != source_rgb)
                or (
                    not source_rgb
                    and source_color
                    and current_color != source_color
                )
            )
            if changed and current.get("id"):
                if dry_run:
                    result["updated"] += 1
                else:
                    action = {
                        "op": "color",
                        "id": current.get("id"),
                    }
                    if source_rgb:
                        action["rgb"] = source_rgb
                    else:
                        action["color"] = source_color
                    try:
                        destination.request(
                            "TagAction", {"action": action}
                        )
                        current["color"] = source_color
                        current["rgb"] = source_rgb
                        result["updated"] += 1
                    except Exception as exc:  # noqa: BLE001
                        result["errors"].append(
                            "tag %s (couleur): %s" % (name, describe_error(exc))
                        )
            else:
                result["skipped"] += 1
            continue
        if dry_run:
            result["created"] += 1
            existing[key] = {
                "name": name,
                "color": source_color,
                "rgb": source_rgb,
            }
            continue
        payload: Dict[str, Any] = {"name": name}
        if source_rgb:
            payload["rgb"] = source_rgb
        elif source_color:
            payload["color"] = source_color
        try:
            destination.request("CreateTag", {"tag": payload})
            existing[key] = {
                "name": name,
                "color": source_color,
                "rgb": source_rgb,
            }
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
    result: Dict[str, Any] = {"updated": 0, "skipped": 0, "errors": []}
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
    result: Dict[str, Any] = {"white_added": 0, "black_added": 0, "errors": []}
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


def copy_permissions(
    source_settings: Dict[str, Any], destination, dry_run: bool
) -> Dict[str, Any]:
    result: Dict[str, Any] = {"created": 0, "skipped": 0, "errors": []}
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


