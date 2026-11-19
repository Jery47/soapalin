"""Folder creation, colors and destination folder resolution."""
from typing import Any, Dict

from .connections import describe_error
from .inventory import unwrap
from .inventory_folders import collect_folders
from .settings_common import _rgb_to_folder_color


def copy_folder_colors(
    source_settings: Dict[str, Any],
    destination,
    dry_run: bool,
) -> Dict[str, Any]:
    result: Dict[str, Any] = {"updated": 0, "skipped": 0, "errors": []}
    source_folders = source_settings.get("folders") or []
    if not source_folders:
        return result
    try:
        destination_folders = collect_folders(
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
    result: Dict[str, Any] = {"created": 0, "skipped": 0, "errors": []}
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
        destination_folders = collect_folders(
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


def resolve_folder_id(destination, path: str) -> str:
    payload = destination.request("GetFolder", {"folder": {"path": path}})
    folder = payload.get("folder") if isinstance(payload, dict) else None
    if isinstance(folder, dict):
        return str(folder.get("id") or "")
    return ""


