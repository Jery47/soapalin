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
_LAST_REPORTS = []


def _out(*args, **kwargs):
    if kwargs.get("file") is sys.stderr or not _QUIET:
        print(*args, **kwargs)


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on", "oui"}:
        return True
    if text in {"0", "false", "no", "off", "non"}:
        return False
    raise argparse.ArgumentTypeError("valeur booleenne attendue (true/false)")


ACCOUNT_ELEMENTS = (
    "mail",
    "calendar",
    "contacts",
    "tasks",
    "briefcase",
    "cos",
    "folder",
    "color",
    "settings",
    "tags",
    "filter",
    "banlist",
    "whitelist",
    "share",
    "sendas",
    "sendonbehalfof",
)
RESOURCE_ELEMENTS = ("mail", "calendar")
DL_ELEMENTS = ("members",)
ELEMENT_ALIASES = {
    "task": "tasks",
    "filters": "filter",
    "shares": "share",
    "sendas": "sendas",
    "sendonbehalfof": "sendonbehalfof",
}


def iso_date(value: str):
    import datetime

    try:
        return datetime.datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "date invalide %r : utilisez YYYY-MM-DD" % value
        ) from exc


def _elements_for_type(kind: str):
    if kind == "account":
        return list(ACCOUNT_ELEMENTS)
    if kind == "resource":
        return list(RESOURCE_ELEMENTS)
    if kind == "dl":
        return list(DL_ELEMENTS)
    return list(ACCOUNT_ELEMENTS + RESOURCE_ELEMENTS + DL_ELEMENTS)


def normalize_migrate_elements(kind: str, values):
    values = list(values or ["all"])
    if "all" in values:
        if len(values) != 1:
            raise ValueError("'all' doit etre utilise seul.")
        return _elements_for_type(kind)
    valid = set(ACCOUNT_ELEMENTS + RESOURCE_ELEMENTS + DL_ELEMENTS)
    applicable = set(_elements_for_type(kind))
    normalized = []
    ignored = []
    for raw in values:
        value = str(raw or "").strip().lower()
        if not value:
            continue
        value = ELEMENT_ALIASES.get(value, value)
        if value not in valid:
            raise ValueError("element inconnu : %s" % raw)
        if value not in applicable:
            ignored.append(value)
            continue
        if value not in normalized:
            normalized.append(value)
    for value in ignored:
        _out(
            "AVERTISSEMENT : element %s inapplicable au type %s, ignore."
            % (value, kind),
            file=sys.stderr,
        )
    if not normalized:
        raise ValueError(
            "aucun element applicable au type %s dans la selection." % kind
        )
    return normalized


def _add_global_options(parser):
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--foreground", action="store_true")
    parser.add_argument("--job-worker", default="", help=argparse.SUPPRESS)


