"""`migrate` sub-command."""

import argparse

from ..pipeline import (
    account_elements as _account_elements,
)
from ..pipeline import (
    resource_elements as _resource_elements,
)
from ..pipeline import (
    run_account as _pipeline_run_account,
)
from ..pipeline import (
    run_cos as _run_cos,
)
from ..pipeline import (
    run_dl as _run_dl,
)
from ..pipeline import (
    run_resource as _run_resource,
)
from ..pipeline import (
    users as _users,
)

__all__ = [
    "_account_elements",
    "_pipeline_run_account",
    "_resource_elements",
    "_run_account",
    "_run_cos",
    "_run_dl",
    "_run_resource",
    "_users",
]


def _run_account(config, args, elements):
    return _pipeline_run_account(config, args, elements, users_fn=_users)


def add_parser(subparsers):
    parser = subparsers.add_parser(
        "migrate",
        help="copier les donnees vers la destination",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Le type definit l'objet migre :\n"
            "  account  : mail calendar contacts tasks briefcase cos folder color settings tags\n"
            "             filter banlist whitelist share sendas sendonbehalfof\n"
            "  resource : mail calendar folder share\n"
            "  dl       : members uniquement (une DL n'a ni dossiers, ni settings, ni partages)\n"
            "  all      : account + resource + dl, avec CSV 'all'\n"
            "\n"
            "Exemples:\n"
            "  soapalin migrate users.csv --type account --item mail,calendar,settings --from src1 --to dst1\n"
            "  soapalin migrate users.csv --type account --item all --from src1 --to dst1\n"
            "  soapalin migrate all --type account --item all --from src1 --to dst1\n"
            "  soapalin migrate resources.csv --type resource --item mail,calendar --from src1 --to dst1\n"
            "  soapalin migrate lists.csv --type dl --item members --from src1 --to dst1\n"
            "  soapalin migrate all --type all --item all --from src1 --to dst1\n"
            "\n"
            "Attention : --differential accepte une date optionnelle et peut avaler\n"
            "le CSV positionnel. Ecrivez le CSV avant l'option, ou utilisez\n"
            "--differential=YYYY-MM-DD.\n"
        ),
    )
    from ..cli import _add_connection_options

    parser.add_argument("csv", metavar="CSV|all")
    parser.add_argument(
        "--item",
        required=True,
        metavar="ELEMENTS",
        help="elements separes par des virgules, sans espace (ex. mail,calendar)",
    )
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
    parser.add_argument(
        "--resume",
        action="store_true",
        help="reprendre la derniere copie de boites interrompue",
    )
    parser.add_argument("--json", action="store_true")
    return parser


def _iso_date(value):
    from ..cli import iso_date

    return iso_date(value)


def _bool(value):
    from ..cli import _parse_bool

    return _parse_bool(value)


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
