"""`server` sub-commands."""

import argparse
import json


def add_parser(subparsers):
    parser = subparsers.add_parser(
        "server",
        help="gerer les serveurs Zimbra",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exemples:\n"
            "  soapalin server add src1 --role source\n"
            "  soapalin server list\n"
            "  soapalin server test src1\n"
            "  soapalin server inventory src1\n"
        ),
    )
    sub = parser.add_subparsers(dest="server_command", metavar="ACTION")
    add = sub.add_parser("add", help="ajouter un serveur")
    add.add_argument("label")
    add.add_argument("--role", choices=("source", "destination"), required=True)
    for name, help_text in (
        ("test", "tester la connexion"),
        ("inventory", "lancer un inventaire"),
    ):
        item = sub.add_parser(name, help=help_text)
        item.add_argument("label")
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
        cli._out(
            "Serveur cree : %s (%s)\nFichier : %s"
            % (server["label"], server["type"], server["path"])
        )
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
            config, args.label, cancel=cli._JOB_STOP_EVENT or threading.Event()
        )
    raise RuntimeError("Action serveur inconnue : %s" % command)
