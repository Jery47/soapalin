"""Command line interface for Soap a l'un."""

import argparse
import datetime
import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import __version__
from . import config as config_module
from . import diff as diff_module
from . import inventory as inventory_module
from . import jobs as jobs_module
from . import mailbox as mailbox_module
from . import settings as settings_module
from .apply import apply_plan
from .connections import ZimbraSession, describe_error
from .context import RunContext
from .inputs import (
    ACCOUNT_ELEMENTS,
    DL_ELEMENTS,
    ELEMENT_ALIASES,
    RESOURCE_ELEMENTS,
    iso_date,
    normalize_migrate_elements,
    read_mapping,
    read_object_names,
    read_object_selection,
    read_users,
)
from .inputs import (
    elements_for_type as _elements_for_type,
)
from .storage import write_json_atomic

__all__ = [
    "ACCOUNT_ELEMENTS",
    "DL_ELEMENTS",
    "ELEMENT_ALIASES",
    "RESOURCE_ELEMENTS",
    "_elements_for_type",
    "apply_plan",
    "diff_module",
    "iso_date",
    "mailbox_module",
    "normalize_migrate_elements",
    "read_mapping",
    "read_object_names",
    "read_object_selection",
    "read_users",
    "settings_module",
]

_QUIET = False
_CONTEXT = RunContext()


def _out(*args, **kwargs):
    if kwargs.get("file") is sys.stderr or not _QUIET:
        print(*args, **kwargs)


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on", "oui"}:
        return True
    if text in {"0", "false", "no", "off", "non"}:
        return False
    raise argparse.ArgumentTypeError("valeur booleenne attendue (true/false)")


def _add_global_options(parser):
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--foreground", action="store_true")
    parser.add_argument(
        "--directly-clean",
        action="store_true",
        help="deplacer les archives temporaires vers data/trash apres chaque compte",
    )
    parser.add_argument("--job-worker", default="", help=argparse.SUPPRESS)


def _add_connection_options(parser):
    parser.add_argument("--from", dest="from_label", required=True, metavar="LABEL")
    parser.add_argument("--to", dest="to_label", required=True, metavar="LABEL")
    parser.add_argument("--mapping-domain", default="", metavar="CSV")
    parser.add_argument("--mapping-host", default="", metavar="CSV")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="soapalin",
        description="Migration Zimbra en sous-commandes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Options de migrate :\n"
            "  --type definit le type d'objet migre :\n"
            "    account  : compte utilisateur (mail, settings, folders, shares...)\n"
            "    resource : ressource calendrier (mail, calendar)\n"
            "    dl       : liste de distribution (members uniquement)\n"
            "    all      : compte + ressource + dl (CSV obligatoirement 'all')\n"
            "  Les elements doivent appartenir au type ; un element incompatible est ignore.\n"
            "  CSV|all ELEMENTS --type {account,resource,dl,all}\n"
            "    --from LABEL --to LABEL\n"
            "    --mapping-domain CSV --mapping-host CSV\n"
            "    --differential [YYYY-MM-DD]\n"
            "    --retry-failed [RAPPORT]\n"
            "    --parallelism N (defaut : 1)\n"
            "    --keep-tgz [true|false] --keep-tgz-days N\n"
            "    --json (uniquement avec --dry-run)\n"
            "  account  : mail calendar contacts tasks briefcase cos folder color settings tags\n"
            "             filter banlist whitelist share sendas sendonbehalfof\n"
            "  resource : mail calendar\n"
            "  dl       : members\n"
            "\n"
            "Exemples:\n"
            "  soapalin server add src1 --role source\n"
            "  soapalin server add dst1 --role destination\n"
            "  soapalin server list\n"
            "  soapalin server test src1\n"
            "  soapalin server inventory src1\n"
            "  soapalin server inventory src1 --no-stats\n"
            "  soapalin server inventory src1 --no-stats --parallelism 4\n"
            "  soapalin provision domains.csv --type domain --from src1 --to dst1\n"
            "  soapalin provision all --type cos --from src1 --to dst1\n"
            "  soapalin provision users.csv --type account --from src1 --to dst1 --password Secret!\n"
            "  soapalin provision users.csv --type account --from src1 --to dst1 --random-password --keep-admin\n"
            "  soapalin migrate users.csv mail calendar settings --type account --from src1 --to dst1\n"
            "  soapalin migrate users.csv cos --type account --from src1 --to dst1\n"
            "  soapalin migrate all --type account --from src1 --to dst1\n"
            "  soapalin migrate resources.csv mail calendar --type resource --from src1 --to dst1\n"
            "  soapalin migrate lists.csv members --type dl --from src1 --to dst1\n"
            "  soapalin migrate all --type all --from src1 --to dst1\n"
            "  soapalin migrate users.csv mail --type account --differential --from src1 --to dst1\n"
            "  soapalin migrate users.csv mail --type account --differential=2026-10-01 --from src1 --to dst1\n"
            "  soapalin migrate users.csv mail --type account --retry-failed --from src1 --to dst1\n"
            "  soapalin --dry-run migrate users.csv all --type account --json --from src1 --to dst1\n"
            "  soapalin --foreground migrate users.csv mail --type account --from src1 --to dst1\n"
            "  soapalin job list\n"
            "  soapalin job status JOB_ID\n"
            "  soapalin job live\n"
            "  soapalin job stop all\n"
        ),
    )
    _add_global_options(parser)
    sub = parser.add_subparsers(dest="command", metavar="{server,provision,migrate,job,clean}")

    from .commands import clean as clean_cmd
    from .commands import job as job_cmd
    from .commands import migrate as migrate_cmd
    from .commands import provision as provision_cmd
    from .commands import server as server_cmd

    server_cmd.add_parser(sub)
    provision_cmd.add_parser(sub)
    migrate_cmd.add_parser(sub)
    job_cmd.add_parser(sub)
    clean_cmd.add_parser(sub)
    return parser