def _add_connection_options(parser):
    parser.add_argument("--from", dest="from_label", required=True, metavar="LABEL")
    parser.add_argument("--to", dest="to_label", required=True, metavar="LABEL")
    parser.add_argument("--mapping-domain", default="", metavar="CSV")
    parser.add_argument("--mapping-host", default="", metavar="CSV")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="soapalin",
        description="Migration Zimbra en sous-commandes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Options de migrate :\n"
            "  CSV|all ELEMENTS --type {account,resource,dl,all}\n"
            "    --from LABEL --to LABEL\n"
            "    --mapping-domain CSV --mapping-host CSV\n"
            "    --differential [YYYY-MM-DD]\n"
            "    --retry-failed [RAPPORT]\n"
            "    --parallelism N (defaut : 1)\n"
            "    --keep-tgz [true|false] --keep-tgz-days N\n"
            "    --json (uniquement avec --dry-run)\n"
            "  account  : mail calendar contacts tasks briefcase cos folder color settings tags\n"
            "             filter banlist whitelist share sendas sendonbehalfof\n"
            "  resource : mail calendar\n"
            "  dl       : members\n"
            "\n"
            "Exemples:\n"
            "  soapalin server add src1 --role source\n"
            "  soapalin server add dst1 --role destination\n"
            "  soapalin server list\n"
            "  soapalin server test src1\n"
            "  soapalin server inventory src1\n"
            "  soapalin server inventory src1 --no-stats\n"
            "  soapalin server inventory src1 --no-stats --parallelism 4\n"
            "  soapalin provision domain domains.csv --from src1 --to dst1\n"
            "  soapalin provision cos all --from src1 --to dst1\n"
            "  soapalin provision account users.csv --from src1 --to dst1 --password Secret!\n"
            "  soapalin provision account users.csv --from src1 --to dst1 --random-password --keep-admin\n"
            "  soapalin migrate users.csv mail calendar settings --type account --from src1 --to dst1\n"
            "  soapalin migrate users.csv cos --type account --from src1 --to dst1\n"
            "  soapalin migrate all --type account --from src1 --to dst1\n"
            "  soapalin migrate resources.csv mail calendar --type resource --from src1 --to dst1\n"
            "  soapalin migrate lists.csv members --type dl --from src1 --to dst1\n"
            "  soapalin migrate all --type all --from src1 --to dst1\n"
            "  soapalin migrate users.csv mail --type account --differential --from src1 --to dst1\n"
            "  soapalin migrate users.csv mail --type account --differential=2026-10-01 --from src1 --to dst1\n"
            "  soapalin migrate users.csv mail --type account --retry-failed --from src1 --to dst1\n"
            "  soapalin --dry-run migrate users.csv all --type account --json --from src1 --to dst1\n"
            "  soapalin --foreground migrate users.csv mail --type account --from src1 --to dst1\n"
            "  soapalin job list\n"
            "  soapalin job status JOB_ID\n"
            "  soapalin job live\n"
            "  soapalin job stop all\n"
        ),
    )
    _add_global_options(parser)
    sub = parser.add_subparsers(dest="command", metavar="{server,provision,migrate,job}")

    from .commands import job as job_cmd
    from .commands import migrate as migrate_cmd
    from .commands import provision as provision_cmd
    from .commands import server as server_cmd

    server_cmd.add_parser(sub)
    provision_cmd.add_parser(sub)
    migrate_cmd.add_parser(sub)
    job_cmd.add_parser(sub)
    return parser


