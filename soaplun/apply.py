"""Apply a provisioning plan to the destination server.

Only create/update operations are implemented.  There is deliberately no
delete/unset path: the destination is never pruned.
"""

import datetime
import json
import string
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from zimsoap.client import ZimbraSoapServerError

from . import config as config_module
from .connections import ZimbraSession, describe_error
from .inventory import now_iso
from .diff import action_sort_key

Progress = Optional[Callable[[int, int, str], None]]

_RUN_OPTIONS = threading.local()


def _current_options() -> Dict[str, Any]:
    return getattr(_RUN_OPTIONS, "value", {}) or {}


class ApplyError(Exception):
    pass


class SafetyError(ApplyError):
    pass


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def a_list(attrs: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Convert a dict into Zimbra ``<a>`` entries (repeated keys supported)."""
    entries: List[Dict[str, Any]] = []
    for name, value in (attrs or {}).items():
        if isinstance(value, (list, tuple)):
            for item in value:
                entries.append({"n": name, "_content": item})
        else:
            entries.append({"n": name, "_content": value})
    return entries


def resolve_id(session: ZimbraSession, kind: str, name: str) -> str:
    selectors = {
        "account": ("GetAccount", "account"),
        "domain": ("GetDomain", "domain"),
        "cos": ("GetCos", "cos"),
        "dl": ("GetDistributionList", "dl"),
        "resource": ("GetCalendarResource", "calresource"),
    }
    command, key = selectors[kind]
    payload = session.request_single(
        command, {key: {"by": "name", "_content": name}}
    )
    if isinstance(payload, dict) and key in payload:
        payload = payload[key]
    identifier = ""
    if isinstance(payload, dict):
        identifier = str(payload.get("id") or "")
    if not identifier:
        raise ApplyError("Impossible de trouver l'identifiant de %s %r" % (kind, name))
    return identifier


def random_password(length: int = 24) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%*-_"
    length = max(4, int(length))
    chars = [secrets.choice(alphabet) for _ in range(length)]
    chars[0] = secrets.choice(string.digits)
    chars[1] = secrets.choice(string.ascii_lowercase)
    chars[2] = secrets.choice(string.ascii_uppercase)
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def _is_already_exists(exc: BaseException) -> bool:
    if isinstance(exc, ZimbraSoapServerError):
        code = (exc.code or "").lower()
        msg = (exc.msg or "").lower()
        return (
            "already exists" in msg
            or "exists" in code
            or code in {"account.account_exists", "domain.domain_exists"}
        )
    return False


def _backup(
    config: config_module.Config, stamp: str, kind: str, target: str, payload: Any
) -> str:
    directory = config.storage_path("report_path") / "backups" / stamp
    directory.mkdir(parents=True, exist_ok=True)
    filename = "%s-%s.json" % (
        kind,
        "".join(c if c.isalnum() or c in "@._-" else "_" for c in target),
    )
    path = directory / filename
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
        handle.write("\n")
    return str(path)


def _backup_object(session: ZimbraSession, kind: str, name: str) -> Dict[str, Any]:
    mapping = {
        "account": ("GetAccount", "account"),
        "cos": ("GetCos", "cos"),
        "dl": ("GetDistributionList", "dl"),
        "resource": ("GetCalendarResource", "calresource"),
        "domain": ("GetDomain", "domain"),
    }
    command, key = mapping[kind]
    try:
        payload = session.request_single(
            command, {key: {"by": "name", "_content": name}, "applyCos": "0"}
            if kind == "account"
            else {key: {"by": "name", "_content": name}},
        )
        return payload if isinstance(payload, dict) else {"payload": payload}
    except Exception as exc:  # noqa: BLE001 - a backup failure must be visible
        return {"error": describe_error(exc), "name": name}


# ----------------------------------------------------------------------
# Action handlers
# ----------------------------------------------------------------------
def _create_domain(session: ZimbraSession, config, payload) -> str:
    content = {"name": payload["name"]}
    entries = a_list(payload.get("attrs") or {})
    if entries:
        content["a"] = entries
    session.request("CreateDomain", content)
    return "domaine créé"


def _create_domain_alias(session: ZimbraSession, config, payload) -> str:
    target_id = resolve_id(session, "domain", payload["alias_target"])
    content = {
        "name": payload["name"],
        "a": [
            {"n": "zimbraDomainType", "_content": "alias"},
            {"n": "zimbraDomainAliasTargetId", "_content": target_id},
        ],
    }
    session.request("CreateDomain", content)
    return "domaine alias créé → %s" % payload["alias_target"]


def _modify_domain(session: ZimbraSession, config, payload) -> str:
    identifier = payload.get("id") or resolve_id(session, "domain", payload["name"])
    session.request(
        "ModifyDomain",
        {"id": identifier, "a": a_list(payload.get("attrs") or {})},
    )
    return "domaine mis à jour"


def _create_cos(session: ZimbraSession, config, payload) -> str:
    content = {"name": payload["name"]}
    entries = a_list(payload.get("attrs") or {})
    if entries:
        content["a"] = entries
    session.request("CreateCos", content)
    return "classe de service créée"


def _modify_cos(session: ZimbraSession, config, payload) -> str:
    identifier = payload.get("id") or resolve_id(session, "cos", payload["name"])
    session.request(
        "ModifyCos", {"id": identifier, "a": a_list(payload.get("attrs") or {})}
    )
    return "classe de service mise à jour"


def _create_account(session: ZimbraSession, config, payload) -> str:
    password = _password(config, payload.get("password_source"))
    content = {
        "name": payload["name"],
        "password": password,
        "a": a_list(payload.get("attrs") or {}),
    }
    cos_name = payload.get("cos_name") or ""
    if cos_name:
        content["cos"] = {"by": "name", "_content": cos_name}
    session.request("CreateAccount", content)
    return "compte créé"


def _modify_account(session: ZimbraSession, config, payload) -> str:
    identifier = payload.get("id") or resolve_id(
        session, "account", payload["name"]
    )
    content: Dict[str, Any] = {
        "id": identifier,
        "a": a_list(payload.get("attrs") or {}),
    }
    cos_name = payload.get("cos_name") or ""
    if cos_name:
        content["cos"] = {"by": "name", "_content": cos_name}
    session.request("ModifyAccount", content)
    return "compte mis à jour"


def _set_password(session: ZimbraSession, config, payload) -> str:
    identifier = payload.get("id") or resolve_id(
        session, "account", payload["name"]
    )
    password = _password(config, payload.get("password_source"))
    session.request("SetPassword", {"id": identifier, "newPassword": password})
    if payload.get("must_change"):
        session.request(
            "ModifyAccount",
            {
                "id": identifier,
                "a": [{"n": "zimbraPasswordMustChange", "_content": "TRUE"}],
            },
        )
    return "mot de passe réinitialisé"


def _add_alias(session: ZimbraSession, config, payload) -> str:
    identifier = resolve_id(session, "account", payload["account"])
    session.request(
        "AddAccountAlias", {"id": identifier, "alias": payload["alias"]}
    )
    return "alias ajouté"


def _set_delegated_sender(session: ZimbraSession, config, payload) -> str:
    identifier = payload.get("id") or resolve_id(
        session, "account", payload["name"]
    )
    values = [
        str(value)
        for value in (payload.get("values") or [])
        if str(value).strip()
    ]
    if not values:
        return "aucune adresse déléguée"
    session.request(
        "ModifyAccount",
        {
            "id": identifier,
            "a": [
                {"n": "zimbraPrefAllowAddressForDelegatedSender", "_content": value}
                for value in values
            ],
        },
    )
    return "adresses d'envoi délégué restaurées"


DL_MEMBER_CHUNK = 100


def _add_dl_members(
    session: ZimbraSession, name: str, members: List[str]
) -> None:
    for index in range(0, len(members), DL_MEMBER_CHUNK):
        chunk = members[index : index + DL_MEMBER_CHUNK]
        session.request(
            "AddDistributionListMember",
            {
                "dl": {"by": "name", "_content": name},
                "dlm": [{"_content": member} for member in chunk],
            },
        )


def _create_dl(session: ZimbraSession, config, payload) -> str:
    content: Dict[str, Any] = {"name": payload["name"]}
    entries = a_list(payload.get("attrs") or {})
    if entries:
        content["a"] = entries
    if payload.get("dynamic"):
        content["dynamic"] = "1"
    session.request("CreateDistributionList", content)
    members = payload.get("members") or []
    if members:
        _add_dl_members(session, payload["name"], members)
    return "liste cr??e" + (
        " avec %d membre(s)" % len(members) if members else ""
    )


def _modify_dl(session: ZimbraSession, config, payload) -> str:
    identifier = payload.get("id") or resolve_id(session, "dl", payload["name"])
    session.request(
        "ModifyDistributionList",
        {"id": identifier, "a": a_list(payload.get("attrs") or {})},
    )
    return "liste mise à jour"


def _add_dl_member(session: ZimbraSession, config, payload) -> str:
    members = payload.get("members") or []
    if members:
        _add_dl_members(session, payload["name"], members)
    return "%d membre(s) ajout?(s)" % len(members)


def _create_resource(session: ZimbraSession, config, payload) -> str:
    password = _password(config, payload.get("password_source"))
    content = {
        "name": payload["name"],
        "password": password,
        "a": a_list(payload.get("attrs") or {}),
    }
    session.request("CreateCalendarResource", content)
    return "ressource créée"


def _modify_resource(session: ZimbraSession, config, payload) -> str:
    identifier = payload.get("id") or resolve_id(
        session, "resource", payload["name"]
    )
    session.request(
        "ModifyCalendarResource",
        {"id": identifier, "a": a_list(payload.get("attrs") or {})},
    )
    return "ressource mise à jour"


HANDLERS: Dict[str, Callable[[ZimbraSession, Any, Dict[str, Any]], str]] = {
    "create_domain": _create_domain,
    "create_domain_alias": _create_domain_alias,
    "modify_domain": _modify_domain,
    "create_cos": _create_cos,
    "modify_cos": _modify_cos,
    "create_account": _create_account,
    "modify_account": _modify_account,
    "set_password": _set_password,
    "add_alias": _add_alias,
    "set_delegated_sender": _set_delegated_sender,
    "create_dl": _create_dl,
    "modify_dl": _modify_dl,
    "add_dl_member": _add_dl_member,
    "create_resource": _create_resource,
    "modify_resource": _modify_resource,
}

def action_type(kind: str) -> str:
    if kind in {"create_resource", "modify_resource"}:
        return "resource"
    if kind in {"create_dl", "modify_dl", "add_dl_member"}:
        return "dl"
    if kind in {
        "create_domain",
        "create_domain_alias",
        "modify_domain",
        "create_cos",
        "modify_cos",
    }:
        return "configuration"
    return "account"


BACKUP_KINDS = {
    "modify_account": "account",
    "set_password": "account",
    "set_delegated_sender": "account",
    "modify_cos": "cos",
    "modify_dl": "dl",
    "modify_resource": "resource",
    "modify_domain": "domain",
}


def _password(config: config_module.Config, source: Optional[str]) -> str:
    options = _current_options() or config.sync()
    if source == "fixed":
        password = options.get("fixed_password")
        if not isinstance(password, str) or not password:
            password = config.sync().get("fixed_password") or ""
        if not password:
            raise ApplyError(
                "Aucun mot de passe fixe n'est configuré dans ce plan : impossible "
                "de créer les comptes."
            )
        return password
    if source == "random":
        password = random_password()
        _RUN_OPTIONS.last_random = password
        return password
    raise ApplyError(
        "Mot de passe inconnu pour ce compte : configurez un mot de passe fixe "
        "dans le plan."
    )


# ----------------------------------------------------------------------
# Apply
# ----------------------------------------------------------------------
def check_safety(
    config: config_module.Config, plan: Optional[Dict[str, Any]] = None
) -> None:
    source = config.connection("source")
    destination = config.connection("destination")
    source_store = config_module.store_host(source)
    destination_store = config_module.store_host(destination)
    if not destination_store:
        raise SafetyError("La destination n'est pas configurée.")

    plan = plan or {}
    validation = plan.get("validation") or {}
    if validation and not validation.get("ok", True):
        raise SafetyError(
            "Plan invalide : " + " ".join(validation.get("errors") or [])
        )
    plan_destination = str((plan.get("destination") or {}).get("host") or "")
    plan_source = str((plan.get("source") or {}).get("host") or "")
    if plan_destination and destination_store and (
        plan_destination.lower() != destination_store.lower()
    ):
        raise SafetyError(
            "Le plan a été généré pour la destination %s mais la destination "
            "configurée est %s. Régénérez le plan."
            % (plan_destination, destination_store)
        )
    if (
        plan_source
        and source_store
        and plan_source.lower() != source_store.lower()
    ):
        raise SafetyError(
            "Le plan a été généré pour la source %s mais la source configurée est "
            "%s. Régénérez le plan." % (plan_source, source_store)
        )



def _action_in_accounts(
    item: Dict[str, Any], accounts: Optional[set]
) -> bool:
    if accounts is None:
        return True
    kind = item.get("kind", "")
    payload = item.get("payload") or {}
    if kind in {"create_account", "modify_account", "set_password", "set_delegated_sender"}:
        value = payload.get("name") or item.get("target", "")
    elif kind == "add_alias":
        value = payload.get("account") or ""
    else:
        return True
    return str(value).lower() in accounts


def plan_options_for_run(
    config: config_module.Config,
    plan: Dict[str, Any],
    overrides: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Resolve plan options and decrypt the fixed password for this run."""
    effective = dict(plan.get("options") or {})
    if overrides:
        effective.update(overrides)
    encrypted = effective.get("fixed_password_encrypted") or ""
    if encrypted:
        effective["fixed_password"] = config.decrypt_secret(encrypted)
    elif not isinstance(effective.get("fixed_password"), str):
        effective["fixed_password"] = ""
    return effective


def _finish_report(
    config,
    plan,
    dry_run,
    stamp,
    total,
    counters,
    items,
    generated_passwords,
):
    report = {
        "created_at": now_iso(),
        "dry_run": dry_run,
        "profile": config.current_profile(),
        "profile_name": config.profile_name(),
        "plan_created_at": plan.get("created_at", ""),
        "source_host": config_module.store_host(config.connection("source")),
        "source_proxy_host": config_module.proxy_host(config.connection("source")),
        "destination_host": config_module.store_host(config.connection("destination")),
        "destination_proxy_host": config_module.proxy_host(
            config.connection("destination")
        ),
        "total": total,
        "summary": counters,
        "items": items,
    }
    if not dry_run:
        path = config.storage_path("report_path") / ("apply-%s.json" % stamp)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=1)
            handle.write("\n")
        report["report_path"] = str(path)
    if generated_passwords:
        report["_generated_passwords"] = generated_passwords
    return report


