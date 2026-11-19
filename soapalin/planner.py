"""Build migration plans from source and destination snapshots."""

from typing import Any, Dict, List, Optional, cast

from . import config as config_module
from .diff import (
    PLAN_CATEGORY_GROUPS,
    PLAN_CONTENT_TYPES,
    PLANNER_VERSION,
    PlanMapper,
    action_sort_key,
    normalize_options,
    normalize_selection,
    validate_snapshots,
)
from .inventory import now_iso
from .models import Plan, PlanOptions
from .planner_accounts import AccountPlanMixin
from .planner_domains import DomainPlanMixin
from .planner_resources import ResourcePlanMixin


class PlanBuilder(DomainPlanMixin, AccountPlanMixin, ResourcePlanMixin):
    """Incrementally build a migration plan from two inventory snapshots."""

    def __init__(
        self,
        config: config_module.Config,
        source: Dict[str, Any],
        destination: Dict[str, Any],
        include_mailbox: Optional[bool] = None,
        options: Optional[PlanOptions] = None,
        selection: Optional[Dict[str, Any]] = None,
        category: str = "global",
    ):
        self.base_config = config
        self.source = source or {}
        self.destination = destination or {}
        self.category = category
        self.sync = normalize_options(config, options)
        self.picked = normalize_selection(selection, self.source)
        self._filter_category()
        self.validation = validate_snapshots(
            config, self.source, self.destination, self.sync
        )
        self.mapper = PlanMapper(config, self.sync)
        if include_mailbox is None:
            include_mailbox = bool(self.sync.get("copy_mailbox", True))
        self.include_mailbox = include_mailbox

        self.source_domains = {
            item["name"].lower(): item for item in self.source.get("domains", [])
        }
        self.dest_domains = {
            item["name"].lower(): item
            for item in self.destination.get("domains", [])
        }
        self.source_cos = {
            item["name"].lower(): item for item in self.source.get("cos", [])
        }
        self.dest_cos = {
            item["name"].lower(): item
            for item in self.destination.get("cos", [])
        }
        self.dest_accounts = {
            account["name"].lower(): account
            for account in self.destination.get("accounts", [])
            if account.get("name")
        }
        self.dest_alias_owner: Dict[str, str] = {}
        for account in self.destination.get("accounts", []):
            for alias in account.get("aliases", []) or []:
                if alias:
                    self.dest_alias_owner[str(alias).lower()] = account.get(
                        "name", ""
                    )
        self.dest_dls = {
            item["name"].lower(): item
            for item in self.destination.get("distribution_lists", [])
            if item.get("name")
        }
        self.dest_resources = {
            item["name"].lower(): item
            for item in self.destination.get("resources", [])
            if item.get("name")
        }
        self.dest_servers = [
            item.get("name", "") for item in self.destination.get("servers", [])
        ]
        self.dest_servers_lower = {name.lower() for name in self.dest_servers}
        self.host_map_keys = {
            str(key).lower()
            for key in (self.sync.get("mail_host_map") or {})
        }

        self.actions: List[Dict[str, Any]] = []
        self.warnings: List[str] = []
        self.info: List[str] = []
        self.unknown_hosts: set = set()
        self.account_pairs: List[Dict[str, Any]] = []
        self.mailboxes: List[Dict[str, Any]] = []
        self.planned_emails: Dict[str, str] = {}
        self.fixed_password = bool(self.sync.get("fixed_password"))
        self.random_password = bool(self.sync.get("random_password"))
        self.password_source = (
            "random"
            if self.random_password
            else ("fixed" if self.fixed_password else "source_unknown")
        )
        self.reset_existing = bool(self.sync.get("reset_existing_passwords"))
        self.selected_emails = {
            str(value).lower() for value in self.picked.get("accounts") or []
        }
        self.selected_domain_names = (
            set(self.picked["domains"])
            if self.picked.get("domains") is not None
            else None
        )
        self.selected_cos = (
            set(self.picked["cos"])
            if self.picked.get("cos") is not None
            else None
        )
        self.selected_dls = (
            set(self.picked["distribution_lists"])
            if self.picked.get("distribution_lists") is not None
            else None
        )
        self.selected_resources = (
            set(self.picked["resources"])
            if self.picked.get("resources") is not None
            else None
        )
        self.target_domains: List[str] = []
        self.summary: Dict[str, int] = {}

    def _filter_category(self) -> None:
        allowed_groups = PLAN_CATEGORY_GROUPS.get(self.category)
        if allowed_groups is None:
            return
        for key in (
            "accounts",
            "domains",
            "cos",
            "distribution_lists",
            "resources",
        ):
            if key not in allowed_groups:
                self.picked[key] = []
        if "accounts" not in allowed_groups:
            self.picked["all_accounts"] = False
        self.picked["include_domains"] = (
            self.picked["domains"] is None or bool(self.picked["domains"])
        )
        self.picked["include_cos"] = (
            self.picked["cos"] is None or bool(self.picked["cos"])
        )
        self.picked["include_distribution_lists"] = (
            self.picked["distribution_lists"] is None
            or bool(self.picked["distribution_lists"])
        )
        self.picked["include_resources"] = (
            self.picked["resources"] is None or bool(self.picked["resources"])
        )

    def target_host(self, source_host: str) -> str:
        if not source_host:
            return ""
        if (
            source_host.lower() in self.host_map_keys
            or self.sync.get("default_mail_host")
        ):
            return self.mapper.map_host(source_host)
        if source_host.lower() in self.dest_servers_lower:
            return source_host
        self.unknown_hosts.add(source_host)
        return ""

    # -- Domains -------------------------------------------------------
    def collect_warnings(self) -> None:
        source_emails = {
            self.mapper.map_email(a.get("name", "")).lower()
            for a in self.source.get("accounts", [])
        }
        extras = sorted(
            name for name in self.dest_accounts if name not in source_emails
        )
        if extras and self.account_pairs:
            self.info.append(
                "%d compte(s) présent(s) uniquement sur la destination seront "
                "conservés (aucune suppression)." % len(extras)
            )
        source_dl_names = {
            self.mapper.map_email(d.get("name", "")).lower()
            for d in self.source.get("distribution_lists", [])
        }
        extra_dls = sorted(
            name for name in self.dest_dls if name not in source_dl_names
        )
        if extra_dls and (self.selected_dls is None or self.selected_dls):
            self.info.append(
                "%d liste(s) de distribution supplémentaire(s) seront conservées."
                % len(extra_dls)
            )

        for name in sorted(self.source_domains):
            if (
                self.selected_domain_names is not None
                and name not in self.selected_domain_names
            ):
                continue
            mapped = self.mapper.map_domain(name)
            if mapped.lower() not in self.dest_domains and mapped.lower() not in {
                d.lower() for d in self.target_domains
            }:
                self.warnings.append(
                    "Le domaine %s n'existe pas sur la destination et ne sera pas "
                    "créé : vérifiez le mapping." % mapped
                )

        for host in sorted(self.unknown_hosts):
            self.warnings.append(
                "L'hôte mail %s n'existe pas sur la destination et n'est pas mappé : "
                "les comptes seront créés sans zimbraMailHost explicite (Zimbra "
                "choisira l'hôte par défaut)." % host
            )

        limit = int(self.sync.get("max_accounts_per_plan") or 0)
        selected_account_count = (
            len(self.picked.get("accounts") or [])
            if self.picked.get("accounts") is not None
            else len(self.source.get("accounts", []))
        )
        if limit and selected_account_count > limit:
            self.warnings.append(
                "Le plan couvre %d comptes, au-delà de la limite configurée (%d). "
                "Appliquez-le par lots ou augmentez la limite."
                % (selected_account_count, limit)
            )

        if not self.picked["include_domains"] and self.account_pairs:
            self.warnings.append(
                "Les domaines ne sont pas inclus dans ce plan : les comptes ne "
                "pourront être créés que si leurs domaines existent déjà sur la "
                "destination."
            )
        if self.picked.get("accounts") is not None:
            wanted = {
                str(value).lower() for value in self.picked["accounts"]
            }
            known = {
                str(account.get("name", "")).lower()
                for account in self.source.get("accounts", [])
                if account.get("name")
            }
            missing = sorted(wanted - known)
            if missing:
                sample = ", ".join(missing[:10])
                if len(missing) > 10:
                    sample += ", ..."
                self.warnings.append(
                    "%d compte(s) saisi(s) introuvable(s) dans l'inventaire source : %s"
                    % (len(missing), sample)
                )

        if not self.picked["include_cos"] and self.account_pairs:
            self.warnings.append(
                "Les COS ne sont pas incluses dans ce plan : vérifiez que les COS "
                "cibles existent déjà sur la destination."
            )

    # -- Finalization --------------------------------------------------
    def finalize(self) -> None:
        self.actions.sort(key=action_sort_key)
        for item in self.actions:
            self.summary[item["kind"]] = self.summary.get(item["kind"], 0) + 1

    def build_id_map(self) -> List[Dict[str, str]]:
        source_accounts_by_name = {
            str(account.get("name", "")).lower(): account
            for account in self.source.get("accounts", [])
            if account.get("name")
        }
        id_map: List[Dict[str, str]] = []

        def add_id(
            kind,
            source_name,
            source_id,
            destination_name,
            destination_id,
        ) -> None:
            id_map.append(
                {
                    "kind": kind,
                    "source": source_name,
                    "source_id": str(source_id or ""),
                    "destination": destination_name,
                    "destination_id": str(destination_id or ""),
                }
            )

        for pair in self.account_pairs:
            src = source_accounts_by_name.get(pair["source"].lower(), {})
            dst = self.dest_accounts.get(pair["destination"].lower(), {})
            add_id(
                "account",
                pair["source"],
                src.get("id"),
                pair["destination"],
                dst.get("id"),
            )
        for item in self.source.get("domains", []):
            name = str(item.get("name", ""))
            if not name:
                continue
            if (
                self.selected_domain_names is not None
                and name.lower() not in self.selected_domain_names
            ):
                continue
            target = self.mapper.map_domain(name)
            dst = self.dest_domains.get(target.lower(), {})
            add_id("domain", name, item.get("id"), target, dst.get("id"))
        for item in self.source.get("cos", []):
            name = str(item.get("name", ""))
            if not name:
                continue
            if (
                self.selected_cos is not None
                and name.lower() not in self.selected_cos
            ):
                continue
            target = name
            dst = self.dest_cos.get(target.lower(), {})
            add_id("cos", name, item.get("id"), target, dst.get("id"))
        for item in self.source.get("distribution_lists", []):
            name = str(item.get("name", ""))
            if not name:
                continue
            if (
                self.selected_dls is not None
                and name.lower() not in self.selected_dls
            ):
                continue
            target = self.mapper.map_email(name)
            dst = self.dest_dls.get(target.lower(), {})
            add_id(
                "distribution_list",
                name,
                item.get("id"),
                target,
                dst.get("id"),
            )
        for item in self.source.get("resources", []):
            name = str(item.get("name", ""))
            if not name:
                continue
            if (
                self.selected_resources is not None
                and name.lower() not in self.selected_resources
            ):
                continue
            target = self.mapper.map_email(name)
            dst = self.dest_resources.get(target.lower(), {})
            add_id("resource", name, item.get("id"), target, dst.get("id"))
        return id_map

    def build(self) -> Plan:
        self.plan_domains()
        self.plan_cos()
        self.plan_accounts()
        self.plan_distribution_lists()
        self.plan_resources()
        self.collect_warnings()
        self.finalize()

        stored = stored_options(self.base_config, self.sync)
        stored["content_types"] = list(
            self.sync.get("content_types") or PLAN_CONTENT_TYPES
        )
        stored["copy_mailbox"] = bool(self.include_mailbox)

        active_profile = self.base_config.current_profile()
        plan = {
            "version": 2,
            "planner_version": PLANNER_VERSION,
            "category": self.category,
            "profile": active_profile,
            "profile_name": self.base_config.profile_name(active_profile),
            "created_at": now_iso(),
            "source": {
                "host": self.source.get("host", ""),
                "created_at": self.source.get("created_at", ""),
                "counts": self.source.get("counts", {}),
                "path": self.source.get("path", ""),
            },
            "destination": {
                "host": self.destination.get("host", ""),
                "created_at": self.destination.get("created_at", ""),
                "counts": self.destination.get("counts", {}),
                "path": self.destination.get("path", ""),
            },
            "validation": self.validation,
            "summary": self.summary,
            "total_actions": len(self.actions),
            "actions": self.actions,
            "accounts": self.account_pairs,
            "mailboxes": self.mailboxes,
            "mailbox_count": len(self.mailboxes),
            "warnings": self.warnings + self.validation["warnings"],
            "info": self.info,
            "mapping": {
                "domains": self.sync.get("domain_map", {}),
                "hosts": self.sync.get("mail_host_map", {}),
                "default_mail_host": self.sync.get("default_mail_host", ""),
            },
            "options": stored,
            "selection": self.picked,
            "id_map": self.build_id_map(),
        }
        return cast(Plan, plan)


def generate_plan(
    config: config_module.Config,
    source: Dict[str, Any],
    destination: Dict[str, Any],
    include_mailbox: Optional[bool] = None,
    options: Optional[PlanOptions] = None,
    selection: Optional[Dict[str, Any]] = None,
    category: str = "global",
) -> Plan:
    """Build a migration plan without touching either server."""
    return PlanBuilder(
        config,
        source,
        destination,
        include_mailbox=include_mailbox,
        options=options,
        selection=selection,
        category=category,
    ).build()


def stored_options(
    config: config_module.Config, options: Dict[str, Any]
) -> Dict[str, Any]:
    """Prepare options for JSON persistence (secret stays encrypted)."""
    stored = dict(options)
    fixed = stored.pop("fixed_password", "") or ""
    encrypted = stored.pop("fixed_password_encrypted", "") or ""
    if fixed:
        encrypted = config.encrypt_secret(fixed)
    stored["fixed_password"] = bool(fixed or encrypted)
    stored["fixed_password_encrypted"] = encrypted
    return stored


