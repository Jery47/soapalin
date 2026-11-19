"""Atomic JSON and bytes persistence helpers shared across the application."""

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Union

PathLike = Union[Path, str]


def read_json(path: PathLike, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Read a JSON object, returning ``default`` on any read/parse failure."""
    payload = read_json_or_none(path)
    if payload is None:
        return dict(default or {})
    return payload


def read_json_or_none(path: PathLike) -> Optional[Dict[str, Any]]:
    """Read a JSON object, returning ``None`` when the file is corrupt.

    A missing file is not an error and returns an empty dict.
    """
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def write_json_atomic(
    path: PathLike,
    payload: Any,
    *,
    indent: Optional[int] = 2,
    sort_keys: bool = False,
    mode: Optional[int] = None,
    default: Any = None,
) -> None:
    """Write ``payload`` as JSON using ``mkstemp`` + ``os.replace``."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                indent=indent,
                sort_keys=sort_keys,
                default=default,
            )
            handle.write("\n")
        os.replace(tmp_name, target)
        if mode is not None:
            try:
                os.chmod(str(target), mode)
            except OSError:
                pass
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def write_bytes_atomic(
    path: PathLike, data: bytes, *, mode: Optional[int] = None
) -> None:
    """Write raw bytes atomically."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_name, target)
        if mode is not None:
            try:
                os.chmod(str(target), mode)
            except OSError:
                pass
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def write_text_atomic(
    path: PathLike, text: str, *, mode: Optional[int] = None
) -> None:
    """Write text atomically using UTF-8."""
    write_bytes_atomic(path, text.encode("utf-8"), mode=mode)
