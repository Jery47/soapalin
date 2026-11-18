"""Snapshot persistence, indexing, cache and cleanup."""

import datetime
import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from . import config as config_module
from . import storage
from .schema import validate_snapshot

logger = logging.getLogger(__name__)


SNAPSHOT_KEEP = 20


_INDEX_LOCK = threading.RLock()


_INDEX_CACHE: Dict[str, Dict[str, Any]] = {}


_INDEX_CACHE_LIMIT = 64


def _is_index_file(path: Path) -> bool:
    return path.name.endswith(".index.json")


def snapshot_index_path(path: Union[Path, str]) -> Path:
    path = Path(path)
    if path.name.endswith(".json"):
        return path.with_name(path.name[:-5] + ".index.json")
    return path.with_suffix(".index.json")


def _profile_slug(profile_id: str) -> str:
    value = "".join(
        char if char.isalnum() or char in "-_" else "-" for char in profile_id
    )
    return value or "default"


def snapshot_dir(config: config_module.Config) -> Path:
    return config.storage_path("snapshot_path")


def snapshot_path(
    config: config_module.Config,
    side: str,
    stamp: Optional[str] = None,
    profile: Optional[str] = None,
) -> Path:
    stamp = stamp or datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    profile = profile or config.current_profile()
    return snapshot_dir(config) / (
        "%s-%s-%s.json" % (side, _profile_slug(profile), stamp)
    )


def write_snapshot(
    config: config_module.Config,
    side: str,
    payload: Dict[str, Any],
    profile: Optional[str] = None,
) -> Path:
    profile = profile or config.current_profile()
    path = snapshot_path(config, side, profile=profile)
    payload = dict(payload)
    payload["profile"] = profile
    payload["profile_name"] = config.profile_name(profile)
    payload["path"] = str(path)
    storage.write_json_atomic(path, payload, indent=1)
    write_snapshot_index(path, payload)
    return path


