"""Mailbox run checkpoints and run-directory helpers."""
import os
from pathlib import Path
from typing import Any, Dict, Optional, Set

from . import config as config_module
from . import storage
from .cleanup import safe_filename as _cleanup_safe_filename
from .inventory import now_iso


def _safe_filename(email: str) -> str:
    return _cleanup_safe_filename(email)


def _register_run_dir(config: config_module.Config, run_dir: Path) -> None:
    """Record the active run directory in the background job state."""
    job_id = os.environ.get("SOAPALIN_JOB_ID", "")
    if not job_id:
        return
    try:
        from .jobs import JobStore

        JobStore(config).update(job_id, run_dir=str(run_dir))
    except (KeyError, OSError, ValueError):
        pass


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    storage.write_json_atomic(path, payload, indent=1)


CHECKPOINT_NAME = "checkpoint.json"


def _checkpoint_path(run_dir: Path) -> Path:
    return run_dir / CHECKPOINT_NAME


def _load_checkpoint(run_dir: Path) -> Dict[str, Any]:
    return storage.read_json(_checkpoint_path(run_dir))


def _write_checkpoint(
    run_dir: Path,
    completed: Set[str],
    failed: Set[str],
    total: int,
    profile: str = "",
) -> None:
    storage.write_json_atomic(
        _checkpoint_path(run_dir),
        {
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "run_dir": str(run_dir),
            "profile": profile,
            "completed": sorted(completed),
            "failed": sorted(failed),
            "total": int(total),
        },
        indent=1,
    )


def _clear_checkpoint(run_dir: Path) -> None:
    try:
        _checkpoint_path(run_dir).unlink()
    except OSError:
        pass


def _latest_run_dir(root: Path, profile: str = "") -> Optional[Path]:
    candidates = sorted(
        (path for path in root.glob("run-*") if path.is_dir()),
        key=lambda path: path.name,
        reverse=True,
    )
    for path in candidates:
        if not _checkpoint_path(path).exists():
            continue
        checkpoint = _load_checkpoint(path)
        if profile and checkpoint.get("profile") not in ("", None, profile):
            continue
        return path
    return candidates[0] if candidates else None
