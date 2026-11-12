"""`clean` sub-command: delete temporary migration data."""

import argparse
import sys


def add_parser(subparsers):
    parser = subparsers.add_parser(
        "clean",
        help="supprimer les donnees temporaires de migration",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Cibles locales : mailboxes (defaut), jobs, reports, snapshots, trash, all.\n"
            "Cible distante : share (avec --type account et --to LABEL).\n"
            "\n"
            "Exemples:\n"
            "  soapalin clean --dry-run\n"
            "  soapalin clean mailboxes --older-than 7\n"
            "  soapalin clean all --older-than 30 --keep-last 5\n"
            "  soapalin clean snapshots --keep-last 10\n"
            "  soapalin clean all share --type account --to dst1 --dry-run\n"
            "  soapalin clean users.csv share --type account --from src1 --to dst1\n"
        ),
    )
    parser.add_argument(
        "targets",
        nargs="*",
        metavar="{mailboxes,jobs,reports,snapshots,trash,all,share}",
        default=[],
    )
    parser.add_argument(
        "--older-than",
        type=int,
        default=0,
        metavar="DAYS",
        help="ne supprimer que les elements plus anciens que N jours",
    )
    parser.add_argument(
        "--keep-last",
        type=int,
        default=0,
        metavar="N",
        help="conserver les N derniers snapshots/rapports",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=argparse.SUPPRESS,
        help="afficher ce qui serait supprime sans rien supprimer",
    )
    parser.add_argument(
        "--type",
        choices=("account",),
        default="",
        help="type de partages a nettoyer (account)",
    )
    parser.add_argument("--from", dest="from_label", default="", metavar="LABEL")
    parser.add_argument("--to", dest="to_label", default="", metavar="LABEL")
    parser.add_argument(
        "--parallelism",
        type=int,
        default=1,
        help="nombre de comptes traites en parallele (defaut : 1)",
    )
    return parser


def run(args):
    from .. import cli
    from ..cleanup import execute_cleanup, format_bytes, plan_cleanup
    from ..config import Config

    config = Config()
    targets = list(getattr(args, "targets", []) or [])
    if "share" in [str(value).lower() for value in targets]:
        return _run_share_cleanup(args, config)

    try:
        plan = plan_cleanup(
            config,
            getattr(args, "targets", []),
            older_than_days=getattr(args, "older_than", 0),
            keep_last=getattr(args, "keep_last", 0),
        )
    except ValueError as exc:
        cli._out("ERREUR : %s" % exc, file=sys.stderr)
        return 2

    dry_run = bool(getattr(args, "dry_run", False))
    report = execute_cleanup(plan, dry_run=dry_run)
    label = "simulation" if dry_run else "suppression"
    verb = "liberables" if dry_run else "liberes"
    cli._out(
        "Nettoyage (%s) : %d element(s), %s %s."
        % (
            label,
            len(report["removed"]),
            format_bytes(report["freed_bytes"]),
            verb,
        )
    )
    if dry_run:
        for path in report["removed"]:
            cli._out("  %s" % path)
    for message in report["skipped"]:
        cli._out("  conserve : %s" % message)
    for error in report["errors"]:
        cli._out("ERREUR : %s" % error, file=sys.stderr)
    return 1 if report["errors"] else 0

