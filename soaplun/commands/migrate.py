"""`migrate` sub-command."""

import argparse


def add_parser(subparsers):
    parser = subparsers.add_parser(
        "migrate",
        help="copier les donnees vers la destination",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exemples:\n"
            "  soapalin migrate users.csv mail calendar settings --type account --from src1 --to dst1\n"
            "  soapalin migrate all --type account --from src1 --to dst1\n"
            "  soapalin migrate resources.csv mail calendar --type resource --from src1 --to dst1\n"
            "  soapalin migrate lists.csv members --type dl --from src1 --to dst1\n"
            "  soapalin migrate all --type all --from src1 --to dst1\n"
            "\n"
            "Attention : --differential accepte une date optionnelle et peut avaler\n"
            "le CSV positionnel. Ecrivez le CSV avant l'option, ou utilisez\n"
            "--differential=YYYY-MM-DD.\n"
        ),
    )
    from ..cli import _add_connection_options

    parser.add_argument("csv", metavar="CSV|all")
    parser.add_argument("elements", nargs="*", default=["all"])
    _add_connection_options(parser)
    parser.add_argument(
        "--type", choices=("account", "resource", "dl", "all"), required=True
    )
    parser.add_argument(
        "--differential",
        nargs="?",
        const=True,
        default=None,
        type=_iso_date,
        metavar="YYYY-MM-DD",
    )
    parser.add_argument(
        "--retry-failed",
        nargs="?",
        const="",
        default=None,
        metavar="RAPPORT",
    )
    parser.add_argument(
        "--parallelism",
        type=int,
        default=1,
        help="nombre de taches en parallele (defaut : 1)",
    )
    parser.add_argument("--keep-tgz", nargs="?", const=True, default=False,
                        type=_bool)
    parser.add_argument("--keep-tgz-days", type=int, default=0)
    parser.add_argument("--json", action="store_true")
    return parser


def _iso_date(value):
    from ..cli import iso_date

    return iso_date(value)


def _bool(value):
    from ..cli import _parse_bool

    return _parse_bool(value)


