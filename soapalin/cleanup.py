"""Manual cleanup of temporary migration data.

``clean`` only deletes data: it never moves or archives anything.  The
``--directly-clean`` run option uses :func:`move_account_archives` to move
per-account temporary archives to ``data/trash`` during a migration.
"""

import datetime
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import config as config_module
from . import storage

TERMINAL_JOB_STATUSES = {"succeeded", "failed", "stopped", "interrupted"}
ALL_TARGETS = ("mailboxes", "jobs", "reports", "snapshots")
CLEAN_TARGETS = ALL_TARGETS + ("trash",)
REPORT_PATTERNS = (
    "apply-*.json",
    "settings-*.json",
    "mailboxes-*.json",
    "passwords-*.csv",
    "interrupted-*.json",
)


@dataclass
class CleanupItem:
    path: Path
    kind: str
    size: int


@dataclass
class CleanupPlan:
    items: List[CleanupItem] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(item.size for item in self.items)


def safe_filename(value: str) -> str:
    return "".join(
        char if char.isalnum() or char in "@._-" else "_" for char in value
    )


def format_bytes(value: int) -> str:
    size = float(value or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return "%.1f %s" % (size, unit)
        size /= 1024
    return "%.1f TiB" % size


def _cutoff(older_than_days: int) -> float:
    days = max(0, int(older_than_days or 0))
    return datetime.datetime.now().timestamp() - days * 86400


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for child in path.rglob("*"):
            try:
                if child.is_file():
                    total += child.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _parse_time(value: str) -> float:
    try:
        return datetime.datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _is_old(path: Path, cutoff: float, created_at: str = "") -> bool:
    if created_at:
        return _parse_time(created_at) < cutoff
    return _mtime(path) < cutoff


def _add(plan: CleanupPlan, path: Path, kind: str) -> None:
    plan.items.append(
        CleanupItem(path=path, kind=kind, size=_dir_size(path) if path.is_dir() else _mtime_size(path))
    )


def _mtime_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _mailbox_run_dirs(config: config_module.Config) -> List[Path]:
    root = config.storage_path("mailbox_path")
    if not root.exists():
        return []
    return sorted(
        (path for path in root.glob("run-*") if path.is_dir()),
        key=_mtime,
        reverse=True,
    )


def active_run_dirs(config: config_module.Config) -> Set[str]:
    """Return run directories that must not be deleted."""
    from .jobs import JobStore

    try:
        states = JobStore(config).list()
    except (OSError, ValueError):
        return set()
    active = [
        state
        for state in states
        if state.get("status") not in TERMINAL_JOB_STATUSES
    ]
    protected = {
        str(Path(str(state["run_dir"])))
        for state in active
        if state.get("run_dir")
    }
    if active:
        run_dirs = _mailbox_run_dirs(config)
        if run_dirs:
            protected.add(str(run_dirs[0]))
    return protected


def _normalize_targets(targets: Optional[Sequence[str]]) -> List[str]:
    values = [str(value).strip().lower() for value in (targets or []) if str(value).strip()]
    if not values:
        return ["mailboxes"]
    if "all" in values:
        return list(ALL_TARGETS)
    unknown = sorted(set(values) - set(CLEAN_TARGETS))
    if unknown:
        raise ValueError(
            "cible inconnue : %s (valeurs : %s)"
            % (", ".join(unknown), ", ".join(CLEAN_TARGETS + ("all",)))
        )
    return values


def _plan_mailboxes(
    config: config_module.Config, plan: CleanupPlan, cutoff: float
) -> None:
    protected = active_run_dirs(config)
    for path in _mailbox_run_dirs(config):
        if str(path) in protected:
            plan.skipped.append("run actif conserve : %s" % path)
            continue
        if _is_old(path, cutoff):
            _add(plan, path, "mailboxes")
    root = config.storage_path("mailbox_path")
    if not root.exists():
        return
    for pattern in ("*.tgz", "*.part", "*.tmp"):
        for path in root.glob(pattern):
            if path.is_file() and _is_old(path, cutoff):
                _add(plan, path, "mailboxes")


def _plan_jobs(config: config_module.Config, plan: CleanupPlan, cutoff: float) -> None:
    from .jobs import JobStore

    store = JobStore(config)
    for state in store.list():
        job_id = str(state.get("id") or "")
        if not job_id:
            continue
        if state.get("status") not in TERMINAL_JOB_STATUSES:
            plan.skipped.append("job actif conserve : %s" % job_id)
            continue
        path = store.directory / job_id
        if not path.exists():
            continue
        if _is_old(path, cutoff, str(state.get("created_at") or "")):
            _add(plan, path, "jobs")


def _report_files(config: config_module.Config) -> List[Path]:
    root = config.storage_path("report_path")
    if not root.exists():
        return []
    files: List[Path] = []
    for pattern in REPORT_PATTERNS:
        files.extend(path for path in root.glob(pattern) if path.is_file())
    return files


def _plan_reports(
    config: config_module.Config,
    plan: CleanupPlan,
    cutoff: float,
    keep_last: int,
) -> None:
    files = sorted(_report_files(config), key=_mtime, reverse=True)
    keep = max(0, int(keep_last or 0))
    for index, path in enumerate(files):
        if index < keep:
            continue
        if _is_old(path, cutoff):
            _add(plan, path, "reports")
    root = config.storage_path("report_path")
    backups = root / "backups"
    if backups.exists():
        for path in backups.iterdir():
            if _is_old(path, cutoff):
                _add(plan, path, "reports")


def _snapshot_groups(
    config: config_module.Config,
) -> Dict[Tuple[str, str], List[Tuple[Path, Optional[Path]]]]:
    root = config.storage_path("snapshot_path")
    if not root.exists():
        return {}
    groups: Dict[Tuple[str, str], List[Tuple[Path, Optional[Path]]]] = {}
    for path in sorted(root.glob("*.json")):
        if path.name.endswith(".index.json"):
            continue
        side = path.name.split("-", 1)[0]
        payload = storage.read_json(path)
        profile = str(payload.get("profile") or "default")
        index_path = path.with_name(path.name[:-5] + ".index.json")
        groups.setdefault((side, profile), []).append(
            (path, index_path if index_path.exists() else None)
        )
    return groups


def _plan_snapshots(
    config: config_module.Config,
    plan: CleanupPlan,
    cutoff: float,
    keep_last: int,
) -> None:
    keep = max(0, int(keep_last or 0))
    for entries in _snapshot_groups(config).values():
        entries.sort(key=lambda item: item[0].name, reverse=True)
        for index, (snapshot, index_path) in enumerate(entries):
            if index < keep:
                continue
            if _is_old(snapshot, cutoff):
                _add(plan, snapshot, "snapshots")
                if index_path is not None:
                    _add(plan, index_path, "snapshots")


def _plan_trash(config: config_module.Config, plan: CleanupPlan, cutoff: float) -> None:
    root = config.storage_path("trash_path")
    if not root.exists():
        return
    for path in root.iterdir():
        if _is_old(path, cutoff):
            _add(plan, path, "trash")


def plan_cleanup(
    config: config_module.Config,
    targets: Optional[Sequence[str]] = None,
    older_than_days: int = 0,
    keep_last: int = 0,
) -> CleanupPlan:
    """Compute what ``clean`` would delete, without touching the filesystem."""
    selected = _normalize_targets(targets)
    cutoff = _cutoff(older_than_days)
    plan = CleanupPlan()
    if "mailboxes" in selected:
        _plan_mailboxes(config, plan, cutoff)
    if "jobs" in selected:
        _plan_jobs(config, plan, cutoff)
    if "reports" in selected:
        _plan_reports(config, plan, cutoff, keep_last)
    if "snapshots" in selected:
        _plan_snapshots(config, plan, cutoff, keep_last)
    if "trash" in selected:
        _plan_trash(config, plan, cutoff)
    return plan


def execute_cleanup(plan: CleanupPlan, dry_run: bool = False) -> Dict[str, Any]:
    """Delete the planned items (or only report them when ``dry_run``)."""
    removed: List[str] = []
    errors: List[str] = []
    freed = 0
    for item in plan.items:
        if dry_run:
            removed.append(str(item.path))
            freed += item.size
            continue
        try:
            if item.path.is_dir():
                shutil.rmtree(item.path)
            else:
                item.path.unlink()
            removed.append(str(item.path))
            freed += item.size
        except OSError as exc:
            errors.append("%s : %s" % (item.path, exc))
    return {
        "dry_run": dry_run,
        "removed": removed,
        "errors": errors,
        "freed_bytes": freed,
        "total_bytes": plan.total_bytes,
        "skipped": list(plan.skipped),
    }


def _trash_root(config: config_module.Config) -> Path:
    try:
        return config.storage_path("trash_path")
    except (KeyError, OSError):
        return config.data_dir / "trash"


def move_account_archives(
    config: config_module.Config, run_dir: Path, account: str
) -> Dict[str, str]:
    """Move one account's temporary archives to ``data/trash``.

    Returns a mapping ``old path -> new path`` so callers can update reports.
    """
    run_dir = Path(run_dir)
    if not run_dir.exists():
        return {}
    prefix = safe_filename(account) + "."
    target_root = _trash_root(config) / run_dir.name / safe_filename(account)
    moved: Dict[str, str] = {}
    for path in sorted(run_dir.iterdir()):
        if not path.is_file() or not path.name.startswith(prefix):
            continue
        target_root.mkdir(parents=True, exist_ok=True)
        target = target_root / path.name
        try:
            os.replace(path, target)
        except OSError:
            shutil.move(str(path), str(target))
        moved[str(path)] = str(target)
    return moved


def cleanup_partial_archives(config: config_module.Config) -> List[str]:
    """Remove interrupted .part/.tmp archives from mailbox run directories."""
    removed: List[str] = []
    root = config.storage_path("mailbox_path")
    if not root.exists():
        return removed
    for pattern in ("run-*/*.part", "run-*/*.tmp", "run-*/**/*.part"):
        for path in root.glob(pattern):
            try:
                path.unlink()
                removed.append(str(path))
            except OSError:
                continue
    return removed


def cleanup_archives(
    config: config_module.Config, older_than_days: int
) -> List[str]:
    """Delete mailbox archives older than ``older_than_days``. Returns paths."""
    removed: List[str] = []
    if older_than_days <= 0:
        return removed
    cutoff = time.time() - older_than_days * 86400
    root = config.storage_path("mailbox_path")
    if not root.exists():
        return removed
    for path in root.glob("run-*/*.tgz"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed.append(str(path))
        except OSError:
            continue
    for directory in root.glob("run-*"):
        try:
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        except OSError:
            continue
    return removed
