"""Command line interface for Soap a l'un."""

import argparse
import csv
import datetime
import json
import logging
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import __version__
from . import config as config_module
from . import diff as diff_module
from . import inventory as inventory_module
from . import jobs as jobs_module
from . import mailbox as mailbox_module
from . import settings as settings_module
from .apply import apply_plan
from .connections import ZimbraSession, describe_error

_QUIET = False
_JOB_STOP_EVENT = None


def _out(*args, **kwargs):
    if kwargs.get("file") is sys.stderr or not _QUIET:
        print(*args, **kwargs)


CONTENT_ALIASES = {
    "mail": "mail",
    "calendar": "calendar",
    "task": "tasks",
    "tasks": "tasks",
    "contacts": "contacts",
    "briefcase": "briefcase",
}

STEP_ALIASES = {
    "settings": "settings",
    "tag": "tags",
    "tags": "tags",
    "cos": "cos",
    "filter": "filters",
    "filters": "filters",
    "banlist": "lists",
    "blacklist": "lists",
    "whitelist": "lists",
    "whitelsit": "lists",
    "share": "shares",
    "shares": "shares",
    "sendas": "permissions",
    "sendonbehalfof": "permissions",
}

IMPORT_STEP_ORDER = (
    "mailbox",
    "settings",
    "tags",
    "cos",
    "filters",
    "lists",
    "shares",
    "permissions",
)


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on", "oui"}:
        return True
    if text in {"0", "false", "no", "off", "non"}:
        return False
    raise argparse.ArgumentTypeError("valeur booleenne attendue (true/false)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="soapalin",
        description="Migration Zimbra - interface CLI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exemples:\n"
            "  soapalin --server-action create --server-type source "
            "--server-label src1\n"
            "  soapalin --server-action test --server-label src1\n"
            "  soapalin --server-action inventory --server-label src1\n"
            "  soapalin --create-domain domains.csv --server-source src1 "
            "--server-destination dst1\n"
            "  soapalin --create-cos cos.csv --server-source src1 "
            "--server-destination dst1\n"
            "  soapalin --import-cos all --server-source src1 "
            "--server-destination dst1\n"
            "  soapalin --create-dl lists.csv --server-source src1 "
            "--server-destination dst1\n"
            "  soapalin --create-resources resources.csv --server-source src1 "
            "--server-destination dst1\n"
            "  soapalin --import-resources resources.csv --server-source src1 "
            "--server-destination dst1\n"
            "  soapalin --import-dl lists.csv --server-source src1 "
            "--server-destination dst1\n"
            "  soapalin --create-user --password Secret! --server-source src1 "
            "--server-destination dst1 --list-user users.csv\n"
            "  soapalin --create-user --random-password --server-source src1 "
            "--server-destination dst1 --list-user users.csv\n"
            "  soapalin --import-user mail calendar settings --since 2026-11-01 "
            "--server-source src1 --server-destination dst1 "
            "--list-user users.csv\n"
            "  soapalin --import-user mail --retry-failed --server-source src1 "
            "--server-destination dst1\n"
            "  soapalin --diff-user --server-source src1 "
            "--server-destination dst1 --list-user users.csv\n"
            "  soapalin --diff-user --force-sync-tags --server-source src1 "
            "--server-destination dst1 --list-user users.csv\n"            "  soapalin --jobs all --jobs-action list\n"
            "  soapalin --jobs all --jobs-action status\n"
            "  soapalin --jobs all --jobs-action live\n"
            "  soapalin --jobs all --jobs-action stop\n"

        ),
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--jobs",
        default="",
        metavar="ID|all",
        help="selectionner un job ou tous les jobs",
    )
    parser.add_argument(
        "--jobs-action",
        choices=("list", "status", "stop", "live"),
        help="action sur les jobs en arriere-plan",
    )
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="executer la commande au premier plan",
    )
    parser.add_argument(
        "--job-worker",
        default="",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--server-action",
        choices=("create", "test", "remove", "list", "show", "rename", "inventory"),
        help="gerer ou utiliser un serveur configure",
    )
    parser.add_argument(
        "--server-type",
        choices=("source", "destination"),
        help="type de serveur pour --server-action create",
    )
    parser.add_argument(
        "--server-label",
        "--label",
        dest="server_label",
        default="",
        help="label du serveur",
    )
    parser.add_argument("--new-label", default="", help="nouveau label pour rename")
    parser.add_argument(
        "--server-source", default="", help="label du serveur source"
    )
    parser.add_argument(
        "--server-destination", default="", help="label du serveur destination"
    )
    parser.add_argument(
        "--create-user",
        action="store_true",
        help="executer le provisioning des comptes",
    )
    parser.add_argument(
        "--create-domain",
        metavar="CSV",
        default="",
        help="creer/mettre a jour les domaines d'un CSV, ou all",
    )
    parser.add_argument(
        "--create-cos",
        "--import-cos",
        dest="create_cos",
        metavar="CSV",
        default="",
        help="creer/mettre a jour les COS d'un CSV, ou all",
    )
    parser.add_argument(
        "--create-dl",
        "--import-dl",
        "--import-distribution-lists",
        dest="create_dl",
        metavar="CSV",
        default="",
        help="creer/mettre a jour les listes de distribution d'un CSV, ou all",
    )
    parser.add_argument(
        "--create-resources",
        "--create-resource",
        dest="create_resources",
        metavar="CSV",
        default="",
        help="creer/mettre a jour les ressources d'un CSV, ou all",
    )
    parser.add_argument(
        "--import-resources",
        "--import-resource",
        dest="import_resources",
        metavar="CSV",
        default="",
        help="creer les ressources d'un CSV ou all, puis importer agendas et mails",
    )
    parser.add_argument(
        "--import-user",
        dest="imports",
        action="append",
        nargs="+",
        metavar="ELEMENT",
        help=(
            "importer des elements : mail, calendar, tasks, contacts, "
            "settings, tags, cos, filter, banlist, whitelist, share, sendas, "
            "sendonbehalfof, all"
        ),
    )
    parser.add_argument(
        "--diff-user",
        action="store_true",
        help="importer uniquement les differences",
    )
    parser.add_argument(
        "--list-user",
        default="",
        help="CSV des comptes a traiter (obligatoire pour les migrations)",
    )
    parser.add_argument("--mapping-domain", default="", help="CSV source,destination")
    parser.add_argument("--mapping-host", default="", help="CSV source,destination")
    parser.add_argument(
        "--adminrights",
        action="store_true",
        help="conserver les permissions admin (avec --create-user)",
    )
    parser.add_argument(
        "--password",
        default="",
        help="mot de passe fixe pour les comptes crees",
    )
    parser.add_argument(
        "--random-password",
        action="store_true",
        help="generer un mot de passe aleatoire par compte et exporter un CSV",
    )
    parser.add_argument(
        "--keep-tgz",
        nargs="?",
        const=True,
        default=False,
        type=_parse_bool,
        help="garder les archives TGZ (true/false)",
    )
    parser.add_argument(
        "--keep-tgz-days",
        type=int,
        default=0,
        help="jours de conservation des archives (0 = infini)",
    )
    parser.add_argument(
        "--parallelism",
        type=int,
        default=1,
        help="nombre de comptes traites en parallele",
    )
    parser.add_argument(
        "--since",
        default="",
        metavar="YYYY-MM-DD",
        help="ne copier que les elements arrives depuis cette date (mail)",
    )
    parser.add_argument(
        "--retry-failed",
        nargs="?",
        const="",
        default=None,
        metavar="RAPPORT",
        help="relancer les comptes en echec du dernier rapport ou du rapport indique",
    )
    parser.add_argument(
        "--force-sync-tags",
        action="store_true",
        help="avec --diff-user, reappliquer les tags manquants aux elements existants",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="simuler sans ecrire sur la destination",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="reduire la sortie standard (les erreurs restent affichees)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="activer les traces SOAP/REST detaillees (repetable)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="activer le logging de debogage",
    )
    return parser


