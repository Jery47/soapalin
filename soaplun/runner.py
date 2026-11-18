"""Orchestration helpers used by the CLI adapter."""

import csv
import datetime
import io
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import cli
from . import config as config_module
from .storage import write_text_atomic


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
        overrides["domain_map"] = cli.read_mapping(args.mapping_domain)
    if args.mapping_host:
        overrides["mail_host_map"] = cli.read_mapping(args.mapping_host)
    if getattr(args, "directly_clean", False):
        overrides["directly_clean"] = True
    if getattr(args, "resume", False):
        overrides["resume"] = True
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
            overrides["domain_map"] = cli.read_mapping(args.mapping_domain)
        if args.mapping_host:
            overrides["mail_host_map"] = cli.read_mapping(args.mapping_host)
    return cli.diff_module.normalize_options(config, overrides)


def _load_snapshots(
    config: config_module.Config, source_label: str, destination_label: str
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    source = cli.inventory_module.latest_snapshot(
        config, "source", profile=source_label
    )
    destination = cli.inventory_module.latest_snapshot(
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
    source, destination = cli._load_snapshots(
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
        cli._out(
            "AVERTISSEMENT : %d compte(s) du CSV sont introuvables "
            "sur la source : %s"
            % (len(missing), ", ".join(missing)),
            file=sys.stderr,
        )
    plan = cli.diff_module.generate_plan(
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
            cli._out("ERREUR : %s" % error, file=sys.stderr)
        raise RuntimeError("Selection invalide.")
    return plan


def _progress(done: int, total: int, message: str, detail=None) -> None:
    if message:
        with cli._CONTEXT.lock:
            cli._out("[%s/%s] %s" % (done, total, message), flush=True)


def _print_summary(title: str, report: Dict[str, Any]) -> int:
    cli._CONTEXT.reports.append({"title": title, "report": report})
    summary = report.get("summary") or {}
    if summary.get("planned"):
        cli._out(
            "%s : %s simule(s), %s echec(s), %s ignore(s)."
            % (title, summary.get("planned", 0), summary.get("failed", 0), summary.get("skipped", 0))
        )
        return 1 if summary.get("failed") else 0
    cli._out(
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
        cli._out("Rapport : %s" % report_path)
    return 1 if summary.get("failed") else 0


def _validate_target_domains(config, users, options, destination_label: str) -> None:
    snapshot = cli.inventory_module.latest_snapshot(
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
    root = config.storage_path("report_path")
    path = root / ("passwords-%s.csv" % stamp)
    index = 1
    while path.exists():
        path = root / ("passwords-%s-%d.csv" % (stamp, index))
        index += 1
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["account", "password"])
    for account in sorted(passwords):
        writer.writerow([account, passwords[account]])
    write_text_atomic(path, buffer.getvalue(), mode=0o600)
    cli._out("Mots de passe generes : %s" % path)


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
                cli._validate_target_domains(
                    config, users, options, destination_label
                )
            except RuntimeError as exc:
                cli._out("AVERTISSEMENT : %s" % exc, file=sys.stderr)
        else:
            cli._validate_target_domains(config, users, options, destination_label)
    report = cli.apply_plan(
        config,
        plan,
        progress=cli._progress,
        dry_run=dry_run,
        kinds=cli.diff_module.PROVISIONING_KINDS,
        accounts=users,
        options=options,
        parallelism=parallelism,
        cancel=cancel,
    )
    if not dry_run:
        cli._write_passwords(config, report)
    return 2 if cli._print_summary("Provisioning", report) else 0


def _copy_settings_parts(
    config,
    plan,
    users,
    options,
    args,
    cancel,
    parts: List[str],
    title: str,
):
    report = cli.settings_module.copy_account_settings(
        config,
        plan,
        progress=cli._progress,
        only=users,
        dry_run=args.dry_run,
        options=options,
        parallelism=getattr(args, "parallelism", 1),
        cancel=cancel,
        parts=parts,
    )
    return cli._print_summary(title, report), report


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
        if content in cli.mailbox_module.TAG_CONTENT_SPECS
    ]
    tags_ready = False
    if ("tags" in steps or tag_contents) and not args.dry_run:
        failures += _copy_settings_parts(
            config, plan, users, options, args, cancel, ["tags"], "Tags"
        )[0]
        tags_ready = True
    elif "tags" in steps or tag_contents:
        cli._out("Simulation des definitions de tags.")
        tags_ready = True
    if contents:
        if args.dry_run:
            cli._out(
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
            report = cli.mailbox_module.copy_mailboxes_chunked(
                config,
                plan,
                progress=cli._progress,
                only=users,
                keep_archives=bool(args.keep_tgz),
                options=step_options,
                content_types=contents,
                cancel=cancel,
            )
            failures += cli._print_summary("Import contenus", report)
    if tag_contents and tags_ready:
        if args.dry_run:
            cli._out(
                "Simulation application des tags : %d compte(s), contenus=%s."
                % (len(users), ", ".join(tag_contents))
            )
        else:
            report = cli.mailbox_module.sync_tags(
                config,
                plan,
                only=users,
                contents=tag_contents,
                dry_run=False,
                progress=cli._progress,
                cancel=cancel,
            )
            summary = report.get("summary") or {}
            cli._out(
                "Tags appliques : %s element(s), %s tag(s), %s echec(s)."
                % (
                    summary.get("updated", 0),
                    summary.get("tags", 0),
                    summary.get("failed", 0),
                )
            )
            for error in (report.get("errors") or [])[:20]:
                cli._out("ERREUR TAG : %s" % error, file=sys.stderr)
            failures += 1 if summary.get("failed") else 0
    folder_parts: List[str] = [
        value for value in ("folder", "color") if value in steps
    ]
    if folder_parts:
        failures += _copy_settings_parts(
            config,
            plan,
            users,
            options,
            args,
            cancel,
            folder_parts,
            "Dossiers",
        )[0]
    if "settings" in steps:
        failures += _copy_settings_parts(
            config,
            plan,
            users,
            options,
            args,
            cancel,
            ["settings", "prefs", "folders", "attributes"],
            "Reglages",
        )[0]
    if "filters" in steps:
        parts = ["filters"]
        if "lists" in steps:
            parts.append("lists")
        failures += _copy_settings_parts(
            config,
            plan,
            users,
            options,
            args,
            cancel,
            parts,
            "Filtres & listes",
        )[0]
    if "shares" in steps:
        failure, report = _copy_settings_parts(
            config,
            plan,
            users,
            options,
            args,
            cancel,
            ["shares"],
            "Partages",
        )
        failures += failure
        ignored = int(report.get("ignored_shares") or 0)
        if ignored:
            cli._out(
                "AVERTISSEMENT : %d partage(s) ignore(s) car le compte cible "
                "n'existe pas encore. Relancez 'soapalin migrate CSV --type account --item share' "
                "une fois tous les comptes crees." % ignored,
                file=sys.stderr,
            )
    if "permissions" in steps:
        failures += _copy_settings_parts(
            config,
            plan,
            users,
            options,
            args,
            cancel,
            ["permissions"],
            "Droits",
        )[0]
    return 2 if failures else 0


def _run_objects(config, args, options, cancel=None) -> int:
    source, destination = cli._load_snapshots(
        config, args.server_source, args.server_destination
    )
    jobs = []
    domains = (
        cli.read_object_selection(args.create_domain) if args.create_domain else []
    )
    cos = cli.read_object_selection(args.create_cos) if args.create_cos else []
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
        names = cli.read_object_selection(args.create_dl)
        if names == []:
            raise RuntimeError("Aucune liste de distribution dans le fichier fourni.")
        jobs.append(("distribution_lists", {"distribution_lists": names}))
    resource_names: List[str] = []
    if args.create_resources or getattr(args, "import_resources", ""):
        resource_path = args.create_resources or getattr(args, "import_resources", "")
        selected_names = cli.read_object_selection(resource_path)
        resource_names = selected_names or []
        if resource_names == []:
            raise RuntimeError("Aucune ressource dans le fichier fourni.")
        jobs.append(("resources", {"resources": resource_names}))

    failures = 0
    for category, selection in jobs:
        plan = cli.diff_module.generate_plan(
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
                cli._out("ERREUR : %s" % error, file=sys.stderr)
            raise RuntimeError("Selection %s invalide." % category)
        report = cli.apply_plan(
            config,
            plan,
            progress=cli._progress,
            dry_run=args.dry_run,
            kinds=cli.diff_module.PROVISIONING_KINDS,
            options=options,
            parallelism=getattr(args, "parallelism", 1),
            cancel=cancel,
        )
        failures += cli._print_summary(category, report)

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
            cli._out(
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
            report = cli.mailbox_module.copy_mailboxes_chunked(
                config,
                {"mailboxes": mailboxes, "options": content_options},
                progress=cli._progress,
                only=resource_names,
                keep_archives=bool(args.keep_tgz),
                options=content_options,
                content_types=["mail", "calendar"],
                cancel=cancel,
            )
            failures += cli._print_summary("Ressources - contenus", report)
        else:
            cli._out(
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
            cli._out("Reprise du rapport : %s" % path)
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
        cli._out(
            "Simulation differences : %d compte(s), contenus=%s"
            % (len(users), ",".join(content_types))
        )
        return 0
    tag_report = cli.settings_module.copy_account_settings(
        config,
        plan,
        progress=cli._progress,
        only=users,
        dry_run=False,
        options=options,
        parallelism=getattr(args, "parallelism", 1),
        cancel=cancel,
        parts=["tags"],
    )
    tag_summary = tag_report.get("summary") or {}
    if tag_summary.get("created") or tag_summary.get("updated"):
        cli._out(
            "Tags prepares : %s cree(s), %s mis a jour."
            % (tag_summary.get("created", 0), tag_summary.get("updated", 0))
        )
    report = cli.mailbox_module.copy_mailboxes_incremental(
        config,
        plan,
        progress=cli._progress,
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
    failures = cli._print_summary("Differences", report)
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
        sync_report = cli.mailbox_module.sync_tags(
            config,
            plan,
            only=users,
            contents=content_types,
            keys=keys,
            dry_run=False,
            progress=cli._progress,
            cancel=cancel,
        )
        sync_summary = sync_report.get("summary") or {}
        cli._out(
            "Tags appliques : %s element(s), %s tag(s), %s echec(s)."
            % (
                sync_summary.get("updated", 0),
                sync_summary.get("tags", 0),
                sync_summary.get("failed", 0),
            )
        )
        for error in (sync_report.get("errors") or [])[:20]:
            cli._out("ERREUR TAG : %s" % error, file=sys.stderr)
        failures += 1 if sync_summary.get("failed") else 0
    return failures