def _apply_actions_parallel(
    config,
    actions,
    total,
    progress,
    cancel,
    stamp,
    options,
    workers,
    overall_total=None,
    offset=0,
):
    results = [None] * total
    profile = config.current_profile()
    local = threading.local()
    progress_lock = threading.Lock()
    completed = 0

    def session():
        if not hasattr(local, "session"):
            local.session = ZimbraSession(
                config, "destination", readonly=False
            )
        return local.session

    def task(index, item):
        _RUN_OPTIONS.value = options
        with config.profile_context(profile):
            kind = item.get("kind", "")
            target = item.get("target", "")
            record = {
                "id": item.get("id"),
                "kind": kind,
                "type": action_type(kind),
                "target": target,
                "description": item.get("description", ""),
                "started_at": now_iso(),
            }
            if cancel is not None and cancel.is_set():
                record.update(
                    {
                        "status": "skipped",
                        "error": "Annule par l'utilisateur",
                    }
                )
                return index, record, ""
            handler = HANDLERS.get(kind)
            if handler is None:
                record.update(
                    {
                        "status": "skipped",
                        "error": "Type d'action inconnu : %s" % kind,
                    }
                )
                return index, record, ""
            started = time.time()
            try:
                _RUN_OPTIONS.last_random = None
                backup_kind = BACKUP_KINDS.get(kind)
                if backup_kind:
                    previous = _backup_object(
                        session(), backup_kind, target
                    )
                    record["backup"] = _backup(
                        config, stamp, backup_kind, target, previous
                    )
                message = handler(
                    session(), config, item.get("payload") or {}
                )
                password = getattr(_RUN_OPTIONS, "last_random", None)
                record.update({"status": "ok", "message": message})
                return index, record, str(password) if password else ""
            except Exception as exc:  # noqa: BLE001 - reported per action
                if _is_already_exists(exc):
                    record.update(
                        {
                            "status": "skipped",
                            "message": "Deja present sur la destination",
                        }
                    )
                else:
                    record.update(
                        {"status": "failed", "error": describe_error(exc)}
                    )
                return index, record, ""
            finally:
                record["seconds"] = round(time.time() - started, 2)

    def completed_task(index, record, password):
        nonlocal completed
        results[index - 1] = record
        if password:
            record["_generated_password"] = password
        with progress_lock:
            completed += 1
            if progress:
                progress(
                    offset + completed,
                    overall_total or total,
                    "%s : %s"
                    % (record.get("kind", ""), record.get("target", "")),
                )

    if progress:
        progress(offset, overall_total or total, "En cours")
    with ThreadPoolExecutor(max_workers=min(workers, max(1, total))) as executor:
        futures = {
            executor.submit(task, index, item): index
            for index, item in enumerate(actions, start=1)
        }
        for future in as_completed(futures):
            index, record, password = future.result()
            completed_task(index, record, password)

    generated_passwords = {}
    counters = {"ok": 0, "failed": 0, "skipped": 0, "planned": 0}
    items = []
    for record in results:
        if record is None:
            continue
        password = record.pop("_generated_password", "")
        if password:
            generated_passwords[str(record.get("target", ""))] = password
        status = record.get("status", "failed")
        if status in counters:
            counters[status] += 1
        else:
            counters["failed"] += 1
        items.append(record)
    return items, counters, generated_passwords