def _read_rows(path: str) -> List[List[str]]:
    if not path:
        return []
    if path == "-":
        return list(csv.reader(sys.stdin))
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.reader(handle))


def read_users(path: str) -> List[str]:
    users: List[str] = []
    seen = set()
    for line_no, row in enumerate(_read_rows(path), 1):
        if not row:
            continue
        raw = str(row[0] or "")
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        if value.lower() in {"email", "account", "mail", "user", "compte"}:
            continue
        if raw != value:
            _out(
                "AVERTISSEMENT ligne %d : espaces ignores autour de %r."
                % (line_no, value),
                file=sys.stderr,
            )
        if "@" not in value:
            _out(
                "AVERTISSEMENT ligne %d ignoree : adresse invalide %r."
                % (line_no, value),
                file=sys.stderr,
            )
            continue
        if any(str(column).strip() for column in row[1:]):
            _out(
                "AVERTISSEMENT ligne %d : colonnes supplementaires ignorees."
                % line_no,
                file=sys.stderr,
            )
        normalized = value.lower()
        if normalized not in seen:
            seen.add(normalized)
            users.append(value)
        else:
            _out(
                "AVERTISSEMENT ligne %d : doublon ignore %s."
                % (line_no, value),
                file=sys.stderr,
            )
    return users


def read_mapping(path: str) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for row in _read_rows(path):
        if not row:
            continue
        if len(row) == 1 and "=" in row[0]:
            left, right = row[0].split("=", 1)
        elif len(row) >= 2:
            left, right = row[0], row[1]
        else:
            continue
        source = str(left or "").strip()
        destination = str(right or "").strip()
        if not source or not destination:
            continue
        if source.lower() in {"source", "from", "origine"}:
            continue
        mapping[source.lower()] = destination
    return mapping


OBJECT_HEADERS = {
    "name",
    "nom",
    "domain",
    "domaine",
    "cos",
    "list",
    "liste",
    "dl",
    "resource",
    "ressource",
    "room",
    "salle",
    "equipment",
    "equipement",
}


def read_object_names(path: str) -> List[str]:
    names: List[str] = []
    seen = set()
    for line_no, row in enumerate(_read_rows(path), 1):
        if not row:
            continue
        value = str(row[0] or "").strip()
        if not value or value.startswith("#"):
            continue
        if value.lower() in OBJECT_HEADERS:
            continue
        if value.lower() in seen:
            _out(
                "AVERTISSEMENT ligne %d : doublon ignore %s."
                % (line_no, value),
                file=sys.stderr,
            )
            continue
        seen.add(value.lower())
        names.append(value)
    return names


def read_object_selection(value: str):
    """Return None for the special 'all' selector, otherwise a name list."""
    text = str(value or "").strip()
    if not text:
        return []
    if text.lower() == "all":
        return None
    return read_object_names(text)


