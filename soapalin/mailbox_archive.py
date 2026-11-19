"""Archive backend: zmmailbox/REST export, import and verification."""

import json
import os
import re
import shutil
import socket
import subprocess
import tarfile
import time
from email.parser import BytesHeaderParser
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from . import config as config_module
from .connections import ZimbraSession
from .mailbox_common import (
    CONTENT_SPECS,
    MailboxError,
    Progress,
    since_query,
)

_LOCAL_NAMES: Optional[set] = None


TGZ_MAGIC = b"\x1f\x8b"


HEADER_READ_LIMIT = 256 * 1024


def zmmailbox_path() -> str:
    return shutil.which("zmmailbox") or ""


def local_hostnames() -> set:
    """Names that designate this machine, including the Zimbra mail host."""
    global _LOCAL_NAMES
    if _LOCAL_NAMES is not None:
        return _LOCAL_NAMES
    names = {
        "localhost",
        "127.0.0.1",
        "::1",
        socket.gethostname().lower(),
        socket.getfqdn().lower(),
    }
    try:
        _name, aliases, addresses = socket.gethostbyname_ex(socket.gethostname())
        names.update(alias.lower() for alias in aliases)
        names.update(address.lower() for address in addresses)
    except OSError:
        pass
    binary = shutil.which("zmhostname")
    if binary and os.name == "posix":
        try:
            result = subprocess.run(
                [binary],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )
            value = result.stdout.decode("utf-8", "replace").strip().lower()
            if value:
                names.add(value)
        except (OSError, subprocess.SubprocessError):
            pass
    names.discard("")
    _LOCAL_NAMES = names
    return names


def is_local_host(host: str) -> bool:
    """True when ``host`` points at the machine running this process."""
    value = (host or "").strip().lower()
    if not value:
        return False
    if value in local_hostnames():
        return True
    try:
        return socket.gethostbyname(value).lower() in local_hostnames()
    except OSError:
        return False


def use_zmmailbox(connection: Dict[str, Any]) -> bool:
    """Decide whether ``zmmailbox`` can safely act on this server.

    ``zmmailbox`` always talks to the *local* mailbox server.  Running an
    export or import for an account hosted elsewhere would target the wrong
    server, so the automatic mode only uses it when the configured host is
    this machine.  An explicit ``zmmailbox`` mode still forces it for unusual
    setups (for example a container that shares the mailbox store).
    """
    mode = (connection.get("zmmailbox_mode") or "auto").lower()
    if mode == "rest":
        return False
    if os.name != "posix" or not zmmailbox_path():
        return False
    if mode == "zmmailbox":
        return True
    return is_local_host(config_module.store_host(connection))


def backend_info(connection: Dict[str, Any]) -> Dict[str, Any]:
    """Describe the backend that will be used for one side, with a reason."""
    mode = (connection.get("zmmailbox_mode") or "auto").lower()
    binary = zmmailbox_path()
    if mode == "rest":
        return {"backend": "rest", "reason": "mode REST forcé"}
    if os.name != "posix":
        reason = "zmmailbox nécessite Linux ; REST utilisé"
        if mode == "zmmailbox":
            reason = "mode zmmailbox demandé, mais zmmailbox nécessite Linux ; REST utilisé"
        return {"backend": "rest", "reason": reason, "warning": mode == "zmmailbox"}
    if not binary:
        return {"backend": "rest", "reason": "zmmailbox introuvable"}
    if mode == "zmmailbox":
        return {
            "backend": "zmmailbox",
            "reason": "mode zmmailbox forcé",
            "warning": not is_local_host(config_module.store_host(connection)),
        }
    if is_local_host(config_module.store_host(connection)):
        return {"backend": "zmmailbox", "reason": "hôte local détecté"}
    return {
        "backend": "rest",
        "reason": "hôte distant : zmmailbox agirait sur le mauvais serveur",
    }


