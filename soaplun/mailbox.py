"""Mailbox data copy (export/import) via zmmailbox or Zimbra REST."""

import datetime
import gzip
import json
import os
import re
import shutil
import socket
import subprocess
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.parser import BytesHeaderParser
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from . import config as config_module
from . import inventory as inventory_module
from .connections import ZimbraSession, describe_error, preflight
from .inventory import now_iso

Progress = Optional[Callable[[int, int, str], None]]

TGZ_MAGIC = b"\x1f\x8b"

CONTENT_SPECS: Dict[str, Dict[str, str]] = {
    "all": {
        "label": "Tous les contenus",
        "query": "is:anywhere",
        "types": "",
    },
    "mail": {
        "label": "Mails",
        "query": "is:anywhere",
        "types": "message",
    },
    "calendar": {
        "label": "Agenda",
        "query": "is:anywhere",
        "types": "appointment",
    },
    "contacts": {
        "label": "Contacts",
        "query": "is:anywhere",
        "types": "contact",
    },
    "tasks": {
        "label": "Tâches",
        "query": "is:anywhere",
        "types": "task",
    },
    "briefcase": {
        "label": "Briefcase",
        "query": "is:anywhere",
        "types": "document",
    },
}


def normalize_content_types(values: Optional[List[str]]) -> List[str]:
    if values is None:
        return ["all"]
    if "all" in values:
        return ["all"]
    return [name for name in CONTENT_SPECS if name in values and name != "all"]


def since_query(value: str) -> str:
    """Convert an ISO date to the Zimbra ``date:>=`` search fragment."""
    value = str(value or "").strip()
    if not value:
        return ""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            parsed = datetime.datetime.strptime(value, fmt)
            return "date:>=%s" % parsed.strftime("%m/%d/%Y")
        except ValueError:
            continue
    raise MailboxError(
        "Date --since invalide : utilisez le format YYYY-MM-DD."
    )


class MailboxError(Exception):
    pass


# ----------------------------------------------------------------------
# Backend detection
# ----------------------------------------------------------------------
_LOCAL_NAMES: Optional[set] = None


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


# ----------------------------------------------------------------------
# Verification helpers
# ----------------------------------------------------------------------
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


HEADER_READ_LIMIT = 256 * 1024
DUPLICATE_BATCH = 50


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)


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


def _search_items(value: Any) -> List[Dict[str, Any]]:
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _search_pages(
    client: Any,
    types: str,
    response_key: str,
    query: str = "is:anywhere",
    page_size: int = 1000,
    adaptive: bool = False,
    skipped: Optional[List[int]] = None,
    fetch: str = "",
) -> Iterable[Dict]:
    offset = 0
    sizes = [page_size] if not adaptive else [
        value for value in (page_size, 100, 50, 10, 1) if value <= page_size
    ]
    size_index = 0
    while True:
        current_size = sizes[size_index]
        try:
            response = client.request(
                "Search",
                dict(
                    {
                        "query": {"_content": query},
                        "types": types,
                        "limit": str(current_size),
                        "offset": str(offset),
                    },
                    **({"fetch": fetch} if fetch else {})
                ),
            )
        except Exception:  # noqa: BLE001 - some Zimbra indexes fail on large pages
            if adaptive and size_index + 1 < len(sizes):
                size_index += 1
                continue
            if adaptive:
                if skipped is not None:
                    skipped.append(offset)
                offset += 1
                size_index = 0
                continue
            raise
        size_index = 0
        items = _search_items(response.get(response_key))
        for item in items:
            yield item
        if str(response.get("more", "0")).lower() not in {"1", "true"}:
            break
        if not items:
            break
        offset += len(items)


STRUCTURED_SEARCH = {
    "calendar": ("appointment", "appt"),
    "tasks": ("task", "task"),
    "contacts": ("contact", "cn"),
}


def _structured_item_keys(item: Dict[str, Any], kind: str) -> Set[str]:
    keys: Set[str] = set()
    if kind in {"calendar", "tasks"}:
        normalized = _normalize_duplicate_key(
            item.get("uid") or item.get("x_uid") or ""
        )
        if normalized:
            keys.add("uid:" + normalized)
    elif kind == "contacts":
        for attribute in _search_items(item.get("a")):
            name = str(attribute.get("n") or "").lower()
            value = attribute.get("_content")
            if not value:
                continue
            normalized = (
                _normalize_email(value)
                if name.startswith("email")
                else _normalize_duplicate_key(value)
            )
            if not normalized:
                continue
            if name.startswith("email"):
                keys.add("email:" + normalized)
            elif name == "fullname":
                keys.add("name:" + normalized)
    return keys


def _destination_structured_keys(client: Any, kinds: Set[str]) -> Set[str]:
    keys: Set[str] = set()
    for kind in kinds:
        search = STRUCTURED_SEARCH.get(kind)
        if not search:
            continue
        for item in _search_pages(client, search[0], search[1]):
            keys.update(_structured_item_keys(item, kind))
    return keys


def _source_structured_entries(
    client: Any, kind: str
) -> List[Dict[str, Any]]:
    search = STRUCTURED_SEARCH.get(kind)
    if not search:
        return []
    entries: List[Dict[str, Any]] = []
    for item in _search_pages(client, search[0], search[1]):
        item_id = item.get("id")
        if item_id in {None, ""}:
            return []
        entries.append(
            {
                "id": str(item_id),
                "size": int(item.get("s") or 0),
                "keys": _structured_item_keys(item, kind),
            }
        )
    return entries


def _emit_progress(
    progress: Progress,
    done: int,
    total: int,
    message: str,
    detail: Optional[Dict[str, Any]] = None,
) -> None:
    if not progress:
        return
    try:
        progress(done, total, message, detail or {})
    except TypeError:
        progress(done, total, message)


def _mailbox_used(session: ZimbraSession, email: str) -> int:
    try:
        client = session.account_client(email)
        response = client.request("GetInfo", {"sections": "mbox"})
        return int(response.get("used") or 0)
    except Exception:  # noqa: BLE001 - size is optional for progress
        return 0


def _walk_mail_folders(node: Any) -> Iterable[Dict[str, Any]]:
    if not isinstance(node, dict):
        return
    yield node
    for child in _search_items(node.get("folder")):
        yield from _walk_mail_folders(child)


def _mail_folder_paths(client: Any) -> List[str]:
    response = client.request(
        "GetFolder", {"folder": {"path": "/"}, "depth": "20"}
    )
    root = response.get("folder") if isinstance(response, dict) else None
    paths: List[str] = []
    for folder in _walk_mail_folders(root):
        path = str(folder.get("absFolderPath") or "")
        view = str(folder.get("view") or "").lower()
        try:
            count = int(folder.get("n") or 0)
        except (TypeError, ValueError):
            count = 0
        if not path or path == "/" or count <= 0:
            continue
        if view in {"appointment", "contact", "task", "document"}:
            continue
        paths.append(path)
    return paths


