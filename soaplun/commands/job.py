"""`job` sub-command."""

import argparse
import datetime


def add_parser(subparsers):
    parser = subparsers.add_parser(
        "job",
        help="suivre et arreter les jobs en arriere-plan",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exemples:\n"
            "  soapalin job list\n"
            "  soapalin job list --running\n"
            "  soapalin job list --failed\n"
            "  soapalin job list --status running,failed\n"
            "  soapalin job status JOB_ID --tail 30\n"
            "  soapalin job log JOB_ID\n"
            "  soapalin job live\n"
            "  soapalin job stop all\n"
        ),
    )
    sub = parser.add_subparsers(dest="job_command", metavar="ACTION")
    list_p = sub.add_parser("list", help="lister les jobs")
    list_p.add_argument(
        "--status",
        default="",
        metavar="STATUT,...",
        help="filtrer par statut (pending,running,stopping,succeeded,failed,stopped)",
    )
    list_p.add_argument("--running", action="store_true", help="jobs actifs uniquement")
    list_p.add_argument("--failed", action="store_true", help="jobs en echec uniquement")
    for name, help_text in (
        ("status", "afficher le statut"),
        ("live", "suivre les logs en direct"),
        ("stop", "arreter un job ou tous les jobs"),
    ):
        item = sub.add_parser(name, help=help_text)
        item.add_argument("job_id", nargs="?", default="all", metavar="ID|all")
        if name == "status":
            item.add_argument(
                "--tail",
                type=int,
                default=20,
                metavar="N",
                help="afficher les N dernieres lignes du journal (0 = aucune)",
            )
    log_p = sub.add_parser("log", help="afficher le journal d'un job")
    log_p.add_argument("job_id", nargs="?", default="", metavar="ID")
    log_p.add_argument("--tail", type=int, default=0, metavar="N")
    return parser


def _duration(state):
    start = state.get("started_at") or state.get("created_at") or ""
    end = state.get("finished_at") or ""
    if not start:
        return "-"
    try:
        t0 = datetime.datetime.fromisoformat(start)
        t1 = datetime.datetime.fromisoformat(end) if end else datetime.datetime.now()
        secs = max(0, int((t1 - t0).total_seconds()))
        if secs < 60:
            return "%ds" % secs
        if secs < 3600:
            return "%dm%ds" % (secs // 60, secs % 60)
        return "%dh%dm" % (secs // 3600, (secs % 3600) // 60)
    except (TypeError, ValueError):
        return "-"


def _filter_states(states, args):
    statuses = {
        value.strip().lower()
        for value in str(getattr(args, "status", "") or "").split(",")
        if value.strip()
    }
    if getattr(args, "running", False):
        statuses.update({"pending", "running", "stopping"})
    if getattr(args, "failed", False):
        statuses.add("failed")
    if not statuses:
        return states
    return [state for state in states if str(state.get("status", "")).lower() in statuses]


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
        selected = _filter_states(selected, args)
        if not selected:
            cli._out("Aucun job.")
            return 0
        cli._out(
            "%-24s %-12s %-19s %-9s %s"
            % ("ID", "STATUT", "CREE", "DUREE", "COMMANDE")
        )
        for state in selected:
            cli._out(
                "%-24s %-12s %-19s %-9s %s"
                % (
                    state.get("id", "?"),
                    state.get("status", "?"),
                    state.get("created_at", ""),
                    _duration(state),
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
            tail = int(getattr(args, "tail", 20) or 0)
            if tail > 0:
                path = store.log_path(state["id"])
                if path.exists():
                    lines = path.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()
                    cli._out("Journal (%d dernieres lignes) :" % tail)
                    for line in lines[-tail:]:
                        cli._out("  " + line)
            cli._out("")
        return 0
    if command == "log":
        if not getattr(args, "job_id", ""):
            cli._out("ERREUR : job log exige un ID.", file=__import__("sys").stderr)
            return 2
        for state in selected:
            path = store.log_path(state["id"])
            if not path.exists():
                cli._out("Aucun journal pour %s." % state["id"])
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            lines = content.splitlines()
            shown = lines[-args.tail:] if args.tail else lines
            for line in shown:
                cli._out(line)
        return 0
    if command == "live":
        return jobs_module.follow_jobs(store, selected, out=cli._out)
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
