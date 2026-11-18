"""Background CLI jobs: create, launch, follow and stop migration processes."""

import datetime
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List

from . import storage

TERMINAL_STATUSES = {"succeeded", "failed", "stopped", "interrupted"}


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


class JobStore:
    def __init__(self, config):
        self.config = config
        self.directory = config.storage_path("job_path")

    def _job_dir(self, job_id: str) -> Path:
        safe = "".join(
            char for char in str(job_id) if char.isalnum() or char in "-_."
        )
        if not safe or safe in {".", ".."}:
            raise KeyError("Identifiant de job invalide : %s" % job_id)
        return self.directory / safe

    def _write_json(self, path: Path, payload: Dict[str, Any]) -> None:
        storage.write_json_atomic(path, payload, indent=1)

    def _read_json(self, path: Path) -> Dict[str, Any]:
        return storage.read_json(path)

    def create(self, argv: List[str], title: str = "") -> Dict[str, Any]:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        job_id = "%s-%s" % (stamp, uuid.uuid4().hex[:6])
        directory = self._job_dir(job_id)
        directory.mkdir(parents=True, exist_ok=True)
        now = _now()
        spec = {
            "id": job_id,
            "argv": list(argv),
            "cwd": os.getcwd(),
            "base_dir": str(self.config.base_dir),
            "title": title or " ".join(argv)[:180],
            "created_at": now,
        }
        state = {
            "id": job_id,
            "title": spec["title"],
            "status": "pending",
            "created_at": now,
            "started_at": "",
            "finished_at": "",
            "pid": 0,
            "returncode": None,
            "log_path": str(directory / "job.log"),
            "stop_requested": False,
        }
        self._write_json(directory / "spec.json", spec)
        self._write_json(directory / "state.json", state)
        return spec

    def launch(self, spec: Dict[str, Any]) -> subprocess.Popen:
        directory = self._job_dir(spec["id"])
        log_path = directory / "job.log"
        env = dict(os.environ)
        env["SOAPLUN_HOME"] = str(spec.get("base_dir") or self.config.base_dir)
        log_file = log_path.open("ab")
        try:
            kwargs: Dict[str, Any] = {
                "cwd": spec.get("cwd") or str(self.config.base_dir),
                "env": env,
                "stdin": subprocess.DEVNULL,
                "stdout": log_file,
                "stderr": subprocess.STDOUT,
            }
            if os.name == "nt":
                kwargs["creationflags"] = 0x00000008 | 0x00000200
            else:
                kwargs["start_new_session"] = True
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "soaplun",
                    "--job-worker",
                    str(directory / "spec.json"),
                ],
                **kwargs
            )
        finally:
            log_file.close()
        state = self.state(spec["id"])
        state.update(
            {
                "status": "running",
                "pid": process.pid,
                "started_at": state.get("started_at") or _now(),
            }
        )
        self._write_json(directory / "state.json", state)
        return process

    def state(self, job_id: str) -> Dict[str, Any]:
        directory = self._job_dir(job_id)
        return self._read_json(directory / "state.json")

    def spec(self, job_id: str) -> Dict[str, Any]:
        return self._read_json(self._job_dir(job_id) / "spec.json")

    def update(self, job_id: str, **changes: Any) -> Dict[str, Any]:
        state = self.state(job_id)
        state.update(changes)
        self._write_json(self._job_dir(job_id) / "state.json", state)
        return state

    def list(self) -> List[Dict[str, Any]]:
        states: List[Dict[str, Any]] = []
        for directory in self.directory.iterdir():
            if not directory.is_dir():
                continue
            state = self._read_json(directory / "state.json")
            if state.get("id"):
                states.append(state)
        return sorted(states, key=lambda item: item.get("created_at", ""), reverse=True)

    def selected(self, selector: str) -> List[Dict[str, Any]]:
        selector = str(selector or "").strip()
        if not selector or selector.lower() == "all":
            return self.list()
        matches = [
            state
            for state in self.list()
            if state.get("id") == selector
            or str(state.get("id", "")).startswith(selector)
        ]
        if not matches:
            raise KeyError("Job introuvable : %s" % selector)
        return matches

    def active(self) -> List[Dict[str, Any]]:
        return [
            state
            for state in self.list()
            if state.get("status") not in TERMINAL_STATUSES
        ]

    def request_stop(self, job_id: str) -> Dict[str, Any]:
        state = self.state(job_id)
        if not state:
            raise KeyError("Job introuvable : %s" % job_id)
        if state.get("status") in TERMINAL_STATUSES:
            return state
        directory = self._job_dir(job_id)
        (directory / "stop").write_text("stop\n", encoding="utf-8")
        state["stop_requested"] = True
        if state.get("status") == "running":
            state["status"] = "stopping"
        self._write_json(directory / "state.json", state)
        return state

    def stop_requested(self, job_id: str) -> bool:
        try:
            return (self._job_dir(job_id) / "stop").exists()
        except KeyError:
            return False

    def log_path(self, job_id: str) -> Path:
        return self._job_dir(job_id) / "job.log"


def follow_jobs(
    store: JobStore,
    jobs: List[Dict[str, Any]],
    sleep_seconds: float = 0.5,
    out: Callable[..., None] = print,
) -> int:
    offsets: Dict[str, int] = {state["id"]: 0 for state in jobs}
    active = {
        state["id"]
        for state in jobs
        if state.get("status") not in TERMINAL_STATUSES
    }
    if not active:
        out("Aucun job en cours.")
        return 0
    printed = set()
    while active:
        for job_id in sorted(list(active)):
            path = store.log_path(job_id)
            if job_id not in printed:
                out("==> %s (%s)" % (job_id, store.state(job_id).get("status", "")))
                printed.add(job_id)
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            offset = offsets.get(job_id, 0)
            if size > offset:
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(offset)
                    chunk = handle.read()
                    offsets[job_id] = handle.tell()
                out(chunk, end="")
            state = store.state(job_id)
            if state.get("status") in TERMINAL_STATUSES:
                out("[%s] %s" % (job_id, state.get("status")))
                active.discard(job_id)
        if active:
            time.sleep(sleep_seconds)
    return 0


def watch_stop(
    store: JobStore,
    job_id: str,
    cancel: threading.Event,
    done: threading.Event,
) -> None:
    while not done.wait(0.5):
        if store.stop_requested(job_id):
            cancel.set()
            state = store.state(job_id)
            if state.get("status") == "running":
                store.update(job_id, status="stopping")
            return
