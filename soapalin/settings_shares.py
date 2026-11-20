"""Outgoing and incoming share/mountpoint copy helpers."""
from typing import Any, Dict, Optional, Tuple

from . import config as config_module
from .connections import ZimbraSession, describe_error
from .inventory import unwrap
from .inventory_folders import collect_mountpoints, collect_outgoing_shares
from .settings_common import SettingsError, _map_email
from .settings_folders import resolve_folder_id


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
    result: Dict[str, Any] = {
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
            collect_outgoing_shares(
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

    folder_cache: Dict[str, str] = {}
    grantee_cache: Dict[Tuple[str, str], bool] = {}
    for share in source_shares:
        path = share.get("folderPath") or ""
        grantee = share.get("granteeName") or ""
        grantee_type = share.get("granteeType") or "usr"
        rights = share.get("rights") or ""
        if not path or not rights:
            continue
        if grantee and "@" in grantee:
            grantee = _map_email(effective, grantee)
        grantee_key = (grantee.lower(), grantee_type)
        grantee_ok = grantee_cache.get(grantee_key)
        if grantee_ok is None:
            grantee_ok = _grantee_exists(
                destination_session, grantee, grantee_type
            )
            grantee_cache[grantee_key] = grantee_ok
        if not grantee_ok:
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
            folder_id = folder_cache.get(path)
            if not folder_id:
                folder_id = resolve_folder_id(destination_mail or destination, path)
                if folder_id:
                    folder_cache[path] = folder_id
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
    result: Dict[str, Any] = {"created": 0, "updated": 0, "skipped": 0, "errors": []}
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

    try:
        destination_links = collect_mountpoints(target)
    except Exception as exc:  # noqa: BLE001
        result["errors"].append("partages entrants: %s" % describe_error(exc))
        return result
    links_by_id = {
        str(link.get("id")): link for link in destination_links if link.get("id")
    }
    existing = {}
    for share in destination_shares:
        if not share.get("mid"):
            continue
        link = links_by_id.get(str(share.get("mid") or ""))
        if link is not None:
            existing[
                (
                    str(share.get("ownerEmail") or "").lower(),
                    str(share.get("folderPath") or ""),
                    str(link.get("path") or ""),
                )
            ] = link
    accepted: Dict[Tuple[str, str], Tuple[int, Dict[str, Any]]] = {}
    for share in source_shares:
        # ``mid`` is present for a share that is already mounted/accepted.
        # ``mountPath`` is only set when the mountpoint is actually present in
        # the source mailbox tree. GetShareInfo can also return inherited or
        # stale shares that must not be recreated on the destination.
        if not share.get("mid"):
            result["skipped"] += 1
            continue
        mount_path = str(share.get("mountPath") or "")
        if not mount_path or mount_path == "/":
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
    owner_ids: Dict[str, str] = {}
    owner_folders: Dict[Tuple[str, str], str] = {}
    parent_folders: Dict[str, str] = {}

    def owner_client(email):
        if email not in owner_clients:
            owner_clients[email] = destination_session.mail_client(email)
        return owner_clients[email]

    for key, (_, share) in accepted.items():
        owner_email = _map_email(effective, share.get("ownerEmail") or "")
        folder_path = str(share.get("folderPath") or "/")
        mount_path = str(share.get("mountPath") or "/")
        mount_key = (key[0], key[1], mount_path)
        desired_color = str(share.get("mountColor") or "").strip()
        desired_rgb = str(share.get("mountRgb") or "").strip()
        desired_reminder = str(share.get("mountReminder") or "").strip()
        current_link = existing.get(mount_key)
        if current_link is not None:
            current_color = str(current_link.get("color") or "").strip()
            current_rgb = str(current_link.get("rgb") or "").strip()
            current_reminder = str(current_link.get("reminder") or "0").strip()
            color_changed = (
                (desired_rgb and current_rgb != desired_rgb)
                or (
                    not desired_rgb
                    and desired_color
                    and current_color != desired_color
                )
            )
            reminder_changed = (
                desired_reminder in {"0", "1"}
                and current_reminder != desired_reminder
            )
            if not color_changed and not reminder_changed:
                result["skipped"] += 1
                continue
            if dry_run:
                result["updated"] += 1
                continue
            try:
                if color_changed:
                    action = {
                        "op": "color",
                        "id": current_link.get("id"),
                    }
                    if desired_rgb:
                        action["rgb"] = desired_rgb
                    else:
                        action["color"] = desired_color
                    target.request("FolderAction", {"action": action})
                    current_link["color"] = desired_color
                    current_link["rgb"] = desired_rgb
                if reminder_changed:
                    target.request(
                        "EnableSharedReminder",
                        {
                            "link": {
                                "id": current_link.get("id"),
                                "reminder": desired_reminder,
                            }
                        },
                    )
                    current_link["reminder"] = desired_reminder
                result["updated"] += 1
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(
                    "mise a jour montage %s : %s"
                    % (mount_path, describe_error(exc))
                )
            continue
        if dry_run:
            result["created"] += 1
            existing[mount_key] = {"id": ""}
            continue
        try:
            owner_id = owner_ids.get(owner_email)
            if not owner_id:
                owner_id = resolve_id(
                    destination_session, "account", owner_email
                )
                owner_ids[owner_email] = owner_id
            folder_key = (owner_email, folder_path)
            folder_id = owner_folders.get(folder_key)
            if not folder_id:
                payload = owner_client(owner_email).request(
                    "GetFolder", {"folder": {"path": folder_path}}
                )
                folder = unwrap(payload, "folder")
                if isinstance(folder, list):
                    folder = folder[0] if folder else {}
                folder_id = str((folder or {}).get("id") or "1")
                owner_folders[folder_key] = folder_id
            parent_path = mount_path.rsplit("/", 1)[0] or "/"
            parent_id = parent_folders.get(parent_path)
            if not parent_id:
                parent_payload = target.request(
                    "GetFolder", {"folder": {"path": parent_path}}
                )
                parent = unwrap(parent_payload, "folder")
                if isinstance(parent, list):
                    parent = parent[0] if parent else {}
                parent_id = str((parent or {}).get("id") or root_id)
                parent_folders[parent_path] = parent_id
            name = str(
                share.get("mountName")
                or share.get("name")
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
                "l": parent_id,
                "rid": folder_id,
                "zid": owner_id,
            }
            if view and view != "unknown":
                link["view"] = view
            if rights:
                link["perm"] = rights
            if desired_rgb:
                link["rgb"] = desired_rgb
            elif desired_color:
                link["color"] = desired_color
            if desired_reminder in {"0", "1"}:
                link["reminder"] = desired_reminder
            target.create_mountpoint(**link)
            existing[mount_key] = {"id": ""}
            result["created"] += 1
        except Exception as exc:  # noqa: BLE001
            text = describe_error(exc).lower()
            if "already_exists" in text or "already exists" in text:
                existing[mount_key] = {}
                result["skipped"] += 1
            else:
                result["errors"].append(
                    "partage entrant %s / %s : %s"
                    % (owner_email, folder_path, describe_error(exc))
                )
    return result