def validate(args, parser) -> None:
    if not args.command:
        parser.error("une sous-commande est obligatoire.")
    if args.command in ("provision", "migrate"):
        if not args.from_label or not args.to_label:
            parser.error("--from et --to sont obligatoires.")
        if getattr(args, "json", False) and not args.dry_run:
            parser.error("--json est uniquement autorise avec --dry-run.")
    if args.command == "provision":
        if not getattr(args, "type", ""):
            parser.error("--type est obligatoire.")
        if args.type == "account":
            if not (
                args.password
                or args.password_file
                or args.random_password
            ):
                parser.error(
                    "--password, --password-file ou --random-password est obligatoire."
                )
    if args.command == "migrate":
        if not args.type:
            parser.error("--type est obligatoire.")
        if args.type == "all" and str(args.csv).strip().lower() != "all":
            parser.error("--type all exige le CSV 'all'.")
        try:
            args.elements = normalize_migrate_elements(args.type, args.elements)
        except ValueError as exc:
            parser.error(str(exc))
        if getattr(args, "retry_failed", None) is not None and not args.csv:
            parser.error("--retry-failed exige un CSV ou 'all'.")
    if args.command == "job":
        if not getattr(args, "job_command", ""):
            parser.error("une sous-commande job est obligatoire.")

























def _server_public(server: Dict[str, Any]) -> Dict[str, Any]:
    connection = dict(server.get("connection") or {})
    if connection.get("admin_password"):
        connection["admin_password"] = "********"
    return {
        "label": server.get("label"),
        "type": server.get("type"),
        "path": server.get("path"),
        "connection": connection,
    }


