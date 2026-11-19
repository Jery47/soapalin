"""Orchestration shared by the ``provision`` and ``migrate`` sub-commands.

Keeping the business flow here (instead of inside the argparse modules) makes
the command layer a thin adapter and avoids importing private CLI helpers from
several places.
"""

from types import SimpleNamespace
from typing import Any, Dict, List

from . import cli


def runtime(args, **extra) -> SimpleNamespace:
    differential = getattr(args, "differential", None)
    values: Dict[str, Any] = {
        "server_source": args.from_label,
        "server_destination": args.to_label,
        "dry_run": args.dry_run,
        "keep_tgz": bool(getattr(args, "keep_tgz", False)),
        "keep_tgz_days": int(getattr(args, "keep_tgz_days", 0) or 0),
        "parallelism": getattr(args, "parallelism", 1),
        "mapping_domain": getattr(args, "mapping_domain", ""),
        "mapping_host": getattr(args, "mapping_host", ""),
        "directly_clean": bool(getattr(args, "directly_clean", False)),
        "resume": bool(getattr(args, "resume", False)),
        "since": (
            differential.isoformat()
            if differential is not None and hasattr(differential, "isoformat")
            else ""
        ),
        "create_user": False,
        "create_domain": "",
        "create_cos": "",
        "create_dl": "",
        "create_resources": "",
        "import_resources": "",
        "adminrights": False,
        "password": "",
        "random_password": False,
    }
    values.update(extra)
    return SimpleNamespace(**values)


def users(config, args, source) -> List[str]:
    if str(args.csv).strip().lower() == "all":
        selected = [
            str(item.get("name"))
            for item in source.get("accounts", [])
            if item.get("name")
        ]
    else:
        selected = cli.read_users(args.csv)
    if args.retry_failed is not None:
        retry = cli._retry_users(
            config, args.retry_failed or "", migrate_type=args.type
        )
        wanted = {str(value).lower() for value in retry}
        if str(args.csv).strip().lower() != "all":
            selected = [value for value in selected if value.lower() in wanted]
        else:
            selected = retry
    return selected


def retry_filter(config, args):
    if getattr(args, "retry_failed", None) is None:
        return None
    return {
        str(value).lower()
        for value in cli._retry_users(
            config, args.retry_failed or "", migrate_type=args.type
        )
    }


def record_simulation(kind, plan, elements) -> None:
    from .inventory import now_iso

    items = []
    for mailbox in plan.get("mailboxes", []) or []:
        contents = [
            {
                "content": element,
                "label": element,
                "items": int(mailbox.get("messages") or 0),
                "bytes": int(mailbox.get("size") or 0),
            }
            for element in elements
        ]
        items.append(
            {
                "source": mailbox.get("source", ""),
                "destination": mailbox.get("destination", ""),
                "type": kind,
                "status": "planned",
                "contents": contents,
                "messages": int(mailbox.get("messages") or 0),
                "size": int(mailbox.get("size") or 0),
            }
        )
    cli._CONTEXT.reports.append(
        {
            "title": "Simulation %s" % kind,
            "report": {
                "created_at": now_iso(),
                "dry_run": True,
                "type": kind,
                "total": len(items),
                "items": items,
                "summary": {
                    "ok": 0,
                    "failed": 0,
                    "skipped": 0,
                    "planned": len(items),
                },
            },
        }
    )


def run_cos(config, args, selected_users) -> int:
    from .apply import apply_plan

    source, destination = cli._load_snapshots(
        config, args.from_label, args.to_label
    )
    run_options = runtime(args)
    options = cli._base_options(config, run_options)
    plan = cli._build_plan(
        config, selected_users, options, args.from_label, args.to_label
    )
    destination_cos = {
        str(item.get("name") or "").lower()
        for item in destination.get("cos", [])
        if item.get("name")
    }
    destination_accounts = {
        str(item.get("name") or "").lower()
        for item in destination.get("accounts", [])
        if item.get("name")
    }
    actions = []
    missing = set()
    for item in plan.get("actions", []):
        if item.get("kind") != "modify_account":
            continue
        payload = dict(item.get("payload") or {})
        if payload.get("after_create"):
            continue
        if str(item.get("target") or "").lower() not in destination_accounts:
            continue
        cos_name = str(payload.get("cos_name") or "")
        if not cos_name:
            continue
        if cos_name.lower() not in destination_cos:
            missing.add(cos_name)
            continue
        payload["attrs"] = {}
        payload["changes"] = ["COS -> %s" % cos_name]
        actions.append(
            dict(
                item,
                payload=payload,
                description="Appliquer la COS %s a %s"
                % (cos_name, item.get("target", "")),
            )
        )
    if missing:
        raise RuntimeError(
            "COS absente(s) sur la destination : %s. Lancez d'abord "
            "'soapalin provision all --type cos'." % ", ".join(sorted(missing))
        )
    if not actions:
        raise RuntimeError("Aucun compte existant avec une COS a appliquer.")
    plan = dict(plan, actions=actions)
    report = apply_plan(
        config,
        plan,  # type: ignore[arg-type]
        progress=cli._progress,
        dry_run=args.dry_run,
        options=options,
        accounts=selected_users,
        parallelism=args.parallelism,
        cancel=cli._CONTEXT.cancel,
    )
    return cli._print_summary("COS", report)


