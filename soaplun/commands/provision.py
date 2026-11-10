"""`provision` sub-commands."""

import argparse
from types import SimpleNamespace


def add_parser(subparsers):
    parser = subparsers.add_parser(
        "provision",
        help="creer la structure sur la destination",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exemples:\n"
            "  soapalin provision domain domains.csv --from src1 --to dst1\n"
            "  soapalin provision cos all --from src1 --to dst1\n"
            "  soapalin provision account users.csv --from src1 --to dst1 --password Secret!\n"
        ),
    )
    sub = parser.add_subparsers(dest="provision_command", metavar="TYPE")
    for name, help_text in (
        ("domain", "creer/mettre a jour les domaines"),
        ("cos", "creer/mettre a jour les COS"),
        ("dl", "creer/mettre a jour les listes de distribution"),
        ("resource", "creer/mettre a jour les ressources"),
    ):
        item = sub.add_parser(name, help=help_text)
        item.add_argument("target", metavar="CSV|all")
        _connection_options(item)
        _parallelism_option(item)
    account = sub.add_parser("account", help="creer les comptes")
    account.add_argument("csv", metavar="CSV|all")
    _connection_options(account)
    _parallelism_option(account)
    account.add_argument("--keep-admin", action="store_true")
    password = account.add_mutually_exclusive_group()
    password.add_argument("--password", default="")
    password.add_argument("--password-file", default="")
    password.add_argument("--random-password", action="store_true")
    return parser


def _connection_options(parser):
    from ..cli import _add_connection_options

    _add_connection_options(parser)


def _parallelism_option(parser):
    parser.add_argument(
        "--parallelism",
        type=int,
        default=1,
        help="nombre de taches en parallele (defaut : 1)",
    )


def _runtime(args, **extra):
    values = {
        "server_source": args.from_label,
        "server_destination": args.to_label,
        "dry_run": args.dry_run,
        "keep_tgz": False,
        "keep_tgz_days": 0,
        "parallelism": getattr(args, "parallelism", 1),
        "mapping_domain": args.mapping_domain,
        "mapping_host": args.mapping_host,
        "since": "",
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


def run(args):
    from .. import cli
    from ..config import Config

    command = args.provision_command
    config = Config()
    config.use_servers(args.from_label, args.to_label)
    if command in ("domain", "cos", "dl", "resource"):
        field = {
            "domain": "create_domain",
            "cos": "create_cos",
            "dl": "create_dl",
            "resource": "create_resources",
        }[command]
        runtime = _runtime(args, **{field: args.target})
        return cli._run_objects(
            config,
            runtime,
            cli._base_options(config, runtime),
            cancel=cli._JOB_STOP_EVENT,
        )
    if command == "account":
        source, _ = cli._load_snapshots(
            config, args.from_label, args.to_label
        )
        if str(args.csv).strip().lower() == "all":
            users = [
                str(item.get("name"))
                for item in source.get("accounts", [])
                if item.get("name")
            ]
        else:
            users = cli.read_users(args.csv)
        if not users:
            raise RuntimeError("Aucun compte a provisionner.")
        password = args.password
        if args.password_file:
            with open(args.password_file, "r", encoding="utf-8") as handle:
                password = handle.read().strip()
        runtime = _runtime(
            args,
            create_user=True,
            password=password,
            random_password=args.random_password,
            adminrights=args.keep_admin,
        )
        options = cli._base_options(config, runtime)
        plan = cli._build_plan(
            config,
            users,
            options,
            args.from_label,
            args.to_label,
        )
        return cli._run_create(
            config,
            plan,
            options,
            users,
            args.dry_run,
            destination_label=args.to_label,
            cancel=cli._JOB_STOP_EVENT,
            parallelism=args.parallelism,
        )
    raise RuntimeError("Sous-commande provision inconnue : %s" % command)
