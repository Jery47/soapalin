"""Per-feature copy helpers for account settings."""

import fnmatch
import json
from typing import Any, Dict, List, Optional, Tuple

from . import config as config_module
from . import inventory as inventory_module
from .connections import ZimbraSession, describe_error
from .inventory import as_list, attrs_to_dict, unwrap, zobject_data
from .settings_common import (
    SettingsError,
    _ignored_pref,
    _map_email,
    _norm,
    _rgb_to_folder_color,
    a_entries,
    pref_entries,
)


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


def copy_empty_folders(
    source_settings: Dict[str, Any], destination, dry_run: bool
) -> Dict[str, Any]:
    """Create missing source folders on the destination, without messages."""
    result = {"created": 0, "skipped": 0, "errors": []}
    source_folders = (
        source_settings.get("all_folders")
        or source_settings.get("folders")
        or []
    )
    if not source_folders:
        return result
    try:
        payload = destination.request(
            "GetFolder", {"folder": {"path": "/"}, "depth": "20"}
        )
        root = unwrap(payload, "folder")
        if isinstance(root, list):
            root = root[0] if root else {}
        root_id = str((root or {}).get("id") or "")
        destination_folders = inventory_module.collect_folders(
            destination, colored_only=False
        )
    except Exception as exc:  # noqa: BLE001
        result["errors"].append("dossiers: %s" % describe_error(exc))
        return result

    existing = {"/": root_id} if root_id else {}
    for folder in destination_folders:
        path = folder.get("path") or ""
        if path:
            existing[path] = str(folder.get("id") or "")

    ordered = sorted(
        source_folders,
        key=lambda item: (str(item.get("path") or "").count("/"), item.get("path") or ""),
    )
    for folder in ordered:
        path = str(folder.get("path") or "")
        if not path or path == "/" or path in existing:
            result["skipped"] += 1
            continue
        parent_path = path.rsplit("/", 1)[0] or "/"
        parent_id = existing.get(parent_path) or ""
        if not parent_id:
            result["errors"].append(
                "dossier %s: parent %s absent" % (path, parent_path)
            )
            continue
        if dry_run:
            result["created"] += 1
            existing[path] = "__dry_run__"
            continue
        name = str(folder.get("name") or path.rsplit("/", 1)[-1])
        folder_payload = {"name": name, "l": parent_id}
        view = str(folder.get("view") or "")
        if view:
            folder_payload["view"] = view
        try:
            response = destination.request(
                "CreateFolder", {"folder": folder_payload}
            )
            created = unwrap(response, "folder")
            if isinstance(created, list):
                created = created[0] if created else {}
            folder_id = str((created or {}).get("id") or "")
            if not folder_id:
                folder_id = resolve_folder_id(destination, path)
            if not folder_id:
                result["errors"].append(
                    "dossier %s: identifiant introuvable apres creation" % path
                )
                continue
            existing[path] = folder_id
            result["created"] += 1
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(
                "dossier %s: %s" % (path, describe_error(exc))
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


def copy_incoming_shares(
    source_settings: Dict[str, Any],
    destination_session,
    destination_client,
    destination_email: str,
    dry_run: bool,
    options: Optional[Dict[str, Any]] = None,
    destination_mail=None,
) -> Dict[str, Any]:
    """Mount shares already accepted on the source for the destination user."""
    result = {"created": 0, "skipped": 0, "errors": []}
    source_shares = source_settings.get("mountpoints") or []
    if not source_shares:
        return result
    effective = options or {}
    target = destination_mail or destination_client
    try:
        from .apply import resolve_id

        root_payload = target.request(
            "GetFolder", {"folder": {"path": "/"}}
        )
        root = unwrap(root_payload, "folder")
        if isinstance(root, list):
            root = root[0] if root else {}
        root_id = str((root or {}).get("id") or "")
        if not root_id:
            raise SettingsError("dossier racine destination introuvable")
        destination_shares = destination_client.get_share_info()
    except Exception as exc:  # noqa: BLE001
        result["errors"].append("partages entrants: %s" % describe_error(exc))
        return result

    existing = {
        (
            str(share.get("ownerEmail") or "").lower(),
            str(share.get("folderPath") or ""),
        )
        for share in destination_shares
        if share.get("mid")
    }
    accepted = {}
    for share in source_shares:
        # ``mid`` is present for a share that is already mounted/accepted.
        if not share.get("mid"):
            result["skipped"] += 1
            continue
        owner_email = _map_email(effective, share.get("ownerEmail") or "")
        folder_path = str(share.get("folderPath") or "/")
        if not owner_email:
            result["skipped"] += 1
            continue
        key = (owner_email.lower(), folder_path)
        grantee_type = str(share.get("granteeType") or "usr")
        grantee = _map_email(effective, share.get("granteeName") or "")
        priority = (
            2
            if grantee_type == "usr"
            and grantee
            and grantee.lower() == destination_email.lower()
            else (1 if grantee_type == "grp" else 0)
        )
        current = accepted.get(key)
        if current is None or priority > current[0]:
            accepted[key] = (priority, share)
    owner_clients = {}

    def owner_client(email):
        if email not in owner_clients:
            owner_clients[email] = destination_session.mail_client(email)
        return owner_clients[email]

    for key, (_, share) in accepted.items():
        owner_email = _map_email(effective, share.get("ownerEmail") or "")
        folder_path = str(share.get("folderPath") or "/")
        if key in existing:
            result["skipped"] += 1
            continue
        if dry_run:
            result["created"] += 1
            existing.add(key)
            continue
        try:
            owner_id = resolve_id(destination_session, "account", owner_email)
            payload = owner_client(owner_email).request(
                "GetFolder", {"folder": {"path": folder_path}}
            )
            folder = unwrap(payload, "folder")
            if isinstance(folder, list):
                folder = folder[0] if folder else {}
            folder_id = str((folder or {}).get("id") or "1")
            name = str(
                share.get("name")
                or folder_path.rstrip("/").rsplit("/", 1)[-1]
                or share.get("ownerName")
                or owner_email
            )
            if not name or name == "/":
                name = owner_email
            view = str(share.get("view") or "")
            rights = str(share.get("rights") or share.get("perm") or "")
            link = {
                "name": name,
                "l": root_id,
                "rid": folder_id,
                "zid": owner_id,
            }
            if view and view != "unknown":
                link["view"] = view
            if rights:
                link["perm"] = rights
            target.create_mountpoint(**link)
            existing.add(key)
            result["created"] += 1
        except Exception as exc:  # noqa: BLE001
            text = describe_error(exc).lower()
            if "already_exists" in text or "already exists" in text:
                existing.add(key)
                result["skipped"] += 1
            else:
                result["errors"].append(
                    "partage entrant %s / %s : %s"
                    % (owner_email, folder_path, describe_error(exc))
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