def _run(
    command: List[str],
    stdout_path: Optional[Path] = None,
    timeout: Optional[int] = None,
    stdin_path: Optional[Path] = None,
) -> Tuple[int, bytes, bytes]:
    stdout_handle = None
    stdin_handle = None
    try:
        if stdout_path is not None:
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            stdout_handle = stdout_path.open("wb")
        if stdin_path is not None:
            stdin_handle = stdin_path.open("rb")
        process = subprocess.run(
            command,
            stdout=stdout_handle if stdout_handle else subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=stdin_handle if stdin_handle else subprocess.DEVNULL,
            timeout=timeout,
        )
        return (
            process.returncode,
            process.stdout if process.stdout else b"",
            process.stderr if process.stderr else b"",
        )
    finally:
        if stdout_handle:
            stdout_handle.close()
        if stdin_handle:
            stdin_handle.close()


def check_zmmailbox_environment() -> Dict[str, Any]:
    path = zmmailbox_path()
    names = sorted(local_hostnames())
    note = ""
    if os.name == "nt":
        note = "Windows : zmmailbox est absent ; les copies de boîtes utilisent REST."
    return {
        "available": bool(path),
        "path": path,
        "platform": os.name,
        "user": _current_user(),
        "usable": bool(path) and os.name == "posix",
        "local_hosts": names[:20],
        "note": note,
    }


def _current_user() -> str:
    try:
        import getpass

        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return ""


def verify_archive(path: Path, deep: bool = False) -> Dict[str, Any]:
    """Verify that an export produced a plausible gzip/tar archive."""
    if not path.exists():
        raise MailboxError("Archive absente : %s" % path)
    size = path.stat().st_size
    if size == 0:
        raise MailboxError("Archive vide : %s" % path)
    with path.open("rb") as handle:
        magic = handle.read(2)
    if magic != TGZ_MAGIC:
        raise MailboxError(
            "Le fichier exporté n'est pas une archive gzip valide (%s)" % path
        )
    result: Dict[str, Any] = {"path": str(path), "size": size, "valid": True}
    if deep:
        import tarfile

        count = 0
        try:
            with tarfile.open(path, "r|gz") as archive:
                for _ in archive:
                    count += 1
        except Exception as exc:  # noqa: BLE001
            raise MailboxError("Archive tar.gz corrompue : %s" % exc) from exc
        result["members"] = count
    return result


