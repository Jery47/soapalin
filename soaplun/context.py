"""Explicit per-run context shared by the CLI and the pipeline."""

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class RunContext:
    options: Dict[str, Any] = field(default_factory=dict)
    cancel: Optional[threading.Event] = None
    reports: List[Dict[str, Any]] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