def _run_share_cleanup(args, config):
    from .. import cli
    from .. import inventory
    from ..connections import ZimbraSession, describe_error
    from ..parallel import map_ordered

    values = [
        str(value).strip()
        for value in (getattr(args, "targets", []) or [])
        if str(value).strip()
    ]
    selected = [value for value in values if value.lower() != "share"]
    if len(selected) != 1:
        cli._out(
            "ERREUR : clean share exige un CSV ou 'all'.",
            file=sys.stderr,
        )
        return 2
    if getattr(args, "type", "") != "account":
        cli._out("ERREUR : clean share exige --type account.", file=sys.stderr)
        return 2
    if not getattr(args, "to_label", ""):
        cli._out("ERREUR : clean share exige --to LABEL.", file=sys.stderr)
        return 2

    selector = selected[0]
    source_label = getattr(args, "from_label", "")
    destination_label = args.to_label
    source_users = None
    if selector.lower() == "all":
        if source_label:
            source_snapshot = inventory.latest_snapshot(
                config, "source", profile=source_label
            )
            if not source_snapshot:
                cli._out(
                    "ERREUR : inventaire source introuvable pour %s."
                    % source_label,
                    file=sys.stderr,
                )
                return 2
            source_users = [
                str(item.get("name"))
                for item in source_snapshot.get("accounts", [])
                if item.get("name")
            ]
        else:
            destination_snapshot = inventory.latest_snapshot(
                config, "destination", profile=destination_label
            )
            if not destination_snapshot:
                cli._out(
                    "ERREUR : inventaire destination introuvable pour %s."
                    % destination_label,
                    file=sys.stderr,
                )
                return 2
            source_users = [
                str(item.get("name"))
                for item in destination_snapshot.get("accounts", [])
                if item.get("name")
            ]
    else:
        source_users = cli.read_users(selector)

    if not source_users:
        cli._out("ERREUR : aucun compte a nettoyer.", file=sys.stderr)
        return 2

    if source_label:
        config.use_servers(source_label, destination_label)
    else:
        config.use_server(destination_label)
    profile = config.current_profile()
    dry_run = bool(getattr(args, "dry_run", False))
    workers = max(1, int(getattr(args, "parallelism", 1) or 1))
    local = __import__("threading").local()

    def sessions():
        if not hasattr(local, "source"):
            local.source = (
                ZimbraSession(config, "source", readonly=True)
                if source_label
                else None
            )
            local.destination = ZimbraSession(
                config, "destination", readonly=False
            )
        return local.source, local.destination

    def task(index, source_email):
        destination_email = (
            config.map_email(source_email) if source_label else source_email
        )
        result = {
            "source": source_email,
            "destination": destination_email,
            "removed": 0,
            "kept": 0,
            "errors": [],
        }
        with config.profile_context(profile):
            source_session, destination_session = sessions()
            expected = None
            if source_label:
                try:
                    source_mail = source_session.mail_client(source_email)
                    expected = {
                        str(link.get("path") or "")
                        for link in inventory.collect_mountpoints(source_mail)
                        if link.get("path")
                    }
                except Exception as exc:  # noqa: BLE001
                    result["errors"].append(
                        "source %s : %s" % (source_email, describe_error(exc))
                    )
                    return result
            try:
                destination_mail = destination_session.mail_client(
                    destination_email
                )
                links = inventory.collect_mountpoints(destination_mail)
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(
                    "destination %s : %s"
                    % (destination_email, describe_error(exc))
                )
                return result
            for link in links:
                path = str(link.get("path") or "")
                if not link.get("rid"):
                    continue
                if expected is not None and path in expected:
                    result["kept"] += 1
                    continue
                if dry_run:
                    result["removed"] += 1
                    continue
                try:
                    destination_mail.delete_mountpoints(
                        folder_ids=[link.get("id")]
                    )
                    result["removed"] += 1
                except Exception as exc:  # noqa: BLE001
                    result["errors"].append(
                        "suppression %s : %s"
                        % (path, describe_error(exc))
                    )
        return result

    results = map_ordered(source_users, task, workers)
    removed = sum(item["removed"] for item in results)
    kept = sum(item["kept"] for item in results)
    errors = [error for item in results for error in item["errors"]]
    for error in errors:
        cli._out("ERREUR : %s" % error, file=sys.stderr)
    cli._out(
        "Nettoyage partages (%s) : %d supprime(s), %d conserve(s), %d erreur(s)."
        % (
            "simulation" if dry_run else "suppression",
            removed,
            kept,
            len(errors),
        )
    )
    return 1 if errors else 0