def account_elements(elements):
    return [value for value in elements if value in cli.ACCOUNT_ELEMENTS]


def resource_elements(elements):
    return [value for value in elements if value in cli.RESOURCE_ELEMENTS]


def run_account(config, args, elements, users_fn=None) -> int:
    source, _ = cli._load_snapshots(config, args.from_label, args.to_label)
    selected_users = (users_fn or users)(config, args, source)
    if not selected_users:
        raise RuntimeError("Aucun compte a migrer.")
    selected_elements = account_elements(elements)
    contents = [
        value
        for value in selected_elements
        if value in ("mail", "calendar", "contacts", "tasks", "briefcase")
    ]
    steps = []
    for value in selected_elements:
        if value == "settings":
            steps.append("settings")
        elif value == "tags":
            steps.append("tags")
        elif value in ("folder", "color"):
            steps.append(value)
        elif value in ("filter",):
            steps.append("filters")
        elif value in ("banlist", "whitelist"):
            steps.append("lists")
        elif value == "share":
            steps.append("shares")
        elif value in ("sendas", "sendonbehalfof"):
            steps.append("permissions")
    run_options = runtime(args)
    options = cli._base_options(config, run_options)
    if contents:
        options["content_types"] = contents
    plan = cli._build_plan(
        config, selected_users, options, args.from_label, args.to_label
    )
    if args.dry_run:
        record_simulation("account", plan, contents or steps or ["cos"])
    failures = 0
    if "cos" in selected_elements:
        failures += run_cos(config, args, selected_users)
    if args.differential is not None:
        if contents:
            failures += cli._run_diff(
                config,
                plan,
                options,
                selected_users,
                run_options,
                cancel=cli._CONTEXT.cancel,
                force_sync_tags=True,
            )
        if steps and not args.dry_run:
            failures += cli._run_import(
                config,
                plan,
                options,
                selected_users,
                [],
                steps,
                run_options,
                cancel=cli._CONTEXT.cancel,
            )
        return 2 if failures else 0
    result = cli._run_import(
        config,
        plan,
        options,
        selected_users,
        contents,
        steps,
        run_options,
        cancel=cli._CONTEXT.cancel,
    )
    return 2 if failures or result else 0


def resource_mailboxes(config, args, elements):

    source, _ = cli._load_snapshots(config, args.from_label, args.to_label)
    names = (
        [
            str(item.get("name"))
            for item in source.get("resources", [])
            if item.get("name")
        ]
        if str(args.csv).strip().lower() == "all"
        else cli.read_object_names(args.csv)
    )
    retry = retry_filter(config, args)
    if retry is not None:
        names = [name for name in names if str(name).lower() in retry]
    selected = {str(name).lower() for name in names}
    mailboxes = []
    for item in source.get("resources", []):
        name = str(item.get("name") or "")
        if not name or name.lower() not in selected:
            continue
        mailboxes.append(
            {
                "source": name,
                "destination": config.map_email(name),
                "type": "resource",
                "size": None,
                "messages": None,
                "destination_status": "existing",
                "note": "Ressource calendrier",
            }
        )
    return names, mailboxes, resource_elements(elements)