def normalize_imports(values: Optional[List[List[str]]]) -> Tuple[List[str], List[str]]:
    contents: List[str] = []
    steps: List[str] = []
    for group in values or []:
        for raw in group:
            token = str(raw or "").strip().lower()
            if not token:
                continue
            if token == "all":
                for content in ("mail", "calendar", "tasks", "contacts", "briefcase"):
                    if content not in contents:
                        contents.append(content)
                for step in (
                    "settings",
                    "tags",
                    "cos",
                    "filters",
                    "lists",
                    "shares",
                    "permissions",
                ):
                    if step not in steps:
                        steps.append(step)
                continue
            if token in CONTENT_ALIASES:
                content = CONTENT_ALIASES[token]
                if content not in contents:
                    contents.append(content)
                continue
            if token in STEP_ALIASES:
                step = STEP_ALIASES[token]
                if step not in steps:
                    steps.append(step)
                continue
            if token == "adminrights":
                raise ValueError(
                    "adminrights appartient a --create-user, pas a --import-user"
                )
            raise ValueError("valeur --import-user inconnue : %s" % raw)
    steps.sort(key=IMPORT_STEP_ORDER.index)
    return contents, steps


def _base_options(config: config_module.Config, args) -> Dict[str, Any]:
    overrides: Dict[str, Any] = {
        "parallelism": max(1, int(args.parallelism or 1)),
        "mailbox_keep_archives": bool(args.keep_tgz),
        "mailbox_keep_tgz_days": max(0, int(args.keep_tgz_days or 0)),
        "diff_keep_archives": bool(args.keep_tgz),
        "diff_keep_tgz_days": max(0, int(args.keep_tgz_days or 0)),
    }
    if args.since:
        overrides["since"] = args.since
    if args.mapping_domain:
        overrides["domain_map"] = read_mapping(args.mapping_domain)
    if args.mapping_host:
        overrides["mail_host_map"] = read_mapping(args.mapping_host)
    if args.create_user or args.create_resources or args.import_resources:
        if args.password:
            overrides["fixed_password"] = args.password
    if args.create_user:
        if args.adminrights:
            overrides["keep_admin_permissions"] = True
        if args.random_password:
            overrides["random_password"] = True
            overrides["fixed_password"] = ""
        if args.mapping_domain:
            overrides["domain_map"] = read_mapping(args.mapping_domain)
        if args.mapping_host:
            overrides["mail_host_map"] = read_mapping(args.mapping_host)
    return diff_module.normalize_options(config, overrides)


def _load_snapshots(
    config: config_module.Config, source_label: str, destination_label: str
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    source = inventory_module.latest_snapshot(
        config, "source", profile=source_label
    )
    destination = inventory_module.latest_snapshot(
        config, "destination", profile=destination_label
    )
    if not source or not destination:
        commands = []
        if not source:
            commands.append(
                "  soapalin --server-action inventory --server-label %s"
                % source_label
            )
        if not destination:
            commands.append(
                "  soapalin --server-action inventory --server-label %s"
                % destination_label
            )
        raise RuntimeError(
            "Inventaires requis absents avant le calcul de la migration. "
            "Lancez d'abord :\n%s" % "\n".join(commands)
        )
    return source, destination


def _build_plan(
    config,
    users,
    options,
    source_label,
    destination_label,
    category="account",
    selection=None,
):
    source, destination = _load_snapshots(
        config, source_label, destination_label
    )
    source_names = {
        str(account.get("name") or "").lower()
        for account in source.get("accounts", [])
        if account.get("name")
    }
    missing = [
        user
        for user in users
        if str(user).lower() not in source_names
    ]
    if missing:
        _out(
            "AVERTISSEMENT : %d compte(s) de --list-user sont introuvables "
            "sur la source : %s"
            % (len(missing), ", ".join(missing)),
            file=sys.stderr,
        )
    plan = diff_module.generate_plan(
        config,
        source,
        destination,
        options=options,
        selection=selection if selection is not None else {"accounts": users},
        category=category,
    )
    validation = plan.get("validation") or {}
    if not validation.get("ok", True):
        for error in validation.get("errors") or []:
            _out("ERREUR : %s" % error, file=sys.stderr)
        raise RuntimeError("Selection invalide.")
    return plan


def _progress(done: int, total: int, message: str, detail=None) -> None:
    if message:
        _out("[%s/%s] %s" % (done, total, message), flush=True)


def _print_summary(title: str, report: Dict[str, Any]) -> int:
    summary = report.get("summary") or {}
    if summary.get("planned"):
        _out(
            "%s : %s simule(s), %s echec(s), %s ignore(s)."
            % (title, summary.get("planned", 0), summary.get("failed", 0), summary.get("skipped", 0))
        )
        return 1 if summary.get("failed") else 0
    _out(
        "%s : %s OK, %s partiel(s), %s echec(s), %s ignore(s)."
        % (
            title,
            summary.get("ok", 0),
            summary.get("partial", 0),
            summary.get("failed", 0),
            summary.get("skipped", 0),
        )
    )
    report_path = report.get("report_path")
    if report_path:
        _out("Rapport : %s" % report_path)
    return 1 if summary.get("failed") else 0


def _validate_target_domains(config, users, options, destination_label: str) -> None:
    snapshot = inventory_module.latest_snapshot(
        config, "destination", profile=destination_label
    )
    if not snapshot:
        raise RuntimeError(
            "Inventaire destination introuvable : lancez --server-action inventory."
        )
    available = {
        str(domain.get("name") or "").lower()
        for domain in snapshot.get("domains", [])
        if domain.get("name")
    }
    domain_map = {
        str(key).lower(): str(value).lower()
        for key, value in (options.get("domain_map") or {}).items()
    }
    wanted = set()
    for email in users:
        if "@" not in str(email):
            continue
        domain = str(email).rsplit("@", 1)[1].lower()
        wanted.add(domain_map.get(domain, domain))
    missing = sorted(wanted - available)
    if not missing:
        return
    raise RuntimeError(
        "Domaines absents de l'inventaire destination : %s. "
        "Creez-les avec --create-domain ou relancez l'inventaire destination."
        % ", ".join(missing)
    )


def _write_passwords(config, report: Dict[str, Any]) -> None:
    passwords = report.pop("_generated_passwords", {}) or {}
    if not passwords:
        return
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = config.storage_path("report_path") / ("passwords-%s.csv" % stamp)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["account", "password"])
        for account in sorted(passwords):
            writer.writerow([account, passwords[account]])
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass
    _out("Mots de passe generes : %s" % path)