def validate(args, parser) -> None:
    if not args.command:
        parser.error("une sous-commande est obligatoire.")
    if args.command in ("provision", "migrate"):
        if not args.from_label or not args.to_label:
            parser.error("--from et --to sont obligatoires.")
        if getattr(args, "json", False) and not args.dry_run:
            parser.error("--json est uniquement autorise avec --dry-run.")
    if args.command == "provision":
        if not getattr(args, "provision_command", ""):
            parser.error("une sous-commande provision est obligatoire.")
        if args.provision_command == "account":
            if not (
                args.password
                or args.password_file
                or args.random_password
            ):
                parser.error(
                    "--password, --password-file ou --random-password est obligatoire."
                )
    if args.command == "migrate":
        if not args.type:
            parser.error("--type est obligatoire.")
        if args.type == "all" and str(args.csv).strip().lower() != "all":
            parser.error("--type all exige le CSV 'all'.")
        try:
            args.elements = normalize_migrate_elements(args.type, args.elements)
        except ValueError as exc:
            parser.error(str(exc))
        if getattr(args, "retry_failed", None) is not None and not args.csv:
            parser.error("--retry-failed exige un CSV ou 'all'.")
    if args.command == "job":
        if not getattr(args, "job_command", ""):
            parser.error("une sous-commande job est obligatoire.")


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
                "  soapalin server inventory %s"
                % source_label
            )
        if not destination:
            commands.append(
                "  soapalin server inventory %s"
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
            "AVERTISSEMENT : %d compte(s) du CSV sont introuvables "
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
    _LAST_REPORTS.append({"title": title, "report": report})
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
            "Inventaire destination introuvable : lancez 'soapalin server inventory LABEL'."
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
        "Creez-les avec 'provision domain' ou relancez l'inventaire destination."
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
    parallelism: int = 1,
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
        parallelism=parallelism,
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
    if ("tags" in steps or tag_contents) and not args.dry_run:
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
    elif "tags" in steps or tag_contents:
        _out("Simulation des definitions de tags.")
        tags_ready = True
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
    folder_parts = [
        value for value in ("folder", "color") if value in steps
    ]
    if folder_parts:
        report = settings_module.copy_account_settings(
            config,
            plan,
            progress=_progress,
            only=users,
            dry_run=args.dry_run,
            options=options,
            cancel=cancel,
            parts=folder_parts,
        )
        failures += _print_summary("Dossiers", report)
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
                "n'existe pas encore. Relancez 'soapalin migrate CSV share --type account' "
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
    return 2 if failures else 0


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
            parallelism=getattr(args, "parallelism", 1),
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
    return 2 if failures else 0


def _retry_users(
    config, report_path: str = "", migrate_type: Optional[str] = None
) -> List[str]:
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
            list(directory.glob("mailboxes-*.json"))
            + list(directory.glob("apply-*.json")),
            key=lambda item: item.stem,
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
            item_type = str(item.get("type") or "account")
            if migrate_type and migrate_type != "all" and item_type != migrate_type:
                continue
            account = item.get("source") or item.get("destination") or item.get("target")
            if account:
                failed.append(str(account))
        if failed:
            _out("Reprise du rapport : %s" % path)
            return sorted(set(failed))
    raise RuntimeError(
        "Aucun element en echec trouve dans les rapports mailboxes-*.json "
        "ou apply-*.json."
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


def _run_server_inventory(
    config,
    label: str,
    with_stats: bool = True,
    parallelism: int = 1,
    cancel=None,
) -> int:
    server = config.server(label)
    config.use_server(label)
    report = inventory_module.collect_snapshot(
        config,
        server["type"],
        progress=_progress,
        with_stats=with_stats,
        profile=label,
        cancel=cancel,
        parallelism=parallelism,
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
    _out("Statut  : soapalin job status %s" % spec["id"])
    _out("Suivi   : soapalin job live %s" % spec["id"])
    _out("Arret   : soapalin job stop %s" % spec["id"])
    return 0


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


def _should_background(args) -> bool:
    if args.command in ("provision", "migrate"):
        return True
    if args.command == "server" and getattr(args, "server_command", "") == "inventory":
        return True
    return False


def main(argv: Optional[Sequence[str]] = None, _from_job: bool = False) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.job_worker:
        return _run_job_worker(args.job_worker)
    global _QUIET
    _QUIET = bool(args.quiet)
    level = logging.DEBUG if (args.debug or args.verbose) else logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("soapalin").debug("argument(s): %s", vars(args))
    validate(args, parser)

    from .commands import job as job_cmd
    from .commands import migrate as migrate_cmd
    from .commands import provision as provision_cmd
    from .commands import server as server_cmd

    if not _from_job and not args.foreground and _should_background(args):
        return _launch_job(argv, args)

    config = None
    try:
        if args.command == "job":
            return job_cmd.run(args)
        if args.command == "server":
            return server_cmd.run(args)
        config = config_module.Config()
        if args.command == "provision":
            return provision_cmd.run(args)
        if args.command == "migrate":
            _LAST_REPORTS.clear()
            result = migrate_cmd.run(args)
            if getattr(args, "json", False) and args.dry_run:
                _out(
                    json.dumps(
                        {"dry_run": True, "reports": _LAST_REPORTS},
                        ensure_ascii=False,
                        indent=1,
                        default=str,
                    )
                )
            return result
        parser.error("sous-commande inconnue.")
        return 2
    except KeyboardInterrupt:
        cancel = _JOB_STOP_EVENT
        if cancel is not None:
            cancel.set()
        cleaned = []
        if config is not None:
            try:
                from . import mailbox as mailbox_module

                cleaned = mailbox_module.cleanup_partial_archives(config)
            except Exception:  # noqa: BLE001
                cleaned = []
            path = _write_interrupt_report(config, cleaned)
            if path:
                _out("Rapport d'interruption : %s" % path)
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