def _normalize_duplicate_key(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("_content", "")
    text = str(value or "").strip()
    if text.startswith("<") and text.endswith(">"):
        text = text[1:-1]
    return " ".join(text.split()).lower()


def _normalize_email(value: Any) -> str:
    text = str(value or "").strip()
    _name, address = parseaddr(text)
    return _normalize_duplicate_key(address or text)


def _message_id_from_bytes(data: bytes) -> str:
    try:
        message = BytesHeaderParser().parsebytes(data, headersonly=True)
        return _normalize_duplicate_key(message.get("Message-ID", ""))
    except Exception:  # noqa: BLE001 - malformed message is kept as-is
        return ""


def _metadata_payload(raw: bytes) -> Dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _metadata_text(raw: bytes) -> str:
    return str(_metadata_payload(raw).get("metadata") or "")


def _metadata_values(metadata: str, key: str) -> List[str]:
    pattern = re.compile(r"%d:%s(\d+):" % (len(key), re.escape(key)))
    values: List[str] = []
    for match in pattern.finditer(metadata):
        length = int(match.group(1))
        value = metadata[match.end() : match.end() + length]
        if len(value) == length and value:
            values.append(value)
    return values


def _metadata_duplicate_keys(metadata: str, kind: str) -> Set[str]:
    keys: Set[str] = set()
    if kind in {"calendar", "tasks"}:
        for uid in _metadata_values(metadata, "u"):
            normalized = _normalize_duplicate_key(uid)
            if normalized:
                keys.add("uid:" + normalized)
    elif kind == "contacts":
        for name in ("email", "email2", "email3", "email4"):
            for email in _metadata_values(metadata, name):
                normalized = _normalize_email(email)
                if normalized:
                    keys.add("email:" + normalized)
        for full_name in _metadata_values(metadata, "fullName"):
            normalized = _normalize_duplicate_key(full_name)
            if normalized:
                keys.add("name:" + normalized)
    return keys


def _content_duplicate_keys(data: bytes, kind: str) -> Set[str]:
    text = data.decode("utf-8", "replace")
    keys: Set[str] = set()
    if kind in {"calendar", "tasks"}:
        for match in re.finditer(r"(?im)^UID:\s*(.+?)\s*$", text):
            normalized = _normalize_duplicate_key(match.group(1))
            if normalized:
                keys.add("uid:" + normalized)
    elif kind == "contacts":
        for match in re.finditer(r"(?im)^EMAIL[^:]*:\s*(.+?)\s*$", text):
            normalized = _normalize_email(match.group(1))
            if normalized:
                keys.add("email:" + normalized)
    return keys


def _archive_base(name: str) -> str:
    value = str(name or "")
    if value.endswith(".meta"):
        value = value[:-5]
    for suffix in (".eml", ".appt", ".task", ".vcf", ".ics"):
        if value.endswith(suffix):
            return value[: -len(suffix)]
    return value


def _archive_kind(name: str) -> str:
    value = str(name or "")
    content = value[:-5] if value.endswith(".meta") else value
    lowered = content.lower()
    if lowered.endswith(".eml"):
        return "mail"
    if lowered.endswith(".appt"):
        return "calendar"
    if lowered.endswith(".task"):
        return "tasks"
    if lowered.endswith(".vcf"):
        return "contacts"
    if lowered.endswith(".ics"):
        return "calendar"
    if value.endswith(".meta"):
        return "contacts"
    return ""


def _collect_archive_entries(
    archive: Path, content: str = "all"
) -> Dict[str, Dict[str, Any]]:
    entries: Dict[str, Dict[str, Any]] = {}
    with tarfile.open(str(archive), "r|gz") as handle:
        for member in handle:
            if not member.isfile():
                continue
            kind = _archive_kind(member.name)
            if not kind or (content != "all" and kind != content):
                continue
            base = _archive_base(member.name)
            entry = entries.setdefault(
                base, {"id": "", "size": 0, "keys": set(), "kind": kind}
            )
            if member.name.endswith(".meta"):
                stream = handle.extractfile(member)
                raw = stream.read() if stream is not None else b""
                payload = _metadata_payload(raw)
                item_id = payload.get("id")
                if item_id not in {None, ""}:
                    entry["id"] = str(item_id)
                try:
                    entry["size"] = int(payload.get("size") or 0)
                except (TypeError, ValueError):
                    entry["size"] = 0
                if kind == "mail":
                    continue
                entry["keys"].update(
                    _metadata_duplicate_keys(_metadata_text(raw), kind)
                )
            else:
                stream = handle.extractfile(member)
                data = (
                    stream.read(HEADER_READ_LIMIT) if stream is not None else b""
                )
                if kind == "mail":
                    message_id = _message_id_from_bytes(data)
                    if message_id:
                        entry["keys"].add("mail:" + message_id)
                else:
                    entry["keys"].update(_content_duplicate_keys(data, kind))
    return entries


def _collect_archive_keys(
    archive: Path, content: str = "all"
) -> Tuple[Dict[str, Set[str]], Set[str]]:
    entries = _collect_archive_entries(archive, content=content)
    keys_by_base = {
        base: entry["keys"]
        for base, entry in entries.items()
        if entry["keys"]
    }
    kinds = {
        entry["kind"] for entry in entries.values() if entry["keys"]
    }
    return keys_by_base, kinds


def export_mailbox(
    config: config_module.Config,
    source: ZimbraSession,
    email: str,
    destination: Path,
    progress: Progress = None,
    query: Optional[str] = None,
    content: str = "all",
    body: Optional[int] = None,
) -> Dict[str, Any]:
    connection = config.connection("source")
    spec = CONTENT_SPECS.get(content) or CONTENT_SPECS["all"]
    if query is None and config is not None:
        query = since_query(config.sync().get("since") or "")
    query = query or spec.get("query") or "is:anywhere"
    types = spec.get("types") or ""
    destination.parent.mkdir(parents=True, exist_ok=True)
    part = destination.with_suffix(destination.suffix + ".part")
    if part.exists():
        try:
            part.unlink()
        except OSError:
            pass
    started = time.time()

    if use_zmmailbox(connection):
        binary = zmmailbox_path()
        rest_query = "//?fmt=tgz&meta=1&query=%s" % query
        if types:
            rest_query += "&types=%s" % types
        if body is not None:
            rest_query += "&body=%d" % int(body)
        command = [
            binary,
            "-z",
            "-m",
            email,
            "-t",
            "0",
            "getRestURL",
            rest_query,
        ]
        code, _stdout, stderr = _run(
            command,
            stdout_path=part,
            timeout=int(connection.get("read_timeout") or 300) * 20,
        )
        if code != 0:
            message = stderr.decode("utf-8", "replace").strip()
            raise MailboxError(
                "zmmailbox export a échoué (%s) : %s" % (code, message[:800])
            )
    else:
        token, _lifetime = source.auth_token(email)
        rest = source.rest()
        rest.timeout = max(
            rest.timeout, int(connection.get("read_timeout") or 300) * 20
        )
        url = source.export_url(email, query=query, types=types)
        if body is not None:
            url += "&body=%d" % int(body)

        def download_progress(done: int, total: Optional[int]) -> None:
            if progress:
                progress(done, total or 0, "Téléchargement de %s" % email)

        rest.download(
            url,
            part,
            auth_token=token,
            progress=download_progress,
            chunk_size=1024 * 1024,
        )

    if part.exists() and part.stat().st_size == 0:
        try:
            part.unlink()
        except OSError:
            pass
        return {
            "path": str(destination),
            "size": 0,
            "valid": True,
            "empty": True,
            "seconds": round(time.time() - started, 1),
            "bytes": 0,
            "backend": "zmmailbox" if use_zmmailbox(connection) else "rest",
            "content": content,
        }

    info = verify_archive(part)
    if part.exists():
        os.replace(part, destination)
    info["path"] = str(destination)
    info["seconds"] = round(time.time() - started, 1)
    info["bytes"] = destination.stat().st_size
    info["backend"] = "zmmailbox" if use_zmmailbox(connection) else "rest"
    info["content"] = content
    return info


def import_mailbox(
    config: config_module.Config,
    destination: ZimbraSession,
    email: str,
    archive: Path,
    progress: Progress = None,
    resolve: Optional[str] = None,
) -> Dict[str, Any]:
    connection = config.connection("destination")
    resolve = (resolve or config.sync().get("mailbox_resolve") or "skip").lower()
    if resolve not in {"skip", "mod"}:
        raise MailboxError(
            "Valeur resolve=%r interdite : utilisez « skip » ou « mod »." % resolve
        )
    if not archive.exists():
        raise MailboxError("Archive introuvable : %s" % archive)
    started = time.time()

    if use_zmmailbox(connection):
        binary = zmmailbox_path()
        command = [
            binary,
            "-z",
            "-m",
            email,
            "-t",
            "0",
            "postRestURL",
            "//?fmt=tgz&resolve=%s" % resolve,
            str(archive),
        ]
        code, stdout, stderr = _run(
            command,
            timeout=int(connection.get("read_timeout") or 300) * 20,
        )
        output = (stdout + b"\n" + stderr).decode("utf-8", "replace").strip()
        if code != 0:
            raise MailboxError(
                "zmmailbox import a échoué (%s) : %s" % (code, output[:1200])
            )
    else:
        token, _lifetime = destination.auth_token(email)
        rest = destination.rest()
        rest.timeout = max(
            rest.timeout, int(connection.get("read_timeout") or 300) * 20
        )
        url = destination.import_url(email, resolve=resolve)

        def upload_progress(done: int, total: Optional[int]) -> None:
            if progress:
                progress(done, total or 0, "Envoi de %s" % email)

        status, body = rest.upload(
            url,
            archive,
            auth_token=token,
            progress=upload_progress,
            chunk_size=1024 * 1024,
        )
        if status >= 400:
            raise MailboxError(
                "Import REST HTTP %s pour %s : %s" % (status, email, body[:1200])
            )
        output = body[:1200]

    return {
        "seconds": round(time.time() - started, 1),
        "resolve": resolve,
        "output": output[-800:],
    }


def free_space(path: Path) -> int:
    try:
        return shutil.disk_usage(str(path)).free
    except OSError:
        return 0