def _run_create(
    config,
    plan,
    options,
    users,
    dry_run: bool,
    destination_label: str = "",
    cancel=None,
) -> int:
    if destination_label:
        if dry_run:
            try:
                _validate_target_domains(
                    config, users, options, destination_label
                )
            except RuntimeError as exc:
                _out("AVERTISSEMENT : %s" % exc, file=sys.stderr)
        else:
            _validate_target_domains(config, users, options, destination_label)
    report = apply_plan(
        config,
        plan,
        progress=_progress,
        dry_run=dry_run,
        kinds=diff_module.PROVISIONING_KINDS,
        accounts=users,
        options=options,
        cancel=cancel,
    )
    if not dry_run:
        _write_passwords(config, report)
    return _print_summary("Provisioning", report)


def _run_import(
    config,
    plan,
    options,
    users,
    contents: List[str],
    steps: List[str],
    args,
    cancel=None,
) -> int:
    failures = 0
    tag_contents = [
        content
        for content in contents
        if content in mailbox_module.TAG_CONTENT_SPECS
    ]
    tags_ready = False
    if "tags" in steps or tag_contents:
        report = settings_module.copy_account_settings(
            config,
            plan,
            progress=_progress,
            only=users,
            dry_run=args.dry_run,
            options=options,
            cancel=cancel,
            parts=["tags"],
        )
        failures += _print_summary("Tags", report)
        tags_ready = True
    if "cos" in steps:
        report = settings_module.copy_account_settings(
            config,
            plan,
            progress=_progress,
            only=users,
            dry_run=args.dry_run,
            options=options,
            cancel=cancel,
            parts=["cos"],
        )
        failures += _print_summary("COS", report)
    if contents:
        if args.dry_run:
            _out(
                "Simulation import contenus : %d compte(s), %s"
                % (len(users), ", ".join(contents))
            )
        else:
            step_options = dict(
                options,
                content_types=contents,
                keep_tgz_days=(
                    max(0, int(args.keep_tgz_days or 0))
                    if args.keep_tgz
                    else 0
                ),
            )
            report = mailbox_module.copy_mailboxes_chunked(
                config,
                plan,
                progress=_progress,
                only=users,
                keep_archives=bool(args.keep_tgz),
                options=step_options,
                content_types=contents,
                cancel=cancel,
            )
            failures += _print_summary("Import contenus", report)
    if tag_contents and tags_ready:
        if args.dry_run:
            _out(
                "Simulation application des tags : %d compte(s), contenus=%s."
                % (len(users), ", ".join(tag_contents))
            )
        else:
            report = mailbox_module.sync_tags(
                config,
                plan,
                only=users,
                contents=tag_contents,
                dry_run=False,
                progress=_progress,
                cancel=cancel,
            )
            summary = report.get("summary") or {}
            _out(
                "Tags appliques : %s element(s), %s tag(s), %s echec(s)."
                % (
                    summary.get("updated", 0),
                    summary.get("tags", 0),
                    summary.get("failed", 0),
                )
            )
            for error in (report.get("errors") or [])[:20]:
                _out("ERREUR TAG : %s" % error, file=sys.stderr)
            failures += 1 if summary.get("failed") else 0
    if "settings" in steps:
        report = settings_module.copy_account_settings(
            config,
            plan,
            progress=_progress,
            only=users,
            dry_run=args.dry_run,
            options=options,
            cancel=cancel,
            parts=["settings", "prefs", "folders", "attributes"],
        )
        failures += _print_summary("Reglages", report)
    if "filters" in steps:
        parts = []
        if "filters" in steps:
            parts.append("filters")
        if "lists" in steps:
            parts.append("lists")
        report = settings_module.copy_account_settings(
            config,
            plan,
            progress=_progress,
            only=users,
            dry_run=args.dry_run,
            options=options,
            cancel=cancel,
            parts=parts,
        )
        failures += _print_summary("Filtres & listes", report)
    if "shares" in steps:
        report = settings_module.copy_account_settings(
            config,
            plan,
            progress=_progress,
            only=users,
            dry_run=args.dry_run,
            options=options,
            cancel=cancel,
            parts=["shares"],
        )
        failures += _print_summary("Partages", report)
        ignored = int(report.get("ignored_shares") or 0)
        if ignored:
            _out(
                "AVERTISSEMENT : %d partage(s) ignore(s) car le compte cible "
                "n'existe pas encore. Relancez 'soapalin --import-user share' "
                "une fois tous les comptes crees." % ignored,
                file=sys.stderr,
            )
    if "permissions" in steps:
        report = settings_module.copy_account_settings(
            config,
            plan,
            progress=_progress,
            only=users,
            dry_run=args.dry_run,
            options=options,
            cancel=cancel,
            parts=["permissions"],
        )
        failures += _print_summary("Droits", report)
    return 1 if failures else 0


