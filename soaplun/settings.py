"""Copy per-account settings, signatures, filters, shares and permissions.

Everything here is additive: missing objects are created, existing destination
objects are kept.  Incoming share mounts are reported but not recreated in V1
because they depend on folder identifiers that differ between servers.
"""

import datetime
import threading
from typing import Any, Dict, List, Optional

from . import config as config_module
from . import inventory as inventory_module
from . import storage
from .connections import ZimbraSession, describe_error, preflight
from .inventory import now_iso
from .parallel import map_ordered
from .settings_common import (
    FOLDER_RGB_COLORS,
    SETTING_PARTS,
    Progress,
    SettingsError,
    _ignored_pref,
    _map_email,
    _norm,
    _options_with_defaults,
    _pref_value,
    _rgb_to_folder_color,
    a_entries,
    normalize_parts,
    pref_entries,
)
from .settings_copy import (
    _address_values,
    _grantee_exists,
    copy_account_attributes,
    copy_cos,
    copy_empty_folders,
    copy_filters,
    copy_folder_colors,
    copy_identities,
    copy_incoming_shares,
    copy_outgoing_shares,
    copy_permissions,
    copy_preferences,
    copy_signatures,
    copy_tags,
    copy_white_black_lists,
    resolve_folder_id,
)

__all__ = [
    "FOLDER_RGB_COLORS",
    "SETTING_PARTS",
    "Progress",
    "SettingsError",
    "_address_values",
    "_grantee_exists",
    "_ignored_pref",
    "_map_email",
    "_norm",
    "_options_with_defaults",
    "_pref_value",
    "_rgb_to_folder_color",
    "a_entries",
    "copy_account_attributes",
    "copy_cos",
    "copy_empty_folders",
    "copy_filters",
    "copy_folder_colors",
    "copy_identities",
    "copy_incoming_shares",
    "copy_outgoing_shares",
    "copy_permissions",
    "copy_preferences",
    "copy_signatures",
    "copy_tags",
    "copy_white_black_lists",
    "normalize_parts",
    "pref_entries",
    "resolve_folder_id",
]






















# ----------------------------------------------------------------------
# Individual copy helpers
# ----------------------------------------------------------------------




