def apply_plan(
    config: config_module.Config,
    plan: Dict[str, Any],
    progress: Progress = None,
    cancel: Optional[threading.Event] = None,
    dry_run: bool = False,
    stop_on_error: bool = False,
    options: Optional[Dict[str, Any]] = None,
    kinds: Optional[Iterable[str]] = None,
    accounts: Optional[Iterable[str]] = None,
    parallelism: int = 1,
) -> Dict[str, Any]:
    """Execute the create/update actions of ``plan`` on the destination."""
    check_safety(config, plan)
    effective_options = plan_options_for_run(config, plan, options)
    _RUN_OPTIONS.value = effective_options
    try:
        return _apply_plan_inner(
            config,
            plan,
            progress=progress,
            cancel=cancel,
            dry_run=dry_run,
            stop_on_error=stop_on_error,
            kinds=kinds,
            accounts=accounts,
            parallelism=parallelism,
        )
    finally:
        _RUN_OPTIONS.value = None


def _apply_plan_inner(
    config: config_module.Config,
    plan: Dict[str, Any],
    progress: Progress = None,
    cancel: Optional[threading.Event] = None,
    dry_run: bool = False,
    stop_on_error: bool = False,
    kinds: Optional[Iterable[str]] = None,
    accounts: Optional[Iterable[str]] = None,
    parallelism: int = 1,
) -> Dict[str, Any]:
    actions = plan.get("actions", [])
    if kinds is not None:
        allowed = set(kinds)
        actions = [item for item in actions if item.get("kind") in allowed]
    account_filter = None
    if accounts is not None:
        selected = {str(value).lower() for value in accounts}
        account_filter = set(selected)
        for pair in plan.get("accounts", []):
            if str(pair.get("source", "")).lower() in selected:
                destination = str(pair.get("destination", "")).lower()
                if destination:
                    account_filter.add(destination)
    if account_filter is not None:
        actions = [
            item for item in actions if _action_in_accounts(item, account_filter)
        ]
    total = len(actions)
    session = ZimbraSession(config, "destination", readonly=False)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    workers = max(1, int(parallelism or 1))
    if workers > 1 and total > 1 and not dry_run and not stop_on_error:
        groups = {}
        for item in actions:
            groups.setdefault(action_sort_key(item)[0], []).append(item)
        items = []
        counters = {"ok": 0, "failed": 0, "skipped": 0, "planned": 0}
        generated_passwords = {}
        offset = 0
        for order in sorted(groups):
            group = groups[order]
            ids = {str(item.get("id") or "") for item in group}
            dependent = any(
                str(dep) in ids
                for item in group
                for dep in (item.get("depends_on") or [])
            )
            group_workers = 1 if dependent else workers
            group_items, group_counters, group_passwords = (
                _apply_actions_parallel(
                    config,
                    group,
                    len(group),
                    progress,
                    cancel,
                    stamp,
                    _current_options(),
                    group_workers,
                    overall_total=total,
                    offset=offset,
                )
            )
            items.extend(group_items)
            for key, value in group_counters.items():
                counters[key] = counters.get(key, 0) + value
            generated_passwords.update(group_passwords)
            offset += len(group)
        return _finish_report(
            config, plan, dry_run, stamp, total, counters, items,
            generated_passwords,
        )

    items: List[Dict[str, Any]] = []
    generated_passwords: Dict[str, str] = {}
    counters = {"ok": 0, "failed": 0, "skipped": 0, "planned": 0}
    stopped = False

    for index, item in enumerate(actions, start=1):
        if cancel is not None and cancel.is_set():
            items.append(
                {
                    "id": item.get("id"),
                    "kind": item.get("kind"),
                    "target": item.get("target"),
                    "status": "skipped",
                    "error": "Annulé par l'utilisateur",
                }
            )
            counters["skipped"] += 1
            continue
        if stopped:
            items.append(
                {
                    "id": item.get("id"),
                    "kind": item.get("kind"),
                    "target": item.get("target"),
                    "status": "skipped",
                    "error": "Arrêt après erreur",
                }
            )
            counters["skipped"] += 1
            continue

        kind = item.get("kind", "")
        target = item.get("target", "")
        if progress:
            progress(index - 1, total, "%s : %s" % (kind, target))

        handler = HANDLERS.get(kind)
        record: Dict[str, Any] = {
            "id": item.get("id"),
            "kind": kind,
            "type": action_type(kind),
            "target": target,
            "description": item.get("description", ""),
            "started_at": now_iso(),
        }
        started = time.time()

        if handler is None:
            record.update(
                {
                    "status": "skipped",
                    "error": "Type d'action inconnu : %s" % kind,
                }
            )
            counters["skipped"] += 1
            items.append(record)
            continue

        if dry_run:
            record.update({"status": "planned", "message": "Simulation"})
            counters["planned"] += 1
            items.append(record)
            continue

        try:
            _RUN_OPTIONS.last_random = None
            backup_kind = BACKUP_KINDS.get(kind)
            if backup_kind:
                previous = _backup_object(session, backup_kind, target)
                record["backup"] = _backup(
                    config, stamp, backup_kind, target, previous
                )
            message = handler(session, config, item.get("payload") or {})
            password = getattr(_RUN_OPTIONS, "last_random", None)
            if password and kind in {"create_account", "create_resource"}:
                generated_passwords[str(target)] = str(password)
            record.update({"status": "ok", "message": message})
            counters["ok"] += 1
        except Exception as exc:  # noqa: BLE001 - reported per action
            if _is_already_exists(exc):
                record.update(
                    {"status": "skipped", "message": "Déjà présent sur la destination"}
                )
                counters["skipped"] += 1
            else:
                record.update({"status": "failed", "error": describe_error(exc)})
                counters["failed"] += 1
                if stop_on_error:
                    stopped = True
        finally:
            record["seconds"] = round(time.time() - started, 2)
            items.append(record)

    if progress:
        progress(total, total, "Termin?")

    return _finish_report(
        config, plan, dry_run, stamp, total, counters, items,
        generated_passwords,
    )
