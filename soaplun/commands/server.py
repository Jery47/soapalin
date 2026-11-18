"""`server` sub-commands."""

import argparse
import json
import sys


def add_parser(subparsers):
    parser = subparsers.add_parser(
        "server",
        help="gerer les serveurs Zimbra",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exemples:\n"
            "  soapalin server add src1 --role source\n"
            "  soapalin server add src1 --role source --host zimbra-src.local --admin admin --password Secret!\n"
            "  soapalin server list\n"
            "  soapalin server test src1\n"
            "  soapalin server inventory src1\n"
            "  soapalin server inventory src1 --stats --parallelism 4\n"
            "  soapalin server inventory src1 --follow\n"
        ),
    )
    sub = parser.add_subparsers(dest="server_command", metavar="ACTION")
    add = sub.add_parser("add", help="ajouter un serveur")
    add.add_argument("label")
    add.add_argument("--role", choices=("source", "destination"), required=True)
    add.add_argument("--host", default="", help="hote admin/store Zimbra")
    add.add_argument("--port", type=int, default=None, help="port admin (defaut : 7071)")
    add.add_argument("--admin", default="", help="login administrateur")
    add.add_argument("--password", default="", help="mot de passe administrateur")
    add.add_argument(
        "--no-test",
        action="store_true",
        help="ne pas tester la connexion apres creation",
    )
    for name, help_text in (
        ("test", "tester la connexion"),
        ("inventory", "lancer un inventaire"),
    ):
        item = sub.add_parser(name, help=help_text)
        item.add_argument("label")
        if name == "inventory":
            item.add_argument(
                "--stats",
                action="store_true",
                help="collecter les statistiques GetMailbox (plus lent)",
            )
            item.add_argument(
                "--no-stats",
                action="store_true",
                help="compatibilite : ne pas collecter les statistiques",
            )
            item.add_argument(
                "--parallelism",
                type=int,
                default=1,
                help="nombre de collectes de comptes en parallele (defaut : 1)",
            )
            item.add_argument(
                "--follow",
                action="store_true",
                help="suivre les logs en direct apres le lancement",
            )
    rename = sub.add_parser("rename", help="renommer un serveur")
    rename.add_argument("label")
    rename.add_argument("new_label")
    sub.add_parser("list", help="lister les serveurs")
    show = sub.add_parser("show", help="afficher un ou tous les serveurs")
    show.add_argument("label", nargs="?")
    remove = sub.add_parser("remove", help="supprimer un serveur")
    remove.add_argument("label")
    return parser


def run(args):
    from .. import cli
    from ..config import Config

    config = Config()
    command = args.server_command
    if command == "add":
        server = config.create_server(args.label, args.role)
        connection = dict(server.get("connection") or {})
        interactive = (
            not any(
                [
                    getattr(args, "host", ""),
                    getattr(args, "admin", ""),
                    getattr(args, "password", ""),
                ]
            )
            and sys.stdin.isatty()
        )
        update = {}
        if getattr(args, "host", ""):
            update["store_host"] = args.host
        if getattr(args, "port", None) is not None:
            update["store_admin_port"] = int(args.port)
        if getattr(args, "admin", ""):
            update["admin_login"] = args.admin
        if getattr(args, "password", ""):
            update["admin_password"] = args.password
        if interactive:
            cli._out("Configuration de la connexion (Entree = ignorer) :")
            update["store_host"] = _prompt(
                "Hote admin/store", connection.get("store_host") or "localhost"
            )
            update["store_admin_port"] = int(
                _prompt(
                    "Port admin",
                    str(connection.get("store_admin_port") or 7071),
                )
            )
            update["admin_login"] = _prompt(
                "Login admin", connection.get("admin_login") or "admin"
            )
            password = _prompt_password()
            if password is not None:
                update["admin_password"] = password
        if update:
            connection.update(update)
            config.save_server(args.label, connection)
            server = config.server(args.label)
        cli._out(
            "Serveur cree : %s (%s)\nFichier : %s"
            % (server["label"], server["type"], server["path"])
        )
        should_test = not getattr(args, "no_test", False) and bool(
            getattr(args, "host", "")
            or getattr(args, "admin", "")
            or getattr(args, "password", "")
        )
        if interactive:
            answer = _prompt("Tester la connexion maintenant", "O").lower()
            should_test = answer not in {"n", "no", "non"}
        if should_test and (update or connection.get("store_host")):
            result = cli._run_server_test(config, args.label)
            if result:
                return result
        return 0
    if command == "list":
        servers = config.servers()
        if not servers:
            cli._out("Aucun serveur configure.")
            return 0
        for server in servers.values():
            cli._out(
                "%-28s %-12s %s"
                % (
                    server["label"],
                    server["type"],
                    server.get("connection", {}).get("store_host", ""),
                )
            )
        return 0
    if command == "show":
        if args.label:
            cli._out(
                json.dumps(
                    cli._server_public(config.server(args.label)),
                    indent=2,
                    ensure_ascii=False,
                )
            )
        else:
            for server in config.servers().values():
                cli._out(
                    json.dumps(
                        cli._server_public(server),
                        indent=2,
                        ensure_ascii=False,
                    )
                )
        return 0
    if command == "remove":
        config.remove_server(args.label)
        cli._out("Serveur supprime : %s" % args.label)
        return 0
    if command == "rename":
        new_label = config.rename_server(args.label, args.new_label)
        cli._out("Serveur renomme : %s -> %s" % (args.label, new_label))
        return 0
    if command == "test":
        return cli._run_server_test(config, args.label)
    if command == "inventory":
        import threading

        return cli._run_server_inventory(
            config,
            args.label,
            with_stats=bool(args.stats) and not args.no_stats,
            parallelism=args.parallelism,
            cancel=cli._CONTEXT.cancel or threading.Event(),
        )
    raise RuntimeError("Action serveur inconnue : %s" % command)


def _prompt(label, default):
    try:
        value = input("  %s [%s] : " % (label, default))
    except (EOFError, KeyboardInterrupt):
        return default
    return value.strip() or str(default)


def _prompt_password():
    try:
        import getpass

        return getpass.getpass("  Mot de passe admin : ")
    except (EOFError, KeyboardInterrupt):
        return None
