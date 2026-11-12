"""`provision` sub-commands."""

import argparse

from ..pipeline import (
    run_provision_account as _run_provision_account,
)
from ..pipeline import (
    run_provision_objects as _run_provision_objects,
)


def add_parser(subparsers):
    parser = subparsers.add_parser(
        "provision",
        help="creer la structure sur la destination",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Le type definit l'objet a provisionner :\n"
            "  account  : comptes utilisateurs\n"
            "  domain   : domaines\n"
            "  cos      : classes de service\n"
            "  dl       : listes de distribution\n"
            "  resource : ressources calendrier\n"
            "\n"
            "Exemples:\n"
            "  soapalin provision users.csv --type account --from src1 --to dst1 --password Secret!\n"
            "  soapalin provision domains.csv --type domain --from src1 --to dst1\n"
            "  soapalin provision all --type cos --from src1 --to dst1\n"
        ),
    )
    parser.add_argument("csv", metavar="CSV|all")
    _connection_options(parser)
    parser.add_argument(
        "--type",
        choices=("account", "domain", "cos", "dl", "resource"),
        required=True,
    )
    _parallelism_option(parser)
    parser.add_argument("--keep-admin", action="store_true")
    password = parser.add_mutually_exclusive_group()
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


def run(args):
    from ..config import Config

    config = Config()
    config.use_servers(args.from_label, args.to_label)
    if args.type == "account":
        return _run_provision_account(config, args)
    if args.type in ("domain", "cos", "dl", "resource"):
        return _run_provision_objects(config, args)
    raise RuntimeError("Type de provision inconnu : %s" % args.type)