def _source_mail_entries(
    client: Any,
    skipped: Optional[List[int]] = None,
    since: str = "",
) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for path in _mail_folder_paths(client):
        query = 'in:"%s"' % path.replace('"', '\\"')
        if since:
            query += " " + since
        for item in _search_pages(
            client,
            "message",
            "m",
            query=query,
            page_size=1000,
            adaptive=False,
            skipped=skipped,
        ):
            item_id = item.get("id")
            if item_id in {None, ""} or str(item_id) in seen:
                continue
            seen.add(str(item_id))
            entries.append(
                {
                    "id": str(item_id),
                    "size": int(item.get("s") or 0),
                    "keys": set(),
                }
            )
    return entries


def _mail_query_term(message_id: str) -> str:
    escaped = message_id.replace("\\", "\\\\").replace('"', '\\"')
    return 'msgid:"%s"' % escaped


def _destination_has_messages(client: Any) -> bool:
    response = client.request(
        "Search",
        {
            "query": {"_content": "is:anywhere"},
            "types": "message",
            "limit": "1",
            "offset": "0",
        },
    )
    return bool(_search_items(response.get("m")))


def _destination_mail_keys(client: Any, source_keys: Set[str]) -> Set[str]:
    message_ids = sorted(
        {
            key.split(":", 1)[1]
            for key in source_keys
            if key.startswith("mail:") and key.split(":", 1)[1]
        }
    )
    found: Set[str] = set()
    pending = [
        message_ids[index : index + DUPLICATE_BATCH]
        for index in range(0, len(message_ids), DUPLICATE_BATCH)
    ]
    while pending:
        batch = pending.pop()
        query = "is:anywhere (" + " OR ".join(
            _mail_query_term(message_id) for message_id in batch
        ) + ")"
        params = {
            "query": {"_content": query},
            "types": "message",
            "limit": str(len(batch)),
            "offset": "0",
        }
        try:
            response = client.request("Search", params)
        except Exception:  # noqa: BLE001 - split an oversized/broken batch
            if len(batch) > 1:
                middle = len(batch) // 2
                pending.append(batch[:middle])
                pending.append(batch[middle:])
                continue
            raise
        hits = _search_items(response.get("m"))
        if not hits:
            continue
        expanded = dict(params)
        expanded["fetch"] = "all"
        expanded["limit"] = "1000"
        expanded_response = client.request("Search", expanded)
        for item in _search_items(expanded_response.get("m")):
            message_id = _normalize_duplicate_key(item.get("mid") or "")
            if message_id:
                found.add("mail:" + message_id)
    return found


def _duplicate_bases(
    keys_by_base: Dict[str, Set[str]], destination_keys: Set[str]
) -> Set[str]:
    return {
        base
        for base, keys in keys_by_base.items()
        if keys.intersection(destination_keys)
    }


def _filter_archive(
    archive: Path, target: Path, duplicate_bases: Set[str]
) -> Tuple[Path, int]:
    part = target.with_suffix(target.suffix + ".part")
    if part.exists():
        try:
            part.unlink()
        except OSError:
            pass
    remaining = 0
    try:
        with tarfile.open(str(archive), "r|gz") as source, tarfile.open(
            str(part), "w|gz", format=tarfile.PAX_FORMAT
        ) as destination:
            for member in source:
                if _archive_base(member.name) in duplicate_bases:
                    continue
                stream = source.extractfile(member) if member.isfile() else None
                destination.addfile(member, stream)
                if member.isfile() and not member.name.endswith(".meta"):
                    remaining += 1
        if not part.exists() or part.stat().st_size == 0:
            raise MailboxError("Archive filtree vide : %s" % part)
        verify_archive(part)
        os.replace(part, target)
    finally:
        if part.exists():
            try:
                part.unlink()
            except OSError:
                pass
    return target, remaining


def deduplicate_archive(
    destination: ZimbraSession,
    email: str,
    archive: Path,
    content: str = "all",
) -> Dict[str, Any]:
    """Remove messages/items already present on the destination.

    The returned ``archive`` is either the original file (no duplicate) or a
    filtered copy.  ``all_duplicates`` means the caller must not import it.
    """
    keys_by_base, kinds = _collect_archive_keys(archive, content=content)
    if not keys_by_base:
        return {
            "archive": str(archive),
            "duplicates": 0,
            "all_duplicates": False,
            "filtered": False,
            "kinds": [],
        }

    client = destination.mail_client(email)
    destination_keys: Set[str] = set()
    structured = {kind for kind in kinds if kind != "mail"}
    if structured:
        destination_keys.update(_destination_structured_keys(client, structured))
    if "mail" in kinds:
        mail_keys: Set[str] = set()
        for keys in keys_by_base.values():
            mail_keys.update(key for key in keys if key.startswith("mail:"))
        if mail_keys and _destination_has_messages(client):
            destination_keys.update(_destination_mail_keys(client, mail_keys))

    duplicates = _duplicate_bases(keys_by_base, destination_keys)
    if not duplicates:
        return {
            "archive": str(archive),
            "duplicates": 0,
            "all_duplicates": False,
            "filtered": False,
            "kinds": sorted(kinds),
        }

    filtered = archive.with_name(archive.stem + ".filtered.tgz")
    filtered, remaining = _filter_archive(archive, filtered, duplicates)
    return {
        "archive": str(filtered),
        "duplicates": len(duplicates),
        "all_duplicates": remaining == 0,
        "filtered": True,
        "kinds": sorted(kinds),
    }