def build_snapshot_index(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    accounts = []
    for account in snapshot.get("accounts", []):
        name = account.get("name", "")
        if not name or account.get("is_system"):
            continue
        accounts.append(
            {
                "name": name,
                "domain": account.get("domain", ""),
                "is_admin": bool(account.get("is_admin")),
                "is_system": False,
            }
        )
    accounts.sort(key=lambda item: item["name"].lower())

    def entries(items, alias=False):
        result = []
        for item in items or []:
            name = item.get("name", "")
            if not name:
                continue
            entry = {"name": name, "id": item.get("id", "")}
            if alias:
                entry["is_alias"] = bool(item.get("is_alias"))
            result.append(entry)
        return sorted(result, key=lambda value: value["name"].lower())

    return {
        "version": 1,
        "index": True,
        "side": snapshot.get("side", ""),
        "profile": snapshot.get("profile", ""),
        "profile_name": snapshot.get("profile_name", ""),
        "host": snapshot.get("host", ""),
        "path": snapshot.get("path", ""),
        "created_at": snapshot.get("created_at", ""),
        "counts": snapshot.get("counts", {}),
        "accounts": accounts,
        "domains": entries(snapshot.get("domains"), alias=True),
        "cos": entries(snapshot.get("cos")),
        "distribution_lists": entries(snapshot.get("distribution_lists")),
        "resources": entries(snapshot.get("resources")),
    }


def write_snapshot_index(snapshot_path: Path, snapshot: Dict[str, Any]) -> Path:
    index_path = snapshot_index_path(snapshot_path)
    storage.write_json_atomic(
        index_path, build_snapshot_index(snapshot), indent=None
    )
    return index_path


def _index_cache_key(path: Path):
    stat = path.stat()
    return (str(path), stat.st_mtime_ns, stat.st_size)


def load_snapshot_index(snapshot_path: Union[Path, str]) -> Dict[str, Any]:
    snapshot_path = Path(snapshot_path)
    index_path = snapshot_index_path(snapshot_path)
    with _INDEX_LOCK:
        key = None
        if index_path.exists():
            try:
                key = _index_cache_key(index_path)
            except OSError:
                key = None
            if key and key in _INDEX_CACHE:
                return _INDEX_CACHE[key]
        if not index_path.exists():
            snapshot = load_snapshot(snapshot_path)
            write_snapshot_index(snapshot_path, snapshot)
        with index_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not data.get("path"):
            data["path"] = str(snapshot_path)
        key = _index_cache_key(index_path)
        if len(_INDEX_CACHE) >= _INDEX_CACHE_LIMIT:
            _INDEX_CACHE.clear()
        _INDEX_CACHE[key] = data
        return data


def latest_snapshot_index(
    config: config_module.Config,
    side: str,
    profile: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    wanted_profile = profile or config.current_profile()
    default_profile = config.default_profile()
    files = sorted(snapshot_dir(config).glob(side + "-*.json"), reverse=True)
    for path in files:
        if _is_index_file(path):
            continue
        try:
            index = load_snapshot_index(path)
        except (OSError, ValueError):
            continue
        if (index.get("profile") or default_profile) == wanted_profile:
            return index
    return None


def list_snapshots(
    config: config_module.Config,
    side: Optional[str] = None,
    profile: Optional[str] = None,
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    wanted_profile = profile or config.current_profile()
    default_profile = config.default_profile()
    for path in sorted(snapshot_dir(config).glob("*.json"), reverse=True):
        if _is_index_file(path):
            continue
        if side and not path.name.startswith(side + "-"):
            continue
        try:
            index = load_snapshot_index(path)
        except (OSError, ValueError):
            continue
        if (index.get("profile") or default_profile) != wanted_profile:
            continue
        counts = index.get("counts") or {}
        items.append(
            {
                "path": str(path),
                "name": path.name,
                "side": index.get("side", ""),
                "profile": index.get("profile") or default_profile,
                "profile_name": index.get("profile_name", ""),
                "created_at": index.get("created_at", ""),
                "host": index.get("host", ""),
                "counts": {
                    "domains": counts.get("domains", len(index.get("domains", []))),
                    "cos": counts.get("cos", len(index.get("cos", []))),
                    "accounts": counts.get(
                        "accounts", len(index.get("accounts", []))
                    ),
                    "distribution_lists": counts.get(
                        "distribution_lists",
                        len(index.get("distribution_lists", [])),
                    ),
                    "resources": counts.get(
                        "resources", len(index.get("resources", []))
                    ),
                },
            }
        )
    return items


def load_snapshot(path: Union[Path, str]) -> Dict[str, Any]:
    path = Path(path)
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("Invalid snapshot file: %s" % path)
    validate_snapshot(payload)
    payload.setdefault("path", str(path))
    return payload


def latest_snapshot(
    config: config_module.Config,
    side: str,
    profile: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    wanted_profile = profile or config.current_profile()
    default_profile = config.default_profile()
    files = sorted(snapshot_dir(config).glob(side + "-*.json"), reverse=True)
    for path in files:
        try:
            snapshot = load_snapshot(path)
        except OSError:
            continue
        except ValueError as exc:
            logger.warning("snapshot ignore (%s) : %s", path, exc)
            continue
        if (snapshot.get("profile") or default_profile) == wanted_profile:
            return snapshot
    return None


def cleanup_snapshots(
    config: config_module.Config, keep_per_side: int = SNAPSHOT_KEEP
) -> List[str]:
    """Delete old snapshots, keeping the newest ones per side and profile."""
    removed: List[str] = []
    directory = snapshot_dir(config)
    default_profile = config.default_profile()
    for side in ("source", "destination"):
        groups: Dict[str, List[Path]] = {}
        for path in sorted(
            directory.glob(side + "-*.json"), key=lambda item: item.name, reverse=True
        ):
            if _is_index_file(path):
                continue
            try:
                index = load_snapshot_index(path)
            except (OSError, ValueError):
                index = {}
            profile = index.get("profile") or default_profile
            groups.setdefault(profile, []).append(path)
        for files in groups.values():
            keep = {str(path) for path in files[: max(0, int(keep_per_side))]}
            for path in files:
                if str(path) in keep:
                    continue
                try:
                    path.unlink()
                    index_path = snapshot_index_path(path)
                    if index_path.exists():
                        index_path.unlink()
                    removed.append(str(path))
                except OSError:
                    continue
    return removed


