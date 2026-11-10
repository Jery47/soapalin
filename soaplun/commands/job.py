"""`job` sub-command."""

import argparse


def add_parser(subparsers):
    parser = subparsers.add_parser(
        "job",
        help="suivre et arreter les jobs en arriere-plan",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exemples:\n"
            "  soapalin job list\n"
            "  soapalin job status 20261110-120305-29c057\n"
            "  soapalin job live\n"
            "  soapalin job stop all\n"
        ),
    )
    sub = parser.add_subparsers(dest="job_command", metavar="ACTION")
    sub.add_parser("list", help="lister les jobs")
    for name, help_text in (
        ("status", "afficher le statut"),
        ("live", "suivre les logs en direct"),
        ("stop", "arreter un job ou tous les jobs"),
    ):
        item = sub.add_parser(name, help=help_text)
        item.add_argument("job_id", nargs="?", default="all", metavar="ID|all")
    return parser


def run(args):
    from .. import cli
    from .. import jobs as jobs_module
    from ..config import Config

    store = jobs_module.JobStore(Config())
    selector = getattr(args, "job_id", "all") or "all"
    try:
        selected = store.selected(selector)
    except KeyError as exc:
        cli._out("ERREUR : %s" % exc, file=__import__("sys").stderr)
        return 1
    command = args.job_command
    if command == "list":
        if not selected:
            cli._out("Aucun job.")
            return 0
        cli._out("%-24s %-12s %-19s %s" % ("ID", "STATUT", "CREE", "COMMANDE"))
        for state in selected:
            cli._out(
                "%-24s %-12s %-19s %s"
                % (
                    state.get("id", "?"),
                    state.get("status", "?"),
                    state.get("created_at", ""),
                    state.get("title", ""),
                )
            )
        return 0
    if command == "status":
        if not selected:
            cli._out("Aucun job.")
            return 0
        for state in selected:
            cli._out("Job     : %s" % state.get("id", ""))
            cli._out("Statut  : %s" % state.get("status", ""))
            cli._out("Commande: %s" % state.get("title", ""))
            cli._out("PID     : %s" % state.get("pid", 0))
            cli._out("Debut   : %s" % state.get("started_at", ""))
            cli._out("Fin     : %s" % state.get("finished_at", ""))
            cli._out("Journal : %s" % state.get("log_path", ""))
            if state.get("error"):
                cli._out("Erreur  : %s" % state.get("error"))
            cli._out("")
        return 0
    if command == "live":
        return jobs_module.follow_jobs(store, selected)
    if command == "stop":
        stopped = 0
        for state in selected:
            if state.get("status") in jobs_module.TERMINAL_STATUSES:
                continue
            store.request_stop(state["id"])
            cli._out("Arret demande : %s" % state["id"])
            stopped += 1
        if not stopped:
            cli._out("Aucun job actif.")
        return 0
    cli._out("Action job inconnue : %s" % command, file=__import__("sys").stderr)
    return 2