def _run_objects(config, args, options, cancel=None) -> int:
    source, destination = _load_snapshots(
        config, args.server_source, args.server_destination
    )
    jobs = []
    domains = (
        read_object_selection(args.create_domain) if args.create_domain else []
    )
    cos = read_object_selection(args.create_cos) if args.create_cos else []
    if args.create_domain or args.create_cos:
        if domains == [] and cos == []:
            raise RuntimeError("Aucun domaine ni COS dans le fichier fourni.")
        jobs.append(
            (
                "configuration",
                {"domains": domains, "cos": cos},
            )
        )
    if args.create_dl:
        names = read_object_selection(args.create_dl)
        if names == []:
            raise RuntimeError("Aucune liste de distribution dans le fichier fourni.")
        jobs.append(("distribution_lists", {"distribution_lists": names}))
    resource_names = []
    if args.create_resources or getattr(args, "import_resources", ""):
        resource_path = args.create_resources or getattr(args, "import_resources", "")
        resource_names = read_object_selection(resource_path)
        if resource_names == []:
            raise RuntimeError("Aucune ressource dans le fichier fourni.")
        jobs.append(("resources", {"resources": resource_names}))

    failures = 0
    for category, selection in jobs:
        plan = diff_module.generate_plan(
            config,
            source,
            destination,
            options=options,
            selection=selection,
            category=category,
        )
        validation = plan.get("validation") or {}
        if not validation.get("ok", True):
            for error in validation.get("errors") or []:
                _out("ERREUR : %s" % error, file=sys.stderr)
            raise RuntimeError("Selection %s invalide." % category)
        report = apply_plan(
            config,
            plan,
            progress=_progress,
            dry_run=args.dry_run,
            kinds=diff_module.PROVISIONING_KINDS,
            options=options,
            cancel=cancel,
        )
        failures += _print_summary(category, report)

    if getattr(args, "import_resources", ""):
        resources = {
            str(item.get("name") or "").lower(): item
            for item in source.get("resources", [])
            if item.get("name")
        }
        if resource_names is None:
            resource_names = [
                str(item.get("name"))
                for item in source.get("resources", [])
                if item.get("name")
            ]
        mailboxes = []
        for name in resource_names:
            item = resources.get(str(name).lower())
            if not item:
                continue
            target = config.map_email(str(item.get("name") or ""))
            mailboxes.append(
                {
                    "source": item.get("name"),
                    "destination": target,
                    "size": None,
                    "messages": None,
                    "destination_status": "existing",
                    "note": "Ressource calendrier",
                }
            )
        if mailboxes and args.dry_run:
            _out(
                "Simulation import contenus ressources : %d ressource(s), mail + calendar."
                % len(mailboxes)
            )
        elif mailboxes:
            content_options = dict(
                options,
                content_types=["mail", "calendar"],
                keep_tgz_days=(
                    max(0, int(args.keep_tgz_days or 0))
                    if args.keep_tgz
                    else 0
                ),
            )
            report = mailbox_module.copy_mailboxes_chunked(
                config,
                {"mailboxes": mailboxes, "options": content_options},
                progress=_progress,
                only=resource_names,
                keep_archives=bool(args.keep_tgz),
                options=content_options,
                content_types=["mail", "calendar"],
                cancel=cancel,
            )
            failures += _print_summary("Ressources - contenus", report)
        else:
            _out(
                "AVERTISSEMENT : aucune ressource correspondante dans l'inventaire source.",
                file=sys.stderr,
            )
    return failures


def _retry_users(config, report_path: str = "") -> List[str]:
    directory = config.storage_path("report_path")
    if report_path:
        candidate = Path(report_path)
        if not candidate.is_absolute() and not candidate.exists():
            candidate = config.storage_path("report_path") / report_path
        if not candidate.exists():
            raise RuntimeError("Rapport introuvable : %s" % candidate)
        candidates = [candidate]
    else:
        candidates = sorted(
            directory.glob("mailboxes-*.json"),
            key=lambda item: item.name,
            reverse=True,
        )
    for path in candidates:
        try:
            with path.open("r", encoding="utf-8") as handle:
                report = json.load(handle)
        except (OSError, ValueError):
            continue
        failed = []
        for item in report.get("items") or []:
            if item.get("status") != "failed":
                continue
            account = item.get("source") or item.get("destination")
            if account:
                failed.append(str(account))
        if failed:
            _out("Reprise du rapport : %s" % path)
            return sorted(set(failed))
    raise RuntimeError(
        "Aucun compte en echec trouve dans les rapports mailboxes-*.json."
    )