def _run_server_test(config, label: str) -> int:
    server = config.server(label)
    config.use_server(label)
    session = ZimbraSession(config, server["type"], readonly=True)
    try:
        result = session.test()
    except Exception as exc:  # noqa: BLE001
        _out("%s : ECHEC - %s" % (label, describe_error(exc)), file=sys.stderr)
        return 1
    _out(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


def _run_server_inventory(
    config,
    label: str,
    with_stats: bool = True,
    parallelism: int = 1,
    cancel=None,
) -> int:
    server = config.server(label)
    config.use_server(label)
    report = inventory_module.collect_snapshot(
        config,
        server["type"],
        progress=_progress,
        with_stats=with_stats,
        profile=label,
        cancel=cancel,
        parallelism=parallelism,
    )
    _out(
        "%s (%s) : %s comptes, %s domaines, snapshot=%s"
        % (
            label,
            server["type"],
            (report.get("counts") or {}).get("accounts", 0),
            (report.get("counts") or {}).get("domains", 0),
            report.get("path", ""),
        )
    )
    return 0


def _launch_job(argv, args) -> int:
    config = config_module.Config()
    store = jobs_module.JobStore(config)
    arguments = list(argv) if argv is not None else list(sys.argv[1:])
    title = " ".join(arguments)[:180]
    spec = store.create(arguments, title=title)
    try:
        store.launch(spec)
    except Exception as exc:  # noqa: BLE001
        store.update(spec["id"], status="failed", error=describe_error(exc))
        _out("ERREUR : lancement du job impossible : %s" % describe_error(exc), file=sys.stderr)
        return 1
    _out("Job lance : %s" % spec["id"])
    _out("Statut  : soapalin job status %s" % spec["id"])
    _out("Suivi   : soapalin job live %s" % spec["id"])
    _out("Arret   : soapalin job stop %s" % spec["id"])
    return 0


def _run_job_worker(spec_path: str) -> int:
    import traceback

    try:
        with open(spec_path, "r", encoding="utf-8") as handle:
            spec = json.load(handle)
    except (OSError, ValueError) as exc:
        _out("ERREUR job : specification illisible : %s" % exc, file=sys.stderr)
        return 1
    base_dir = spec.get("base_dir") or str(Path(spec_path).resolve().parent)
    os.environ["SOAPLUN_HOME"] = str(base_dir)
    cwd = spec.get("cwd")
    if cwd:
        try:
            os.chdir(cwd)
        except OSError:
            pass
    config = config_module.Config(base_dir)
    store = jobs_module.JobStore(config)
    job_id = str(spec.get("id") or "")
    if job_id:
        os.environ["SOAPLUN_JOB_ID"] = job_id
    store.update(
        job_id,
        status="running",
        pid=os.getpid(),
        started_at=datetime.datetime.now().isoformat(timespec="seconds"),
    )
    cancel = threading.Event()
    _CONTEXT.cancel = cancel
    done = threading.Event()
    watcher = threading.Thread(
        target=jobs_module.watch_stop,
        args=(store, job_id, cancel, done),
        daemon=True,
    )
    watcher.start()
    returncode = 1
    error = ""
    try:
        returncode = main(spec.get("argv") or [], _from_job=True)
    except KeyboardInterrupt:
        returncode = 130
        cancel.set()
    except SystemExit as exc:
        returncode = int(exc.code or 1)
    except BaseException as exc:  # noqa: BLE001
        traceback.print_exc()
        error = describe_error(exc)
        returncode = 1
    finally:
        done.set()
        _CONTEXT.cancel = None
    stopped = store.stop_requested(job_id)
    if stopped:
        status = "stopped"
    elif returncode == 0:
        status = "succeeded"
    else:
        status = "failed"
    store.update(
        job_id,
        status=status,
        returncode=returncode,
        finished_at=datetime.datetime.now().isoformat(timespec="seconds"),
        error=error,
    )
    return returncode


def _write_interrupt_report(config, cleaned: List[str]) -> str:
    try:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        directory = config.storage_path("report_path")
        path = directory / ("interrupted-%s.json" % stamp)
        payload = {
            "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "status": "interrupted",
            "signal": "SIGINT",
            "cleaned_partials": len(cleaned),
            "cleaned_files": cleaned,
            "summary": {"ok": 0, "failed": 0, "skipped": 0, "planned": 0},
            "items": [],
        }
        write_json_atomic(path, payload, indent=1)
        return str(path)
    except Exception:  # noqa: BLE001 - interruption must not fail again
        return ""


def _should_background(args) -> bool:
    if args.command in ("provision", "migrate"):
        return True
    if args.command == "server" and getattr(args, "server_command", "") == "inventory":
        return True
    return False


def main(argv: Optional[Sequence[str]] = None, _from_job: bool = False) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.job_worker:
        return _run_job_worker(args.job_worker)
    global _QUIET
    _QUIET = bool(args.quiet)
    level = logging.DEBUG if (args.debug or args.verbose) else logging.WARNING
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("soapalin").debug("argument(s): %s", vars(args))
    validate(args, parser)

    from .commands import clean as clean_cmd
    from .commands import job as job_cmd
    from .commands import migrate as migrate_cmd
    from .commands import provision as provision_cmd
    from .commands import server as server_cmd

    if not _from_job and not args.foreground and _should_background(args):
        return _launch_job(argv, args)

    config = None
    try:
        if args.command == "clean":
            return clean_cmd.run(args)
        if args.command == "job":
            return job_cmd.run(args)
        if args.command == "server":
            return server_cmd.run(args)
        config = config_module.Config()
        if args.command == "provision":
            return provision_cmd.run(args)
        if args.command == "migrate":
            _CONTEXT.reports.clear()
            result = migrate_cmd.run(args)
            if getattr(args, "json", False) and args.dry_run:
                _out(
                    json.dumps(
                        {"dry_run": True, "reports": _CONTEXT.reports},
                        ensure_ascii=False,
                        indent=1,
                        default=str,
                    )
                )
            return result
        parser.error("sous-commande inconnue.")
        return 2
    except KeyboardInterrupt:
        cancel = _CONTEXT.cancel
        if cancel is not None:
            cancel.set()
        cleaned = []
        if config is not None:
            try:
                from . import mailbox as mailbox_module

                cleaned = mailbox_module.cleanup_partial_archives(config)
            except Exception:  # noqa: BLE001
                cleaned = []
            path = _write_interrupt_report(config, cleaned)
            if path:
                _out("Rapport d'interruption : %s" % path)
        _out(
            "INTERRUPTION : transfert arrete, %d archive(s) partielle(s) supprimee(s)."
            % len(cleaned),
            file=sys.stderr,
        )
        return 130
    except Exception as exc:  # noqa: BLE001
        _out("ERREUR : %s" % describe_error(exc), file=sys.stderr)
        return 1


from .runner import (  # noqa: E402,F401
    _base_options,
    _build_plan,
    _load_snapshots,
    _print_summary,
    _progress,
    _retry_users,
    _run_create,
    _run_diff,
    _run_import,
    _run_objects,
    _validate_target_domains,
    _write_passwords,
)

if __name__ == "__main__":
    raise SystemExit(main())