def run_resource(config, args, elements) -> int:
    from . import mailbox as mailbox_module

    names, mailboxes, selected = resource_mailboxes(config, args, elements)
    if not mailboxes:
        raise RuntimeError("Aucune ressource a migrer.")
    contents = [
        value for value in selected if value in ("mail", "calendar")
    ]
    share_requested = "share" in selected
    folder_requested = "folder" in selected
    run_options = runtime(args)
    options = cli._base_options(config, run_options)
    plan = {"mailboxes": mailboxes, "options": vars(run_options)}
    failures = 0

    if folder_requested:
        report = cli.settings_module.copy_account_settings(
            config,
            plan,
            progress=cli._progress,
            only=names,
            dry_run=args.dry_run,
            options=options,
            parallelism=args.parallelism,
            cancel=cli._CONTEXT.cancel,
            parts=["folder"],
        )
        failures += cli._print_summary("Dossiers ressources", report)

    if share_requested:
        report = cli.settings_module.copy_account_settings(
            config,
            plan,
            progress=cli._progress,
            only=names,
            dry_run=args.dry_run,
            options=options,
            parallelism=args.parallelism,
            cancel=cli._CONTEXT.cancel,
            parts=["shares"],
        )
        failures += cli._print_summary("Partages ressources", report)

    if contents:
        if args.dry_run:
            cli._out(
                "Simulation ressources : %d ressource(s), contenus=%s."
                % (len(mailboxes), ", ".join(contents))
            )
        elif args.differential is not None:
            report = mailbox_module.copy_mailboxes_incremental(
                config,
                plan,
                progress=cli._progress,
                only=names,
                keep_archives=bool(args.keep_tgz),
                options=vars(run_options),
                content_types=contents,
                cancel=cli._CONTEXT.cancel,
            )
            failures += cli._print_summary("Ressources", report)
        else:
            report = mailbox_module.copy_mailboxes_chunked(
                config,
                plan,
                progress=cli._progress,
                only=names,
                keep_archives=bool(args.keep_tgz),
                options=vars(run_options),
                content_types=contents,
                cancel=cli._CONTEXT.cancel,
            )
            failures += cli._print_summary("Ressources", report)

    return 2 if failures else 0


def run_dl(config, args, elements) -> int:
    from . import diff as diff_module
    from .apply import apply_plan

    if "members" not in elements:
        raise RuntimeError("Le type dl ne supporte que l'element members.")
    source, destination = cli._load_snapshots(
        config, args.from_label, args.to_label
    )
    names = (
        [
            str(item.get("name"))
            for item in source.get("distribution_lists", [])
            if item.get("name")
        ]
        if str(args.csv).strip().lower() == "all"
        else cli.read_object_names(args.csv)
    )
    retry = retry_filter(config, args)
    if retry is not None:
        names = [name for name in names if str(name).lower() in retry]
    run_options = runtime(args)
    options = cli._base_options(config, run_options)
    plan = diff_module.generate_plan(
        config,
        source,
        destination,
        options=options,
        selection={"distribution_lists": names},
        category="distribution_lists",
    )
    report = apply_plan(
        config,
        plan,
        progress=cli._progress,
        dry_run=args.dry_run,
        kinds={"add_dl_member"},
        options=options,
        parallelism=args.parallelism,
        cancel=cli._CONTEXT.cancel,
    )
    return cli._print_summary("Listes", report)


def run_provision_objects(config, args) -> int:
    command = args.type
    field = {
        "domain": "create_domain",
        "cos": "create_cos",
        "dl": "create_dl",
        "resource": "create_resources",
    }[command]
    run_options = runtime(args, **{field: args.csv})
    return cli._run_objects(
        config,
        run_options,
        cli._base_options(config, run_options),
        cancel=cli._CONTEXT.cancel,
    )


def run_provision_account(config, args) -> int:
    source, _ = cli._load_snapshots(config, args.from_label, args.to_label)
    if str(args.csv).strip().lower() == "all":
        selected_users = [
            str(item.get("name"))
            for item in source.get("accounts", [])
            if item.get("name")
        ]
    else:
        selected_users = cli.read_users(args.csv)
    if not selected_users:
        raise RuntimeError("Aucun compte a provisionner.")
    password = args.password
    if args.password_file:
        with open(args.password_file, "r", encoding="utf-8") as handle:
            password = handle.read().strip()
    run_options = runtime(
        args,
        create_user=True,
        password=password,
        random_password=args.random_password,
        adminrights=args.keep_admin,
    )
    options = cli._base_options(config, run_options)
    if not args.dry_run:
        cli._validate_target_domains(
            config, selected_users, options, args.to_label
        )
    plan = cli._build_plan(
        config,
        selected_users,
        options,
        args.from_label,
        args.to_label,
    )
    return cli._run_create(
        config,
        plan,
        options,
        selected_users,
        args.dry_run,
        destination_label=args.to_label,
        cancel=cli._CONTEXT.cancel,
        parallelism=args.parallelism,
    )