def human_size(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "?"
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if number < 1024 or unit == "To":
            return "%.1f %s" % (number, unit)
        number /= 1024
    return "%.1f To" % number


# ----------------------------------------------------------------------
# Export / import
# ----------------------------------------------------------------------
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
    if query is None and content in {"mail", "all"} and config is not None:
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


# ----------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------
def free_space(path: Path) -> int:
    try:
        return shutil.disk_usage(str(path)).free
    except OSError:
        return 0


def _account_exists(session: ZimbraSession, email: str) -> bool:
    try:
        session.request_single(
            "GetAccount", {"account": {"by": "name", "_content": email}}
        )
        return True
    except Exception as exc:  # noqa: BLE001
        text = str(exc).lower()
        return not ("no such account" in text or "no_such_account" in text)


def _destination_quota(session: ZimbraSession, email: str) -> int:
    """Return the effective destination quota, or 0 when unknown/unlimited."""
    try:
        data = session.request_single(
            "GetAccount",
            {
                "account": {"by": "name", "_content": email},
                "attrs": "zimbraMailQuota",
                "applyCos": "1",
            },
        )
    except Exception:  # noqa: BLE001 - quota is advisory before the copy
        return 0
    if isinstance(data, dict) and isinstance(data.get("account"), dict):
        data = data["account"]
    if not isinstance(data, dict):
        return 0
    entries = data.get("a") or []
    if isinstance(entries, dict):
        entries = [entries]
    for entry in entries:
        if str(entry.get("n") or "") == "zimbraMailQuota":
            try:
                return max(0, int(entry.get("_content") or 0))
            except (TypeError, ValueError):
                return 0
    return 0


TAG_CONTENT_SPECS = {
    "mail": ("message", "m"),
    "calendar": ("appointment", "appt"),
    "contacts": ("contact", "cn"),
    "tasks": ("task", "task"),
}


def _text_value(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("_content") or "")
    return str(value or "")


def _split_names(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _item_tag_names(item: Dict[str, Any], tag_names: Dict[str, str]) -> Set[str]:
    names = set(_split_names(item.get("tn")))
    if names:
        return names
    for tag_id in _split_names(item.get("t")):
        name = tag_names.get(str(tag_id))
        if name:
            names.add(name)
    return names


def _tag_item_keys(item: Dict[str, Any], content: str) -> Set[str]:
    if content == "mail":
        message_id = _normalize_duplicate_key(_text_value(item.get("mid")))
        return {"mail:" + message_id} if message_id else set()
    return _structured_item_keys(item, content)


def _tagged_items(client: Any, content: str) -> List[Dict[str, Any]]:
    spec = TAG_CONTENT_SPECS.get(content)
    if not spec:
        return []
    return list(
        _search_pages(
            client,
            spec[0],
            spec[1],
            query="is:anywhere",
            fetch="all",
        )
    )


def sync_tags(
    config: config_module.Config,
    plan: Dict[str, Any],
    only: Optional[List[str]] = None,
    contents: Optional[List[str]] = None,
    keys: Optional[Set[str]] = None,
    dry_run: bool = False,
    progress: Progress = None,
    cancel: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    """Add missing source tags to matching destination items.

    Tag definitions must already exist on the destination. Existing destination
    tags are always preserved.
    """
    selected_contents = [
        content
        for content in (contents or list(TAG_CONTENT_SPECS))
        if content in TAG_CONTENT_SPECS
    ]
    pairs = plan.get("accounts") or [
        {
            "source": mailbox.get("source", ""),
            "destination": mailbox.get("destination", ""),
        }
        for mailbox in plan.get("mailboxes", [])
    ]
    pairs = [
        pair
        for pair in pairs
        if pair.get("source") and pair.get("destination")
    ]
    if only:
        wanted = {str(value).lower() for value in only}
        pairs = [
            pair
            for pair in pairs
            if str(pair.get("source", "")).lower() in wanted
            or str(pair.get("destination", "")).lower() in wanted
        ]
    wanted_keys = {str(key) for key in keys} if keys is not None else None
    source_session = ZimbraSession(config, "source", readonly=True)
    destination_session = ZimbraSession(config, "destination", readonly=False)
    total = len(pairs)
    counters = {
        "accounts": 0,
        "items": 0,
        "updated": 0,
        "tags": 0,
        "skipped": 0,
        "failed": 0,
    }
    errors: List[str] = []

    for index, pair in enumerate(pairs, start=1):
        if cancel is not None and cancel.is_set():
            break
        source_email = str(pair.get("source") or "")
        target_email = str(pair.get("destination") or "")
        _emit_progress(
            progress,
            index - 1,
            total,
            "Tags %d/%d : %s" % (index, total, source_email),
        )
        try:
            source_client = source_session.mail_client(source_email)
            destination_client = destination_session.mail_client(target_email)
            source_tags = {
                str(tag.get("id")): str(tag.get("name"))
                for tag in inventory_module.collect_tags(source_client)
                if tag.get("id") and tag.get("name")
            }
            destination_tags = {
                str(tag.get("id")): str(tag.get("name"))
                for tag in inventory_module.collect_tags(destination_client)
                if tag.get("id") and tag.get("name")
            }
            counters["accounts"] += 1
            for content in selected_contents:
                source_items = _tagged_items(source_client, content)
                destination_items = _tagged_items(destination_client, content)
                destination_by_key: Dict[str, List[Dict[str, Any]]] = {}
                for item in destination_items:
                    for key in _tag_item_keys(item, content):
                        destination_by_key.setdefault(key, []).append(item)
                for source_item in source_items:
                    source_keys = _tag_item_keys(source_item, content)
                    if not source_keys:
                        continue
                    if wanted_keys is not None and not source_keys.intersection(
                        wanted_keys
                    ):
                        continue
                    source_names = _item_tag_names(source_item, source_tags)
                    if not source_names:
                        continue
                    counters["items"] += 1
                    targets: List[Dict[str, Any]] = []
                    for key in source_keys:
                        targets.extend(destination_by_key.get(key, []))
                    if not targets:
                        counters["skipped"] += 1
                        continue
                    seen_ids = set()
                    for target in targets:
                        target_id = str(target.get("id") or "")
                        if not target_id or target_id in seen_ids:
                            continue
                        seen_ids.add(target_id)
                        destination_names = _item_tag_names(
                            target, destination_tags
                        )
                        missing = source_names - destination_names
                        if not missing:
                            counters["skipped"] += 1
                            continue
                        union = sorted(destination_names | source_names)
                        if dry_run:
                            counters["updated"] += 1
                            counters["tags"] += len(missing)
                            continue
                        try:
                            destination_client.request(
                                "ItemAction",
                                {
                                    "action": {
                                        "op": "update",
                                        "id": target_id,
                                        "tn": ",".join(union),
                                    }
                                },
                            )
                            counters["updated"] += 1
                            counters["tags"] += len(missing)
                        except Exception as exc:  # noqa: BLE001
                            counters["failed"] += 1
                            errors.append(
                                "%s / %s : %s"
                                % (target_email, content, describe_error(exc))
                            )
        except Exception as exc:  # noqa: BLE001
            counters["failed"] += 1
            errors.append("%s : %s" % (source_email, describe_error(exc)))
        _emit_progress(
            progress,
            index,
            total,
            "Tags %d/%d : %s termine" % (index, total, source_email),
        )

    return {
        "summary": counters,
        "errors": errors,
        "dry_run": dry_run,
    }


def copy_mailboxes(
    config: config_module.Config,
    plan: Dict[str, Any],
    progress: Progress = None,
    cancel: Optional[threading.Event] = None,
    only: Optional[List[str]] = None,
    keep_archives: bool = True,
    options: Optional[Dict[str, Any]] = None,
    content_types: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Copy every mailbox listed in ``plan``. Returns a report dict."""
    sync = dict(config.sync())
    sync.update(plan.get("options") or {})
    sync.update(options or {})
    if not sync.get("copy_mailbox", True):
        return {
            "created_at": now_iso(),
            "items": [],
            "summary": {"ok": 0, "failed": 0, "skipped": 0},
            "skipped": "copy_mailbox is disabled",
        }

    raw_contents = (
        content_types if content_types is not None else sync.get("content_types")
    )
    contents = normalize_content_types(raw_contents)
    since = since_query(sync.get("since") or "")
    resolve = (sync.get("mailbox_resolve") or "skip").lower()
    skip_duplicates = _truthy(sync.get("skip_duplicates"), True)
    if skip_duplicates and resolve != "skip":
        resolve = "skip"

    source_host = config_module.store_host(config.connection("source"))
    destination_host = config_module.store_host(config.connection("destination"))
    

    check = preflight(config, ("source", "destination"))
    if not check.get("ok"):
        details = []
        for side, entry in check.get("sides", {}).items():
            for error in entry.get("errors", []):
                details.append("%s : %s" % (side, error))
        raise MailboxError(
            "Préflight impossible avant la copie des boîtes : " + " | ".join(details)
        )

    storage = config.storage_path("mailbox_path")
    try:
        cleanup_archives(config, int(sync.get("keep_tgz_days") or 0))
    except Exception:  # noqa: BLE001 - maintenance must never block a copy
        pass
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = storage / ("run-" + stamp)
    run_dir.mkdir(parents=True, exist_ok=True)

    source = ZimbraSession(config, "source", readonly=True)
    destination = ZimbraSession(config, "destination", readonly=False)

    items: List[Dict[str, Any]] = []
    selected = plan.get("mailboxes", [])
    if only:
        wanted = {value.lower() for value in only}
        selected = [
            item
            for item in selected
            if item.get("source", "").lower() in wanted
            or item.get("destination", "").lower() in wanted
        ]

    total = len(selected)
    counters = {"ok": 0, "failed": 0, "skipped": 0}

    for index, item in enumerate(selected, start=1):
        if cancel is not None and cancel.is_set():
            items.append(
                {
                    "source": item.get("source"),
                    "destination": item.get("destination"),
                    "status": "skipped",
                    "error": "Annulé par l'utilisateur",
                }
            )
            counters["skipped"] += 1
            continue

        source_email = item.get("source", "")
        target_email = item.get("destination", "")
        if not source_email or not target_email:
            counters["skipped"] += 1
            continue

        if progress:
            progress(index - 1, total, "Boîte %d/%d : %s" % (index, total, source_email))

        record: Dict[str, Any] = {
            "source": source_email,
            "destination": target_email,
            "type": item.get("type", "account"),
            "size": item.get("size"),
            "messages": item.get("messages"),
            "started_at": now_iso(),
            "contents": [],
        }
        archives: List[Path] = []

        try:
            size = item.get("size")
            remaining = free_space(run_dir)
            if size and remaining and int(size) * 1.15 > remaining:
                raise MailboxError(
                    "Espace disque insuffisant : %s requis, %s libre dans %s"
                    % (human_size(size), human_size(remaining), run_dir)
                )
            if not _account_exists(destination, target_email):
                raise MailboxError(
                    "Le compte %s n'existe pas sur la destination : appliquez "
                    "d'abord l'étape Provisioning." % target_email
                )
            destination_quota = _destination_quota(destination, target_email)
            if destination_quota and size and int(size) > destination_quota:
                raise MailboxError(
                    "Quota destination insuffisant pour %s : boite source %s, "
                    "quota destination %s. Augmentez le quota du compte avant l'import."
                    % (
                        target_email,
                        human_size(int(size)),
                        human_size(destination_quota),
                    )
                )

            for content in contents:
                label = CONTENT_SPECS.get(content, {}).get("label", content)
                suffix = "" if content == "all" else "." + content
                archive = run_dir / (
                    "%s%s.tgz" % (_safe_filename(source_email), suffix)
                )
                archives.append(archive)
                if progress:
                    progress(
                        index - 1,
                        total,
                        "%s — %s : export" % (source_email, label),
                    )
                export_info = export_mailbox(
                    config,
                    source,
                    source_email,
                    archive,
                    content=content,
                    progress=lambda done, total_bytes, message: (
                        progress(
                            index - 1,
                            total,
                            "%s — %s" % (
                                message,
                                human_size(done),
                            ),
                        )
                        if progress
                        else None
                    ),
                )
                if export_info.get("empty"):
                    record["contents"].append(
                        {
                            "content": content,
                            "label": label,
                            "status": "empty",
                            "bytes": 0,
                        }
                    )
                    continue
                dedup_info: Dict[str, Any] = {}
                import_archive = archive
                if skip_duplicates:
                    if progress:
                        progress(
                            index - 1,
                            total,
                            "%s - %s : recherche des doublons" % (
                                source_email,
                                label,
                            ),
                        )
                    dedup_info = deduplicate_archive(
                        destination, target_email, archive, content=content
                    )
                    if dedup_info.get("all_duplicates"):
                        filtered_path = Path(
                            dedup_info.get("archive") or str(archive)
                        )
                        if filtered_path != archive and filtered_path.exists():
                            archives.append(filtered_path)
                        record["contents"].append(
                            {
                                "content": content,
                                "label": label,
                                "status": "skipped",
                                "archive": str(archive),
                                "bytes": export_info["bytes"],
                                "skipped_duplicates": dedup_info.get(
                                    "duplicates", 0
                                ),
                                "export_seconds": export_info["seconds"],
                                "backend_export": export_info["backend"],
                            }
                        )
                        continue
                    filtered = Path(dedup_info.get("archive") or str(archive))
                    if filtered != archive:
                        archives.append(filtered)
                        import_archive = filtered
                if progress:
                    progress(
                        index - 1,
                        total,
                        "%s — %s : import" % (source_email, label),
                    )
                import_info = import_mailbox(
                    config,
                    destination,
                    target_email,
                    import_archive,
                    resolve=resolve,
                    progress=lambda done, total_bytes, message: (
                        progress(
                            index - 1,
                            total,
                            "%s — %s" % (
                                message,
                                human_size(done),
                            ),
                        )
                        if progress
                        else None
                    ),
                )
                record["contents"].append(
                    {
                        "content": content,
                        "label": label,
                        "status": "ok",
                        "archive": str(archive),
                        "import_archive": str(import_archive),
                        "bytes": export_info["bytes"],
                        "skipped_duplicates": dedup_info.get("duplicates", 0),
                        "filtered": bool(dedup_info.get("filtered")),
                        "export_seconds": export_info["seconds"],
                        "import_seconds": import_info["seconds"],
                        "backend_export": export_info["backend"],
                    }
                )
            record.update(
                {
                    "status": "ok",
                    "resolve": resolve,
                    "contents_count": len(record["contents"]),
                }
            )
            counters["ok"] += 1
        except Exception as exc:  # noqa: BLE001 - one failure must not stop the run
            record.update(
                {
                    "status": "failed",
                    "error": describe_error(exc),
                    "archive": str(archives[-1])
                    if archives and archives[-1].exists()
                    else "",
                }
            )
            counters["failed"] += 1
        finally:
            if not keep_archives and record.get("status") == "ok":
                for archive in archives:
                    try:
                        archive.unlink()
                    except OSError:
                        pass
                record["archive"] = ""
            record["finished_at"] = now_iso()
            items.append(record)
            if progress:
                progress(
                    index,
                    total,
                    "%s : %s" % (target_email, record.get("status", "?")),
                )

    report = {
        "created_at": now_iso(),
        "source_host": config_module.store_host(config.connection("source")),
        "source_proxy_host": config_module.proxy_host(config.connection("source")),
        "profile": config.current_profile(),
        "profile_name": config.profile_name(),
        "destination_host": config_module.store_host(config.connection("destination")),
        "destination_proxy_host": config_module.proxy_host(config.connection("destination")),
        "run_dir": str(run_dir),
        "content_types": contents,
        "skip_duplicates": skip_duplicates,
        "items": items,
        "summary": counters,
        "total": total,
    }
    report_path = config.storage_path("report_path") / ("mailboxes-%s.json" % stamp)
    _write_json(report_path, report)
    report["report_path"] = str(report_path)
    return report

INCREMENTAL_CHUNK = 200
IMPORT_CHUNK_BYTES = 1024 * 1024 * 1024
IMPORT_CHUNK_ITEMS = 100


def _destination_has_kind(client: Any, kind: str) -> bool:
    if kind == "mail":
        return _destination_has_messages(client)
    search = STRUCTURED_SEARCH.get(kind)
    if not search:
        return False
    return next(_search_pages(client, search[0], search[1]), None) is not None


def _entry_keys(entries: Iterable[Dict[str, Any]]) -> Set[str]:
    keys: Set[str] = set()
    for entry in entries:
        keys.update(entry.get("keys") or set())
    return keys


def _chunk_entries(
    entries: List[Dict[str, Any]],
    max_bytes: int = IMPORT_CHUNK_BYTES,
    max_items: int = IMPORT_CHUNK_ITEMS,
) -> List[List[Dict[str, Any]]]:
    chunks: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    current_bytes = 0
    for entry in entries:
        item_size = int(entry.get("size") or 0)
        if current and (
            current_bytes + item_size > max_bytes or len(current) >= max_items
        ):
            chunks.append(current)
            current = []
            current_bytes = 0
        current.append(entry)
        current_bytes += item_size
    if current:
        chunks.append(current)
    return chunks


def _missing_entries(
    entries: Iterable[Dict[str, Any]], destination_keys: Set[str]
) -> List[Dict[str, Any]]:
    missing: List[Dict[str, Any]] = []
    for entry in entries:
        item_id = str(entry.get("id") or "")
        keys = entry.get("keys") or set()
        if item_id and (not keys or not keys.intersection(destination_keys)):
            missing.append(entry)
    return missing


def _source_all_entries(client: Any, since: str = "") -> List[Dict[str, Any]]:
    entries = _source_mail_entries(client, since=since)
    for kind in STRUCTURED_SEARCH:
        entries.extend(_source_structured_entries(client, kind))
    return entries


def _missing_entry_ids(
    entries: Iterable[Dict[str, Any]], destination_keys: Set[str]
) -> List[str]:
    missing: List[str] = []
    for entry in entries:
        item_id = str(entry.get("id") or "")
        keys = entry.get("keys") or set()
        if not item_id:
            continue
        if not keys or not keys.intersection(destination_keys):
            missing.append(item_id)
    return missing


def _full_content_record(
    config: config_module.Config,
    source: ZimbraSession,
    destination: ZimbraSession,
    source_email: str,
    target_email: str,
    content: str,
    label: str,
    run_dir: Path,
    archives: List[Path],
    progress: Progress,
    index: int,
    total: int,
    skip_duplicates: bool,
    since: str = "",
) -> Dict[str, Any]:
    suffix = "" if content == "all" else "." + content
    archive = run_dir / ("%s%s.tgz" % (_safe_filename(source_email), suffix))
    archives.append(archive)
    if progress:
        progress(index - 1, total, "%s - %s : export" % (source_email, label))
    export_info = export_mailbox(
        config,
        source,
        source_email,
        archive,
        content=content,
        query=(since or None) if content in {"mail", "all"} else None,
        progress=lambda done, total_bytes, message: (
            progress(
                index - 1,
                total,
                "%s - %s" % (message, human_size(done)),
            )
            if progress
            else None
        ),
    )
    if export_info.get("empty"):
        return {
            "content": content,
            "label": label,
            "status": "empty",
            "bytes": 0,
            "mode": "full",
        }
    dedup_info: Dict[str, Any] = {}
    import_archive = archive
    if skip_duplicates:
        dedup_info = deduplicate_archive(
            destination, target_email, archive, content=content
        )
        if dedup_info.get("all_duplicates"):
            filtered_path = Path(dedup_info.get("archive") or str(archive))
            if filtered_path != archive and filtered_path.exists():
                archives.append(filtered_path)
            return {
                "content": content,
                "label": label,
                "status": "skipped",
                "archive": str(archive),
                "bytes": export_info["bytes"],
                "skipped_duplicates": dedup_info.get("duplicates", 0),
                "mode": "full",
            }
        filtered = Path(dedup_info.get("archive") or str(archive))
        if filtered != archive:
            archives.append(filtered)
            import_archive = filtered
    if progress:
        progress(index - 1, total, "%s - %s : import" % (source_email, label))
    import_info = import_mailbox(
        config,
        destination,
        target_email,
        import_archive,
        resolve="skip",
        progress=lambda done, total_bytes, message: (
            progress(
                index - 1,
                total,
                "%s - %s" % (message, human_size(done)),
            )
            if progress
            else None
        ),
    )
    return {
        "content": content,
        "label": label,
        "status": "ok",
        "archive": str(archive),
        "import_archive": str(import_archive),
        "bytes": export_info["bytes"],
        "skipped_duplicates": dedup_info.get("duplicates", 0),
        "export_seconds": export_info["seconds"],
        "import_seconds": import_info["seconds"],
        "backend_export": export_info["backend"],
        "mode": "full",
    }


def _incremental_content_record(
    config: config_module.Config,
    source: ZimbraSession,
    destination: ZimbraSession,
    source_client: Any,
    destination_client: Any,
    source_email: str,
    target_email: str,
    content: str,
    label: str,
    run_dir: Path,
    archives: List[Path],
    progress: Progress,
    index: int,
    total: int,
    compare_destination: bool = True,
    mailbox_size: int = 0,
    since: str = "",
) -> Dict[str, Any]:
    mode = "incremental" if compare_destination else "chunked"
    if not since and config is not None:
        since = since_query(config.sync().get("since") or "")
    entries: List[Dict[str, Any]] = []
    source_skipped: List[int] = []
    destination_keys: Set[str] = set()
    header_archive: Optional[Path] = None

    if not compare_destination:
        if source_client is None:
            return _full_content_record(
                config,
                source,
                destination,
                source_email,
                target_email,
                content,
                label,
                run_dir,
                archives,
                progress,
                index,
                total,
                False,
                since=since,
            )
        try:
            if content == "all":
                entries = _source_all_entries(source_client, since=since)
            elif content == "mail":
                entries = _source_mail_entries(
                    source_client, skipped=source_skipped, since=since
                )
            else:
                entries = _source_structured_entries(source_client, content)
        except Exception as exc:  # noqa: BLE001 - source index can be broken
            fallback = _full_content_record(
                config,
                source,
                destination,
                source_email,
                target_email,
                content,
                label,
                run_dir,
                archives,
                progress,
                index,
                total,
                False,
                since=since,
            )
            fallback["warnings"] = [
                "Index source illisible pour %s : export complet de secours (%s)"
                % (content, describe_error(exc))
            ]
            return fallback
        if not entries:
            return {
                "content": content,
                "label": label,
                "status": "empty",
                "mode": mode,
                "bytes": 0,
            }
    else:
        if not _destination_has_kind(destination_client, content):
            return _full_content_record(
                config,
                source,
                destination,
                source_email,
                target_email,
                content,
                label,
                run_dir,
                archives,
                progress,
                index,
                total,
                True,
                since=since,
            )

        if content == "mail":
            header_archive = run_dir / (
                "%s.mail.headers.tgz" % _safe_filename(source_email)
            )
            archives.append(header_archive)
            if progress:
                progress(
                    index - 1,
                    total,
                    "%s - %s : lecture des identifiants"
                    % (source_email, label),
                )
            export_mailbox(
                config,
                source,
                source_email,
                header_archive,
                content="mail",
                query=since or None,
                body=0,
                progress=lambda done, total_bytes, message: (
                    progress(
                        index - 1,
                        total,
                        "%s - %s : %s" % (source_email, label, message),
                    )
                    if progress
                    else None
                ),
            )
            entries = list(
                _collect_archive_entries(header_archive, content="mail").values()
            )
            destination_keys = (
                _destination_mail_keys(
                    destination_client, _entry_keys(entries)
                )
                if entries
                else set()
            )
        else:
            if source_client is None:
                return _full_content_record(
                    config,
                    source,
                    destination,
                    source_email,
                    target_email,
                    content,
                    label,
                    run_dir,
                    archives,
                    progress,
                    index,
                    total,
                    True,
                    since=since,
                )
            entries = _source_structured_entries(source_client, content)
            destination_keys = _destination_structured_keys(
                destination_client, {content}
            )

    if not entries or any(not entry.get("id") for entry in entries):
        return _full_content_record(
            config,
            source,
            destination,
            source_email,
            target_email,
            content,
            label,
            run_dir,
            archives,
            progress,
            index,
            total,
            compare_destination,
            since=since,
        )

    missing = (
        _missing_entries(entries, destination_keys)
        if compare_destination
        else list(entries)
    )
    missing_keys = sorted(
        {
            key
            for entry in missing
            for key in (entry.get("keys") or set())
        }
    )
    if not missing:
        return {
            "content": content,
            "label": label,
            "status": "skipped",
            "mode": mode,
            "skipped_duplicates": len(entries),
            "bytes": 0,
            "missing_keys": [],
        }

    chunks = _chunk_entries(missing)
    chunk_records: List[Dict[str, Any]] = []
    suffix = "" if content == "all" else "." + content
    total_items = len(missing)
    total_bytes = sum(int(entry.get("size") or 0) for entry in missing)
    total_lots = len(chunks)
    done_items = 0
    done_bytes = 0
    done_lots = 0

    def detail() -> Dict[str, Any]:
        return {
            "account": source_email,
            "mailbox_size": mailbox_size,
            "bytes_done": done_bytes,
            "bytes_total": total_bytes,
            "items_done": done_items,
            "items_total": total_items,
            "lots_done": done_lots,
            "lots_total": total_lots,
            "account_index": index,
            "account_total": total,
        }

    for part_number, chunk in enumerate(chunks, start=1):
        archive = run_dir / (
            "%s%s.%s-%03d.tgz"
            % (_safe_filename(source_email), suffix, mode, part_number)
        )
        archives.append(archive)
        chunk_ids = [str(entry["id"]) for entry in chunk]
        chunk_bytes = sum(int(entry.get("size") or 0) for entry in chunk)
        _emit_progress(
            progress,
            index - 1,
            total,
            "%s - %s : export %d/%d (%d elements)"
            % (
                source_email,
                label,
                part_number,
                total_lots,
                len(chunk_ids),
            ),
            detail(),
        )
        export_info = export_mailbox(
            config,
            source,
            source_email,
            archive,
            content=content,
            query="item:(%s)" % ",".join(chunk_ids),
            progress=lambda done, total_bytes, message: (
                progress(
                    index - 1,
                    total,
                    "%s - %s" % (message, human_size(done)),
                )
                if progress
                else None
            ),
        )
        if export_info.get("empty"):
            continue
        _emit_progress(
            progress,
            index - 1,
            total,
            "%s - %s : import %d/%d"
            % (source_email, label, part_number, total_lots),
            detail(),
        )
        import_info = import_mailbox(
            config,
            destination,
            target_email,
            archive,
            resolve="skip",
            progress=lambda done, total_bytes, message: (
                progress(
                    index - 1,
                    total,
                    "%s - %s" % (message, human_size(done)),
                )
                if progress
                else None
            ),
        )
        done_items += len(chunk_ids)
        done_bytes += chunk_bytes
        done_lots += 1
        _emit_progress(
            progress,
            index - 1,
            total,
            "%s - %s : import termine %d/%d"
            % (source_email, label, done_lots, total_lots),
            detail(),
        )
        chunk_records.append(
            {
                "archive": str(archive),
                "items": len(chunk_ids),
                "bytes": export_info["bytes"],
                "import_seconds": import_info["seconds"],
            }
        )

    return {
        "content": content,
        "label": label,
        "status": "ok" if chunk_records else "skipped",
        "mode": mode,
        "items": len(missing),
        "chunks": chunk_records,
        "bytes": sum(record["bytes"] for record in chunk_records),
        "missing_keys": missing_keys,
        "skipped_duplicates": len(entries) - len(missing),
        "warnings": (
            ["%d element(s) source illisible(s) ignore(s)" % len(source_skipped)]
            if source_skipped
            else []
        ),
    }


def _copy_incremental_account(
    config: config_module.Config,
    item: Dict[str, Any],
    source: ZimbraSession,
    destination: ZimbraSession,
    contents: List[str],
    run_dir: Path,
    keep_archives: bool,
    progress: Progress,
    cancel: Optional[threading.Event],
    index: int,
    total: int,
    compare_destination: bool,
    since: str = "",
) -> Dict[str, Any]:
    source_email = item.get("source", "")
    target_email = item.get("destination", "")
    if not source_email or not target_email:
        return {
            "source": source_email,
            "destination": target_email,
            "status": "skipped",
            "error": "Compte source ou destination vide",
            "finished_at": now_iso(),
        }
    if cancel is not None and cancel.is_set():
        return {
            "source": source_email,
            "destination": target_email,
            "status": "skipped",
            "error": "Annule par l'utilisateur",
            "finished_at": now_iso(),
        }

    record: Dict[str, Any] = {
        "source": source_email,
        "destination": target_email,
        "type": item.get("type", "account"),
        "size": item.get("size"),
        "messages": item.get("messages"),
        "started_at": now_iso(),
        "contents": [],
        "warnings": [],
    }
    archives: List[Path] = []
    try:
        size = item.get("size")
        remaining = free_space(run_dir)
        if size and remaining and int(size) * 1.15 > remaining:
            raise MailboxError(
                "Espace disque insuffisant : %s requis, %s libre dans %s"
                % (human_size(size), human_size(remaining), run_dir)
            )
        if not _account_exists(destination, target_email):
            raise MailboxError(
                "Le compte %s n'existe pas sur la destination : appliquez "
                "d'abord l'etape Provisioning." % target_email
            )
        mailbox_size = _mailbox_used(source, source_email) or int(
            item.get("size") or 0
        )
        destination_quota = _destination_quota(destination, target_email)
        if destination_quota and mailbox_size > destination_quota:
            raise MailboxError(
                "Quota destination insuffisant pour %s : boite source %s, "
                "quota destination %s. Augmentez le quota du compte avant l'import."
                % (
                    target_email,
                    human_size(mailbox_size),
                    human_size(destination_quota),
                )
            )
        record["mailbox_size"] = mailbox_size
        _emit_progress(
            progress,
            index - 1,
            total,
            "Boite %d/%d : %s - BAL %s"
            % (index, total, source_email, human_size(mailbox_size)),
            {
                "account": source_email,
                "mailbox_size": mailbox_size,
                "account_index": index,
                "account_total": total,
            },
        )

        destination_client = (
            destination.mail_client(target_email) if compare_destination else None
        )
        source_client = None
        needs_source_mail = any(
            content in STRUCTURED_SEARCH for content in contents
        ) or (not compare_destination and any(
            content in {"mail", "all"} for content in contents
        ))
        if needs_source_mail:
            source_client = source.mail_client(source_email)

        for content in contents:
            label = CONTENT_SPECS.get(content, {}).get("label", content)
            if content not in CONTENT_SPECS:
                continue
            content_record = _incremental_content_record(
                config,
                source,
                destination,
                source_client,
                destination_client,
                source_email,
                target_email,
                content,
                label,
                run_dir,
                archives,
                progress,
                index,
                total,
                compare_destination,
                mailbox_size=mailbox_size,
                since=since,
            )
            record["contents"].append(content_record)
            record["warnings"].extend(content_record.get("warnings") or [])

        record.update(
            {
                "status": "ok",
                "resolve": "skip",
                "incremental": compare_destination,
                "chunked": not compare_destination,
                "contents_count": len(record["contents"]),
            }
        )
    except Exception as exc:  # noqa: BLE001 - one failure must not stop the run
        record.update(
            {
                "status": "failed",
                "error": describe_error(exc),
                "archive": str(archives[-1])
                if archives and archives[-1].exists()
                else "",
            }
        )
    finally:
        if not keep_archives and record.get("status") == "ok":
            for archive in archives:
                try:
                    archive.unlink()
                except OSError:
                    pass
            record["archive"] = ""
        record["finished_at"] = now_iso()
    return record


def _run_account_tasks(
    config: config_module.Config,
    selected: List[Dict[str, Any]],
    total: int,
    progress: Progress,
    cancel: Optional[threading.Event],
    parallelism: int,
    worker: Callable[..., Dict[str, Any]],
    run_dir: Path,
    keep_archives: bool,
    contents: List[str],
    compare_destination: bool,
    since: str = "",
) -> List[Dict[str, Any]]:
    results: List[Optional[Dict[str, Any]]] = [None] * total
    profile_id = config.current_profile()
    local = threading.local()
    progress_lock = threading.Lock()
    completed = 0

    def sessions():
        if not hasattr(local, "source"):
            local.source = ZimbraSession(config, "source", readonly=True)
            local.destination = ZimbraSession(
                config, "destination", readonly=False
            )
        return local.source, local.destination

    def worker_progress(
        done: int,
        total_count: int,
        message: str = "",
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not progress:
            return
        with progress_lock:
            current = completed
        _emit_progress(progress, current, total, message, detail)

    def task(index: int, item: Dict[str, Any]) -> Dict[str, Any]:
        with config.profile_context(profile_id):
            source, destination = sessions()
            try:
                return worker(
                    config,
                    item,
                    source,
                    destination,
                    contents,
                    run_dir,
                    keep_archives,
                    worker_progress,
                    cancel,
                    index,
                    total,
                    compare_destination,
                    since,
                )
            except Exception as exc:  # noqa: BLE001 - keep other accounts running
                return {
                    "source": item.get("source"),
                    "destination": item.get("destination"),
                    "status": "failed",
                    "error": describe_error(exc),
                    "finished_at": now_iso(),
                }

    def completed_task(index: int, record: Dict[str, Any]) -> None:
        nonlocal completed
        results[index - 1] = record
        with progress_lock:
            completed += 1
            if progress:
                progress(
                    completed,
                    total,
                    "%s : %s"
                    % (
                        record.get("destination")
                        or record.get("source")
                        or "?",
                        record.get("status", "?"),
                    ),
                )

    if parallelism <= 1 or total <= 1:
        for index, item in enumerate(selected, start=1):
            completed_task(index, task(index, item))
    else:
        with ThreadPoolExecutor(
            max_workers=min(parallelism, max(1, total))
        ) as executor:
            futures = {
                executor.submit(task, index, item): index
                for index, item in enumerate(selected, start=1)
            }
            for future in as_completed(futures):
                completed_task(futures[future], future.result())
    return [record or {} for record in results]


def copy_mailboxes_incremental(
    config: config_module.Config,
    plan: Dict[str, Any],
    progress: Progress = None,
    cancel: Optional[threading.Event] = None,
    only: Optional[List[str]] = None,
    keep_archives: bool = True,
    options: Optional[Dict[str, Any]] = None,
    content_types: Optional[List[str]] = None,
    compare_destination: bool = True,
) -> Dict[str, Any]:
    """Copy only items that are missing on the destination.

    Mail is discovered with a headers-only export, then missing source item
    IDs are exported in chunks with ``item:(...)``.  Calendar, contacts and
    tasks are discovered through SOAP searches.
    """
    sync = dict(config.sync())
    sync.update(plan.get("options") or {})
    sync.update(options or {})
    if not sync.get("copy_mailbox", True):
        return {
            "created_at": now_iso(),
            "items": [],
            "summary": {"ok": 0, "failed": 0, "skipped": 0},
            "skipped": "copy_mailbox is disabled",
        }

    raw_contents = (
        content_types if content_types is not None else sync.get("content_types")
    )
    contents = normalize_content_types(raw_contents)
    since = since_query(sync.get("since") or "")
    if "all" in contents and compare_destination:
        return copy_mailboxes(
            config,
            plan,
            progress=progress,
            cancel=cancel,
            only=only,
            keep_archives=keep_archives,
            options=options,
            content_types=raw_contents,
        )

    source_host = config_module.store_host(config.connection("source"))
    destination_host = config_module.store_host(config.connection("destination"))
    

    check = preflight(config, ("source", "destination"))
    if not check.get("ok"):
        details = []
        for side, entry in check.get("sides", {}).items():
            for error in entry.get("errors", []):
                details.append("%s : %s" % (side, error))
        raise MailboxError(
            "Preflight impossible avant la copie des boites : "
            + " | ".join(details)
        )

    storage = config.storage_path("mailbox_path")
    try:
        cleanup_archives(config, int(sync.get("keep_tgz_days") or 0))
    except Exception:  # noqa: BLE001 - maintenance must never block a copy
        pass
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = storage / ("run-" + stamp)
    run_dir.mkdir(parents=True, exist_ok=True)

    selected = plan.get("mailboxes", [])
    if only:
        wanted = {value.lower() for value in only}
        selected = [
            item
            for item in selected
            if item.get("source", "").lower() in wanted
            or item.get("destination", "").lower() in wanted
        ]

    total = len(selected)
    try:
        parallelism = max(1, int(sync.get("parallelism") or 1))
    except (TypeError, ValueError):
        parallelism = 1

    items = _run_account_tasks(
        config,
        selected,
        total,
        progress,
        cancel,
        parallelism,
        _copy_incremental_account,
        run_dir,
        keep_archives,
        contents,
        compare_destination,
        since,
    )
    counters = {"ok": 0, "failed": 0, "skipped": 0}
    for record in items:
        status = record.get("status")
        if status == "failed":
            counters["failed"] += 1
        elif status == "skipped":
            counters["skipped"] += 1
        else:
            counters["ok"] += 1

    report = {
        "created_at": now_iso(),
        "source_host": config_module.store_host(config.connection("source")),
        "source_proxy_host": config_module.proxy_host(config.connection("source")),
        "profile": config.current_profile(),
        "profile_name": config.profile_name(),
        "destination_host": config_module.store_host(config.connection("destination")),
        "destination_proxy_host": config_module.proxy_host(
            config.connection("destination")
        ),
        "run_dir": str(run_dir),
        "content_types": contents,
        "incremental": compare_destination,
        "chunked": not compare_destination,
        "parallelism": parallelism,
        "items": items,
        "summary": counters,
        "total": total,
    }
    report_path = config.storage_path("report_path") / (
        "mailboxes-%s.json" % stamp
    )
    _write_json(report_path, report)
    report["report_path"] = str(report_path)
    return report


def copy_mailboxes_chunked(
    config: config_module.Config,
    plan: Dict[str, Any],
    progress: Progress = None,
    cancel: Optional[threading.Event] = None,
    only: Optional[List[str]] = None,
    keep_archives: bool = True,
    options: Optional[Dict[str, Any]] = None,
    content_types: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Export and import every mailbox in 1 GiB item chunks."""
    return copy_mailboxes_incremental(
        config,
        plan,
        progress=progress,
        cancel=cancel,
        only=only,
        keep_archives=keep_archives,
        options=options,
        content_types=content_types,
        compare_destination=False,
    )


def _safe_filename(email: str) -> str:
    return "".join(
        char if char.isalnum() or char in "@._-" else "_" for char in email
    )


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
        handle.write("\n")


def cleanup_partial_archives(config: config_module.Config) -> List[str]:
    """Remove interrupted .part/.tmp archives from mailbox run directories."""
    removed: List[str] = []
    storage = config.storage_path("mailbox_path")
    if not storage.exists():
        return removed
    for pattern in ("run-*/*.part", "run-*/*.tmp", "run-*/**/*.part"):
        for path in storage.glob(pattern):
            try:
                path.unlink()
                removed.append(str(path))
            except OSError:
                continue
    return removed


def cleanup_archives(config: config_module.Config, older_than_days: int) -> List[str]:
    """Delete mailbox archives older than ``older_than_days``. Returns paths."""
    removed: List[str] = []
    if older_than_days <= 0:
        return removed
    cutoff = time.time() - older_than_days * 86400
    storage = config.storage_path("mailbox_path")
    if not storage.exists():
        return removed
    for path in storage.glob("run-*/*.tgz"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed.append(str(path))
        except OSError:
            continue
    for directory in storage.glob("run-*"):
        try:
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        except OSError:
            continue
    return removed