def _run_diff(
    config,
    plan,
    options,
    users,
    args,
    cancel=None,
    force_sync_tags: bool = False,
) -> int:
    content_types = options.get("content_types") or [
        "mail",
        "calendar",
        "contacts",
        "tasks",
    ]
    if args.dry_run:
        _out(
            "Simulation differences : %d compte(s), contenus=%s"
            % (len(users), ",".join(content_types))
        )
        return 0
    tag_report = settings_module.copy_account_settings(
        config,
        plan,
        progress=_progress,
        only=users,
        dry_run=False,
        options=options,
        cancel=cancel,
        parts=["tags"],
    )
    tag_summary = tag_report.get("summary") or {}
    if tag_summary.get("created") or tag_summary.get("updated"):
        _out(
            "Tags prepares : %s cree(s), %s mis a jour."
            % (tag_summary.get("created", 0), tag_summary.get("updated", 0))
        )
    report = mailbox_module.copy_mailboxes_incremental(
        config,
        plan,
        progress=_progress,
        only=users,
        keep_archives=bool(args.keep_tgz),
        options=dict(
            options,
            content_types=content_types,
            diff_keep_archives=bool(args.keep_tgz),
            diff_keep_tgz_days=max(0, int(args.keep_tgz_days or 0)),
            keep_tgz_days=(
                max(0, int(args.keep_tgz_days or 0))
                if args.keep_tgz
                else 0
            ),
        ),
        content_types=content_types,
        cancel=cancel,
    )
    failures = _print_summary("Differences", report)
    missing_keys = set()
    for item in report.get("items") or []:
        for content in item.get("contents") or []:
            for key in content.get("missing_keys") or []:
                missing_keys.add(str(key))
    if force_sync_tags:
        keys = None
    else:
        keys = missing_keys
    if keys is None or keys:
        sync_report = mailbox_module.sync_tags(
            config,
            plan,
            only=users,
            contents=content_types,
            keys=keys,
            dry_run=False,
            progress=_progress,
            cancel=cancel,
        )
        sync_summary = sync_report.get("summary") or {}
        _out(
            "Tags appliques : %s element(s), %s tag(s), %s echec(s)."
            % (
                sync_summary.get("updated", 0),
                sync_summary.get("tags", 0),
                sync_summary.get("failed", 0),
            )
        )
        for error in (sync_report.get("errors") or [])[:20]:
            _out("ERREUR TAG : %s" % error, file=sys.stderr)
        failures += 1 if sync_summary.get("failed") else 0
    return failures

def _server_public(server: Dict[str, Any]) -> Dict[str, Any]:
    connection = dict(server.get("connection") or {})
    if connection.get("admin_password"):
        connection["admin_password"] = "********"
    return {
        "label": server.get("label"),
        "type": server.get("type"),
        "path": server.get("path"),
        "connection": connection,
    }