# ----------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------
def copy_account_settings(
    config: config_module.Config,
    plan: Dict[str, Any],
    progress: Progress = None,
    cancel: Optional[threading.Event] = None,
    only: Optional[List[str]] = None,
    dry_run: bool = False,
    options: Optional[Dict[str, Any]] = None,
    parts: Optional[List[str]] = None,
    parallelism: int = 1,
) -> Dict[str, Any]:
    sync = _options_with_defaults(config, plan, options)
    wanted_parts = set(normalize_parts(parts, sync))
    if parts:
        check = preflight(config, ("source", "destination"), check_rest=False)
        if not check.get("ok"):
            details = []
            for side, entry in check.get("sides", {}).items():
                for error in entry.get("errors", []):
                    details.append("%s : %s" % (side, error))
            raise SettingsError(
                "Preflight impossible avant la copie des reglages : "
                + " | ".join(details)
            )

    pairs = plan.get("accounts") or [
        {
            "source": mailbox.get("source", ""),
            "destination": mailbox.get("destination", ""),
        }
        for mailbox in plan.get("mailboxes", [])
    ]
    pairs = [
        pair
        for pair in pairs
        if pair.get("source")
        and pair.get("destination")
        and pair.get("status") != "conflict"
    ]
    if only:
        wanted = {value.lower() for value in only}
        pairs = [
            pair
            for pair in pairs
            if pair.get("source", "").lower() in wanted
            or pair.get("destination", "").lower() in wanted
        ]

    total = len(pairs)
    profile = config.current_profile()
    local = threading.local()

    def sessions():
        if not hasattr(local, "source"):
            local.source = ZimbraSession(config, "source", readonly=True)
            local.destination = ZimbraSession(
                config, "destination", readonly=False
            )
        return local.source, local.destination

    def process_pair(pair):
        source_email = pair.get("source", "")
        destination_email = pair.get("destination", "")
        record = {
            "source": source_email,
            "destination": destination_email,
            "counts": {},
            "errors": [],
            "warnings": [],
        }
        if cancel is not None and cancel.is_set():
            record.update(
                {
                    "status": "skipped",
                    "error": "Annule par l'utilisateur",
                }
            )
            return record
        with config.profile_context(profile):
            try:
                source_session, destination_session = sessions()
                settings = inventory_module.collect_account_settings(
                    source_session, source_email
                )
                if settings.get("error"):
                    raise SettingsError(settings["error"])
                destination_client = destination_session.account_client(
                    destination_email
                )
                destination_mail = None
                if wanted_parts.intersection(
                    {
                        "filters",
                        "permissions",
                        "folder",
                        "folders",
                        "color",
                        "shares",
                        "tags",
                    }
                ):
                    destination_mail = destination_session.mail_client(
                        destination_email
                    )

                counts = {}
                errors = []
                ignored_shares = []
                signature_ids = {}

                def merge(result, prefix):
                    for key, value in result.items():
                        if key == "errors":
                            errors.extend(value)
                        elif key == "ignored_shares":
                            ignored_shares.extend(value)
                        elif key == "id_map":
                            signature_ids.update(value)
                        elif isinstance(value, int):
                            counts["%s_%s" % (prefix, key)] = (
                                counts.get("%s_%s" % (prefix, key), 0)
                                + value
                            )

                if "settings" in wanted_parts:
                    merge(
                        copy_signatures(settings, destination_client, dry_run),
                        "signatures",
                    )
                    merge(
                        copy_identities(settings, destination_client, dry_run),
                        "identities",
                    )
                if "prefs" in wanted_parts:
                    merge(
                        copy_preferences(
                            config,
                            settings,
                            destination_client,
                            dry_run,
                            options=sync,
                            signature_id_map=signature_ids,
                        ),
                        "prefs",
                    )
                if "folder" in wanted_parts:
                    merge(
                        copy_empty_folders(
                            settings,
                            destination_mail or destination_client,
                            dry_run,
                        ),
                        "folder",
                    )
                if "folders" in wanted_parts or "color" in wanted_parts:
                    merge(
                        copy_folder_colors(
                            settings,
                            destination_mail or destination_client,
                            dry_run,
                        ),
                        "folders",
                    )
                if "tags" in wanted_parts:
                    merge(
                        copy_tags(
                            settings,
                            destination_mail or destination_client,
                            dry_run,
                        ),
                        "tags",
                    )
                if "cos" in wanted_parts:
                    merge(
                        copy_cos(
                            settings,
                            destination_session,
                            destination_email,
                            dry_run,
                        ),
                        "cos",
                    )
                if "attributes" in wanted_parts:
                    merge(
                        copy_account_attributes(
                            settings,
                            destination_session,
                            destination_email,
                            dry_run,
                            ignore=sync.get("settings_attr_ignore") or [],
                            keep_admin_permissions=bool(
                                sync.get("keep_admin_permissions", False)
                            ),
                        ),
                        "account_attrs",
                    )
                if "filters" in wanted_parts:
                    filter_client = destination_mail or destination_client
                    merge(
                        copy_filters(settings, filter_client, dry_run, "in"),
                        "filters_in",
                    )
                    merge(
                        copy_filters(settings, filter_client, dry_run, "out"),
                        "filters_out",
                    )
                if "lists" in wanted_parts:
                    merge(
                        copy_white_black_lists(
                            settings, destination_client, dry_run
                        ),
                        "lists",
                    )
                if "shares" in wanted_parts:
                    merge(
                        copy_outgoing_shares(
                            config,
                            settings,
                            destination_client,
                            destination_email,
                            dry_run,
                            options=sync,
                            destination_session=destination_session,
                            destination_mail=destination_mail,
                        ),
                        "shares",
                    )
                    merge(
                        copy_incoming_shares(
                            settings,
                            destination_session,
                            destination_client,
                            destination_email,
                            dry_run,
                            options=sync,
                            destination_mail=destination_mail,
                        ),
                        "shares_in",
                    )
                if "permissions" in wanted_parts:
                    merge(
                        copy_permissions(
                            settings,
                            destination_mail or destination_client,
                            dry_run,
                        ),
                        "permissions",
                    )

                mountpoints = len(settings.get("mountpoints") or [])
                if mountpoints and "shares" not in wanted_parts:
                    record["warnings"].append(
                        "%d partage(s) entrant(s) non recrees : a remonter "
                        "manuellement sur la destination." % mountpoints
                    )
                record["counts"] = counts
                record["errors"] = errors
                record["ignored_shares"] = ignored_shares
                record["status"] = (
                    "partial" if errors else ("planned" if dry_run else "ok")
                )
            except Exception as exc:  # noqa: BLE001
                record["status"] = "failed"
                record["error"] = describe_error(exc)
        record["finished_at"] = now_iso()
        return record

    items: List[Optional[Dict[str, Any]]] = [None] * total
    counters = {"ok": 0, "partial": 0, "failed": 0, "skipped": 0}
    lock = threading.Lock()
    completed = 0

    def completed_task(index, record):
        nonlocal completed
        items[index - 1] = record
        with lock:
            completed += 1
            if progress:
                progress(
                    completed,
                    total,
                    "%s : %s"
                    % (record.get("destination", ""), record.get("status", "")),
                )

    workers = max(1, int(parallelism or 1))
    if workers <= 1 or total <= 1:
        for index, pair in enumerate(pairs, start=1):
            if progress:
                progress(
                    index - 1,
                    total,
                    "Reglages %d/%d : %s"
                    % (index, total, pair.get("source", "")),
                )
            completed_task(index, process_pair(pair))
    else:
        if progress:
            progress(0, total, "Reglages en cours")
        map_ordered(
            pairs,
            lambda index, pair: process_pair(pair),
            workers,
            on_complete=completed_task,
        )

    for item in items:
        if not item:
            continue
        status = item.get("status", "failed")
        if status in counters:
            counters[status] += 1
        elif status == "planned":
            counters["ok"] += 1
        else:
            counters["failed"] += 1
    ignored_share_count = sum(
        len(item.get("ignored_shares") or [])
        for item in items
        if isinstance(item, dict)
    )
    report = {
        "created_at": now_iso(),
        "dry_run": dry_run,
        "ignored_shares": ignored_share_count,
        "profile": config.current_profile(),
        "profile_name": config.profile_name(),
        "source_host": config_module.store_host(config.connection("source")),
        "source_proxy_host": config_module.proxy_host(config.connection("source")),
        "destination_host": config_module.store_host(
            config.connection("destination")
        ),
        "destination_proxy_host": config_module.proxy_host(
            config.connection("destination")
        ),
        "total": total,
        "items": items,
        "summary": counters,
    }
    if not dry_run:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = config.storage_path("report_path") / (
            "settings-%s.json" % stamp
        )
        storage.write_json_atomic(path, report, indent=1, default=str)
        report["report_path"] = str(path)
    return report