def _runtime(args, **extra):
    from types import SimpleNamespace

    values = {
        "server_source": args.from_label,
        "server_destination": args.to_label,
        "dry_run": args.dry_run,
        "keep_tgz": args.keep_tgz,
        "keep_tgz_days": args.keep_tgz_days,
        "parallelism": args.parallelism,
        "mapping_domain": args.mapping_domain,
        "mapping_host": args.mapping_host,
        "since": args.differential.isoformat()
        if hasattr(args.differential, "isoformat")
        else "",
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


def _users(config, args, source):
    from .. import cli

    if str(args.csv).strip().lower() == "all":
        users = [
            str(item.get("name"))
            for item in source.get("accounts", [])
            if item.get("name")
        ]
    else:
        users = cli.read_users(args.csv)
    if args.retry_failed is not None:
        retry = cli._retry_users(
            config, args.retry_failed or "", migrate_type=args.type
        )
        selected = {str(value).lower() for value in retry}
        if str(args.csv).strip().lower() != "all":
            users = [value for value in users if value.lower() in selected]
        else:
            users = retry
    return users


def _retry_filter(config, args):
    from .. import cli

    if getattr(args, "retry_failed", None) is None:
        return None
    return {
        str(value).lower()
        for value in cli._retry_users(
            config, args.retry_failed or "", migrate_type=args.type
        )
    }


def _record_simulation(kind, plan, elements):
    from .. import cli
    from ..inventory import now_iso

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
    cli._LAST_REPORTS.append(
        {
            "title": "Simulation %s" % kind,
            "report": {
                "created_at": now_iso(),
                "dry_run": True,
                "type": kind,
                "total": len(items),
                "items": items,
                "summary": {"ok": 0, "failed": 0, "skipped": 0, "planned": len(items)},
            },
        }
    )


def _run_cos(config, args, users):
    from .. import cli
    from ..apply import apply_plan

    source, destination = cli._load_snapshots(
        config, args.from_label, args.to_label
    )
    runtime = _runtime(args)
    options = cli._base_options(config, runtime)
    plan = cli._build_plan(
        config, users, options, args.from_label, args.to_label
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
            "'soapalin provision cos all'." % ", ".join(sorted(missing))
        )
    if not actions:
        raise RuntimeError(
            "Aucun compte existant avec une COS a appliquer."
        )
    plan = dict(plan, actions=actions)
    report = apply_plan(
        config,
        plan,
        progress=cli._progress,
        dry_run=args.dry_run,
        options=options,
        accounts=users,
        parallelism=args.parallelism,
        cancel=cli._JOB_STOP_EVENT,
    )
    return cli._print_summary("COS", report)


def _account_elements(elements):
    from ..cli import ACCOUNT_ELEMENTS

    return [value for value in elements if value in ACCOUNT_ELEMENTS]


def _resource_elements(elements):
    from ..cli import RESOURCE_ELEMENTS

    return [value for value in elements if value in RESOURCE_ELEMENTS]


def _run_account(config, args, elements):
    from .. import cli

    source, _ = cli._load_snapshots(config, args.from_label, args.to_label)
    users = _users(config, args, source)
    if not users:
        raise RuntimeError("Aucun compte a migrer.")
    account_elements = _account_elements(elements)
    contents = [
        value
        for value in account_elements
        if value in ("mail", "calendar", "contacts", "tasks", "briefcase")
    ]
    steps = []
    for value in account_elements:
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
    runtime = _runtime(args)
    options = cli._base_options(config, runtime)
    if contents:
        options["content_types"] = contents
    plan = cli._build_plan(
        config, users, options, args.from_label, args.to_label
    )
    if args.dry_run:
        _record_simulation("account", plan, contents or steps or ["cos"])
    failures = 0
    if "cos" in account_elements:
        failures += _run_cos(config, args, users)
    if args.differential is not None:
        if contents:
            failures += cli._run_diff(
                config,
                plan,
                options,
                users,
                runtime,
                cancel=cli._JOB_STOP_EVENT,
                force_sync_tags=True,
            )
        if steps and not args.dry_run:
            failures += cli._run_import(
                config,
                plan,
                options,
                users,
                [],
                steps,
                runtime,
                cancel=cli._JOB_STOP_EVENT,
            )
        return 2 if failures else 0
    result = cli._run_import(
        config,
        plan,
        options,
        users,
        contents,
        steps,
        runtime,
        cancel=cli._JOB_STOP_EVENT,
    )
    return 2 if failures or result else 0


def _resource_mailboxes(config, args, elements):
    from .. import cli
    from ..mailbox import TAG_CONTENT_SPECS  # validation side-effect

    source, _ = cli._load_snapshots(config, args.from_label, args.to_label)
    names = (
        [str(item.get("name")) for item in source.get("resources", []) if item.get("name")]
        if str(args.csv).strip().lower() == "all"
        else cli.read_object_names(args.csv)
    )
    retry = _retry_filter(config, args)
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
    return names, mailboxes, _resource_elements(elements)


def _run_resource(config, args, elements):
    from .. import cli
    from .. import mailbox as mailbox_module

    names, mailboxes, contents = _resource_mailboxes(config, args, elements)
    if not mailboxes:
        raise RuntimeError("Aucune ressource a migrer.")
    if args.dry_run:
        _record_simulation("resource", {"mailboxes": mailboxes}, contents)
        cli._out(
            "Simulation ressources : %d ressource(s), contenus=%s."
            % (len(mailboxes), ", ".join(contents))
        )
        return 0
    options = _runtime(args)
    plan = {"mailboxes": mailboxes, "options": vars(options)}
    if args.differential is not None:
        report = mailbox_module.copy_mailboxes_incremental(
            config,
            plan,
            progress=cli._progress,
            only=names,
            keep_archives=bool(args.keep_tgz),
            options=vars(options),
            content_types=contents,
            cancel=cli._JOB_STOP_EVENT,
        )
    else:
        report = mailbox_module.copy_mailboxes_chunked(
            config,
            plan,
            progress=cli._progress,
            only=names,
            keep_archives=bool(args.keep_tgz),
            options=vars(options),
            content_types=contents,
            cancel=cli._JOB_STOP_EVENT,
        )
    return cli._print_summary("Ressources", report)


def _run_dl(config, args, elements):
    from .. import cli
    from .. import diff as diff_module
    from ..apply import apply_plan

    if "members" not in elements:
        raise RuntimeError("Le type dl ne supporte que l'element members.")
    source, destination = cli._load_snapshots(
        config, args.from_label, args.to_label
    )
    names = (
        [str(item.get("name")) for item in source.get("distribution_lists", []) if item.get("name")]
        if str(args.csv).strip().lower() == "all"
        else cli.read_object_names(args.csv)
    )
    retry = _retry_filter(config, args)
    if retry is not None:
        names = [name for name in names if str(name).lower() in retry]
    runtime = _runtime(args)
    options = cli._base_options(config, runtime)
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
        cancel=cli._JOB_STOP_EVENT,
    )
    return cli._print_summary("Listes", report)


def run(args):
    from .. import cli

    config = cli.config_module.Config()
    config.use_servers(args.from_label, args.to_label)
    if args.type == "all":
        failures = 0
        account_elements = _account_elements(args.elements)
        resource_elements = _resource_elements(args.elements)
        if account_elements:
            failures += _run_account(config, args, account_elements)
        if resource_elements:
            failures += _run_resource(config, args, resource_elements)
        if "members" in args.elements:
            failures += _run_dl(config, args, ["members"])
        return 2 if failures else 0
    if args.type == "account":
        return _run_account(config, args, args.elements)
    if args.type == "resource":
        return _run_resource(config, args, args.elements)
    if args.type == "dl":
        return _run_dl(config, args, args.elements)
    raise RuntimeError("Type de migration inconnu : %s" % args.type)