def _run_server_test(config, label: str) -> int:
    server = config.server(label)
    config.use_server(label)
    session = ZimbraSession(config, server["type"], readonly=True)
    try:
        result = session.test()
    except Exception as exc:  # noqa: BLE001
        _out("%s : ECHEC - %s" % (label, describe_error(exc)), file=sys.stderr)
        return 1
    _out(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


def _run_server_inventory(config, label: str, cancel=None) -> int:
    server = config.server(label)
    config.use_server(label)
    report = inventory_module.collect_snapshot(
        config, server["type"], progress=_progress, profile=label, cancel=cancel
    )
    _out(
        "%s (%s) : %s comptes, %s domaines, snapshot=%s"
        % (
            label,
            server["type"],
            (report.get("counts") or {}).get("accounts", 0),
            (report.get("counts") or {}).get("domains", 0),
            report.get("path", ""),
        )
    )
    return 0


def _run_server_action(config, args, cancel=None) -> int:
    action = args.server_action
    label = args.server_label
    if action == "list":
        servers = config.servers()
        if not servers:
            _out("Aucun serveur configure.")
            return 0
        for server in servers.values():
            _out(
                "%-28s %-12s %s"
                % (
                    server["label"],
                    server["type"],
                    server.get("connection", {}).get("store_host", ""),
                )
            )
        return 0
    if action == "show":
        if label:
            _out(
                json.dumps(
                    _server_public(config.server(label)),
                    indent=2,
                    ensure_ascii=False,
                )
            )
            return 0
        servers = config.servers()
        if not servers:
            _out("Aucun serveur configure.")
            return 0
        for server in servers.values():
            _out(
                json.dumps(
                    _server_public(server), indent=2, ensure_ascii=False
                )
            )
        return 0
    if action == "create":
        if not args.server_type:
            raise RuntimeError("--server-type est obligatoire pour create.")
        if not label:
            raise RuntimeError("--server-label est obligatoire pour create.")
        server = config.create_server(label, args.server_type)
        _out(
            "Serveur cree : %s (%s)\nFichier : %s"
            % (server["label"], server["type"], server["path"])
        )
        return 0
    if not label:
        raise RuntimeError("--server-label est obligatoire pour %s." % action)
    if action == "remove":
        config.remove_server(label)
        _out("Serveur supprime : %s" % label)
        return 0
    if action == "rename":
        if not args.new_label:
            raise RuntimeError("--new-label est obligatoire pour rename.")
        new_label = config.rename_server(label, args.new_label)
        _out("Serveur renomme : %s -> %s" % (label, new_label))
        return 0
    if action == "test":
        return _run_server_test(config, label)
    if action == "inventory":
        return _run_server_inventory(config, label, cancel=cancel)
    raise RuntimeError("Action serveur inconnue : %s" % action)


def _background_requested(args) -> bool:
    object_actions = bool(
        args.create_domain
        or args.create_cos
        or args.create_dl
        or args.create_resources
        or getattr(args, "import_resources", "")
    )
    return bool(
        args.create_user
        or args.__dict__.get("imports")
        or args.diff_user
        or object_actions
        or args.server_action == "inventory"
    )


def _launch_job(argv, args) -> int:
    config = config_module.Config()
    store = jobs_module.JobStore(config)
    arguments = list(argv) if argv is not None else list(sys.argv[1:])
    title = " ".join(arguments)[:180]
    spec = store.create(arguments, title=title)
    try:
        store.launch(spec)
    except Exception as exc:  # noqa: BLE001
        store.update(spec["id"], status="failed", error=describe_error(exc))
        _out("ERREUR : lancement du job impossible : %s" % describe_error(exc), file=sys.stderr)
        return 1
    _out("Job lance : %s" % spec["id"])
    _out("Statut  : soapalin --jobs %s --jobs-action status" % spec["id"])
    _out("Suivi   : soapalin --jobs %s --jobs-action live" % spec["id"])
    _out("Arret   : soapalin --jobs %s --jobs-action stop" % spec["id"])
    return 0


def _job_line(state: Dict[str, Any]) -> str:
    return "%-24s %-12s %-19s %s" % (
        state.get("id", "?"),
        state.get("status", "?"),
        state.get("created_at", ""),
        state.get("title", ""),
    )


def _run_jobs(config, selector: str, action: str) -> int:
    store = jobs_module.JobStore(config)
    try:
        jobs = store.selected(selector or "all")
    except KeyError as exc:
        _out("ERREUR : %s" % exc, file=sys.stderr)
        return 1
    if action == "list":
        if not jobs:
            _out("Aucun job.")
            return 0
        _out("%-24s %-12s %-19s %s" % ("ID", "STATUT", "CREE", "COMMANDE"))
        for state in jobs:
            _out(_job_line(state))
        return 0
    if action == "status":
        if not jobs:
            _out("Aucun job.")
            return 0
        for state in jobs:
            _out("Job     : %s" % state.get("id", ""))
            _out("Statut  : %s" % state.get("status", ""))
            _out("Commande: %s" % state.get("title", ""))
            _out("PID     : %s" % state.get("pid", 0))
            _out("Debut   : %s" % state.get("started_at", ""))
            _out("Fin     : %s" % state.get("finished_at", ""))
            _out("Journal : %s" % state.get("log_path", ""))
            if state.get("error"):
                _out("Erreur  : %s" % state.get("error"))
            _out("")
        return 0
    if action == "stop":
        stopped = 0
        for state in jobs:
            if state.get("status") in jobs_module.TERMINAL_STATUSES:
                continue
            store.request_stop(state["id"])
            _out("Arret demande : %s" % state["id"])
            stopped += 1
        if not stopped:
            _out("Aucun job actif.")
        return 0
    if action == "live":
        return jobs_module.follow_jobs(store, jobs)
    _out("Action jobs inconnue : %s" % action, file=sys.stderr)
    return 2


def _run_job_worker(spec_path: str) -> int:
    import traceback

    global _JOB_STOP_EVENT
    try:
        with open(spec_path, "r", encoding="utf-8") as handle:
            spec = json.load(handle)
    except (OSError, ValueError) as exc:
        _out("ERREUR job : specification illisible : %s" % exc, file=sys.stderr)
        return 1
    base_dir = spec.get("base_dir") or str(Path(spec_path).resolve().parent)
    os.environ["SOAPLUN_HOME"] = str(base_dir)
    cwd = spec.get("cwd")
    if cwd:
        try:
            os.chdir(cwd)
        except OSError:
            pass
    config = config_module.Config(base_dir)
    store = jobs_module.JobStore(config)
    job_id = str(spec.get("id") or "")
    store.update(
        job_id,
        status="running",
        pid=os.getpid(),
        started_at=datetime.datetime.now().isoformat(timespec="seconds"),
    )
    cancel = threading.Event()
    _JOB_STOP_EVENT = cancel
    done = threading.Event()
    watcher = threading.Thread(
        target=jobs_module.watch_stop,
        args=(store, job_id, cancel, done),
        daemon=True,
    )
    watcher.start()
    returncode = 1
    error = ""
    try:
        returncode = main(spec.get("argv") or [], _from_job=True)
    except KeyboardInterrupt:
        returncode = 130
        cancel.set()
    except SystemExit as exc:
        returncode = int(exc.code or 1)
    except BaseException as exc:  # noqa: BLE001
        traceback.print_exc()
        error = describe_error(exc)
        returncode = 1
    finally:
        done.set()
        _JOB_STOP_EVENT = None
    stopped = store.stop_requested(job_id)
    if stopped:
        status = "stopped"
    elif returncode == 0:
        status = "succeeded"
    else:
        status = "failed"
    store.update(
        job_id,
        status=status,
        returncode=returncode,
        finished_at=datetime.datetime.now().isoformat(timespec="seconds"),
        error=error,
    )
    return returncode


def _write_interrupt_report(config, cleaned: List[str]) -> str:
    try:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        directory = config.storage_path("report_path")
        path = directory / ("interrupted-%s.json" % stamp)
        payload = {
            "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "status": "interrupted",
            "signal": "SIGINT",
            "cleaned_partials": len(cleaned),
            "cleaned_files": cleaned,
            "summary": {"ok": 0, "failed": 0, "skipped": 0, "planned": 0},
            "items": [],
        }
        fd, tmp_name = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(directory)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=1)
                handle.write("\n")
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
        return str(path)
    except Exception:  # noqa: BLE001 - interruption must not fail again
        return ""


