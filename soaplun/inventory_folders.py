"""Folder, color, mountpoint and outgoing-share collectors."""
from typing import Any, Dict, List


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _walk_folders(node: Any) -> List[Dict[str, Any]]:
    folders: List[Dict[str, Any]] = []
    if not isinstance(node, dict):
        return folders
    folders.append(node)
    for child in _as_list(node.get("folder")):
        folders.extend(_walk_folders(child))
    return folders


def _folder_record(folder: Dict[str, Any]) -> Dict[str, Any]:
    color = folder.get("color")
    rgb = folder.get("rgb")
    return {
        "path": str(folder.get("absFolderPath") or ""),
        "id": str(folder.get("id") or ""),
        "name": str(folder.get("name") or ""),
        "view": str(folder.get("view") or ""),
        "color": str(color) if color not in {None, ""} else "",
        "rgb": str(rgb) if rgb not in {None, ""} else "",
        "acl": folder.get("acl") or {},
    }


def collect_folders(
    client: Any, colored_only: bool = True, include_root: bool = False
) -> List[Dict[str, Any]]:
    payload = client.request(
        "GetFolder", {"folder": {"path": "/"}, "depth": "20"}
    )
    root = payload.get("folder") if isinstance(payload, dict) else None
    folders: List[Dict[str, Any]] = []
    for folder in _walk_folders(root):
        record = _folder_record(folder)
        path = record["path"]
        if not path:
            continue
        if path == "/" and not include_root:
            continue
        if colored_only and not record["color"] and not record["rgb"]:
            continue
        folders.append(record)
    return folders


def collect_folder_colors(client: Any) -> List[Dict[str, Any]]:
    return collect_folders(client, colored_only=True)


def _walk_tree(node: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if not isinstance(node, dict):
        return items
    items.append(node)
    for child in _as_list(node.get("folder")) + _as_list(node.get("link")):
        items.extend(_walk_tree(child))
    return items


def _mountpoint_records(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for node in nodes:
        path = str(node.get("absFolderPath") or "")
        if not path or path == "/" or not (node.get("rid") or node.get("zid")):
            continue
        items.append(
            {
                "path": path,
                "name": str(node.get("name") or ""),
                "id": str(node.get("id") or ""),
                "rid": str(node.get("rid") or ""),
                "zid": str(node.get("zid") or ""),
                "view": str(node.get("view") or ""),
                "perm": str(node.get("perm") or ""),
                "color": str(node.get("color") or ""),
                "rgb": str(node.get("rgb") or ""),
            }
        )
    return items


def collect_mountpoints(client: Any) -> List[Dict[str, Any]]:
    """Return accepted share mountpoints with their local folder path."""
    payload = client.request(
        "GetFolder", {"folder": {"path": "/"}, "depth": "20"}
    )
    root = payload.get("folder") if isinstance(payload, dict) else None
    return _mountpoint_records(_walk_tree(root))


def _share_records(
    nodes: List[Dict[str, Any]], owner_email: str = ""
) -> List[Dict[str, Any]]:
    shares: List[Dict[str, Any]] = []
    seen = set()
    for node in nodes:
        # Links are mountpoints, never outgoing ACL owners.
        if node.get("rid") or node.get("zid"):
            continue
        folder = _folder_record(node)
        path = folder["path"]
        if not path:
            continue
        grants = _as_list((folder.get("acl") or {}).get("grant"))
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
            key = (path, grantee.lower(), grantee_type, rights)
            if key in seen:
                continue
            seen.add(key)
            shares.append(
                {
                    "folderPath": path,
                    "granteeName": grantee,
                    "granteeType": grantee_type,
                    "rights": rights,
                }
            )
    return shares


def collect_outgoing_shares(
    client: Any, owner_email: str = ""
) -> List[Dict[str, Any]]:
    payload = client.request(
        "GetFolder", {"folder": {"path": "/"}, "depth": "20"}
    )
    root = payload.get("folder") if isinstance(payload, dict) else None
    return _share_records(_walk_tree(root), owner_email)


def collect_folder_bundle(
    client: Any, owner_email: str = ""
) -> Dict[str, Any]:
    """Collect folder data, colors, shares and mountpoints in one request."""
    payload = client.request(
        "GetFolder", {"folder": {"path": "/"}, "depth": "20"}
    )
    root = payload.get("folder") if isinstance(payload, dict) else None
    nodes = _walk_tree(root)
    regular = [node for node in nodes if not (node.get("rid") or node.get("zid"))]
    all_folders: List[Dict[str, Any]] = []
    colored: List[Dict[str, Any]] = []
    for node in regular:
        record = _folder_record(node)
        if not record["path"] or record["path"] == "/":
            continue
        all_folders.append(record)
        if record["color"] or record["rgb"]:
            colored.append(record)
    return {
        "folders": colored,
        "all_folders": all_folders,
        "shares_out": _share_records(nodes, owner_email),
        "mountpoints": _mountpoint_records(nodes),
    }