def main(argv: Optional[Sequence[str]] = None, _from_job: bool = False) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.job_worker:
        return _run_job_worker(args.job_worker)
    if args.jobs or args.jobs_action:
        if not args.jobs_action:
            parser.error("--jobs-action est obligatoire avec --jobs.")
        try:
            return _run_jobs(config_module.Config(), args.jobs or "all", args.jobs_action)
        except Exception as exc:  # noqa: BLE001
            _out("ERREUR : %s" % describe_error(exc), file=sys.stderr)
            return 1
    global _QUIET
    _QUIET = bool(args.quiet)
    level = logging.DEBUG if (args.debug or args.verbose) else logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("soapalin").debug("argument(s): %s", vars(args))
    object_actions = bool(
        args.create_domain
        or args.create_cos
        or args.create_dl
        or args.create_resources
        or args.import_resources
    )
    migration = [
        bool(args.create_user),
        bool(args.__dict__.get("imports")),
        bool(args.diff_user),
        object_actions,
    ]
    if args.server_action:
        if any(migration):
            parser.error("--server-action est exclusif avec les migrations.")
        if (
            args.server_action == "inventory"
            and not _from_job
            and not args.foreground
        ):
            return _launch_job(argv, args)
        try:
            config = config_module.Config()
            return _run_server_action(
                config, args, cancel=_JOB_STOP_EVENT or threading.Event()
            )
        except KeyboardInterrupt:
            _out("Operation interrompue.", file=sys.stderr)
            return 130
        except Exception as exc:  # noqa: BLE001
            _out("ERREUR : %s" % describe_error(exc), file=sys.stderr)
            return 1
    if not any(migration):
        parser.print_help()
        return 2
    if args.diff_user and args.__dict__.get("imports"):
        parser.error("--diff-user ne peut pas etre combine avec --import-user.")
    if args.diff_user and args.create_user:
        parser.error("--diff-user ne peut pas etre combine avec --create-user.")
    account_actions = bool(
        args.create_user or args.__dict__.get("imports") or args.diff_user
    )
    if account_actions and not args.list_user and args.retry_failed is None:
        parser.error("--list-user est obligatoire pour les migrations.")
    if not args.server_source or not args.server_destination:
        parser.error(
            "--server-source et --server-destination sont obligatoires."
        )
    if (args.mapping_domain or args.mapping_host) and not any(
        (
            args.create_user,
            args.create_domain,
            args.create_cos,
            args.create_dl,
            args.create_resources,
            args.import_resources,
        )
    ):
        parser.error(
            "les mappings necessitent --create-user ou une option --create-*/--import-*."
        )
    if args.adminrights and not args.create_user:
        parser.error("--adminrights est uniquement utilisable avec --create-user.")
    if args.random_password and not args.create_user:
        parser.error("--random-password est uniquement utilisable avec --create-user.")
    if args.create_user and not (args.password or args.random_password):
        parser.error(
            "--password ou --random-password est obligatoire avec --create-user."
        )
    if args.password and args.random_password:
        parser.error("--password et --random-password sont exclusifs.")
    if args.force_sync_tags and not args.diff_user:
        parser.error("--force-sync-tags est uniquement utilisable avec --diff-user.")
    if args.since and not (args.__dict__.get("imports") or args.diff_user):
        parser.error("--since est uniquement utilisable avec --import-user ou --diff-user.")
    if args.since:
        try:
            mailbox_module.since_query(args.since)
        except Exception as exc:  # noqa: BLE001
            parser.error(str(exc))
    if args.retry_failed is not None and not (
        args.__dict__.get("imports") or args.diff_user
    ):
        parser.error("--retry-failed est uniquement utilisable avec --import-user ou --diff-user.")
    if args.retry_failed is not None and args.list_user:
        parser.error("--retry-failed ne peut pas etre combine avec --list-user.")
    try:
        contents, steps = normalize_imports(args.__dict__.get("imports"))
    except ValueError as exc:
        parser.error(str(exc))

    cancel = threading.Event()
    config = None
    if not _from_job and not args.foreground and _background_requested(args):
        return _launch_job(argv, args)

    try:
        config = config_module.Config()
        config.use_servers(args.server_source, args.server_destination)
        options = _base_options(config, args)
        failures = 0
        if object_actions:
            failures += _run_objects(config, args, options, cancel=cancel)
            if failures:
                return failures
        if not account_actions:
            return 0

        users = (
            _retry_users(config, args.retry_failed or "")
            if args.retry_failed is not None
            else read_users(args.list_user)
        )
        if not users:
            raise RuntimeError("Aucun compte a traiter.")
        if args.diff_user:
            options["content_types"] = ["mail", "calendar", "contacts", "tasks"]
            plan = _build_plan(
                config,
                users,
                options,
                args.server_source,
                args.server_destination,
            )
            return _run_diff(
                config,
                plan,
                options,
                users,
                args,
                cancel=cancel,
                force_sync_tags=args.force_sync_tags,
            )
        plan = _build_plan(
            config,
            users,
            options,
            args.server_source,
            args.server_destination,
        )
        if args.create_user:
            failures += _run_create(
                config,
                plan,
                options,
                users,
                args.dry_run,
                "" if args.create_domain else args.server_destination,
                cancel=cancel,
            )
        if failures:
            return failures
        if args.__dict__.get("imports"):
            return _run_import(
                config, plan, options, users, contents, steps, args, cancel=cancel
            )
        return 0
    except KeyboardInterrupt:
        cancel.set()
        cleaned: List[str] = []
        if config is not None:
            try:
                cleaned = mailbox_module.cleanup_partial_archives(config)
            except Exception:  # noqa: BLE001 - cleanup is best effort
                cleaned = []
            report_path = _write_interrupt_report(config, cleaned)
            if report_path:
                _out("Rapport d'interruption : %s" % report_path)
        _out(
            "INTERRUPTION : transfert arrete, %d archive(s) partielle(s) supprimee(s)."
            % len(cleaned),
            file=sys.stderr,
        )
        return 130
    except Exception as exc:  # noqa: BLE001
        _out("ERREUR : %s" % describe_error(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
