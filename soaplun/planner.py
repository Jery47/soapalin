"""Build migration plans from source and destination snapshots."""

from typing import Any, Dict, List, Optional, cast

from . import config as config_module
from .diff import (
    ACCOUNT_HANDLED,
    DOMAIN_HANDLED,
    PLAN_CATEGORY_GROUPS,
    PLAN_CONTENT_TYPES,
    PLANNER_VERSION,
    RESOURCE_HANDLED,
    PlanMapper,
    action,
    action_sort_key,
    attr_maps_equal,
    changed_attrs,
    dl_attrs,
    filtered_attrs,
    map_account_address_attrs,
    map_member,
    normalize_options,
    normalize_selection,
    resource_attrs,
    validate_snapshots,
)
from .inventory import now_iso
from .models import Plan, PlanOptions


class PlanBuilder:
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
    def plan_domains(self) -> None:
        self.target_domains = sorted(
            {
                self.mapper.map_domain(name)
                for name in self.source_domains
                if name
                and (
                    self.selected_domain_names is None
                    or name in self.selected_domain_names
                )
            }
        )
        for target in self.target_domains:
            if target.lower() in self.dest_domains:
                continue
            origins = [
                name
                for name in self.source_domains
                if self.mapper.map_domain(name) == target
            ]
            origin = self.source_domains[origins[0]]
            attrs = filtered_attrs(
                self.mapper,
                origin.get("attrs", {}),
                kind="domain",
                handled=DOMAIN_HANDLED,
            )
            domain_type = str(
                origin.get("attrs", {}).get("zimbraDomainType", "")
            ).lower()
            alias_target = _alias_target_name(origin, self.source_domains)
            if domain_type == "alias" or alias_target:
                if not alias_target:
                    self.warnings.append(
                        "Domaine alias %s sans cible identifiable : ignoré."
                        % origin["name"]
                    )
                    continue
                target_mapped = self.mapper.map_domain(alias_target)
                known_dest = target_mapped.lower() in self.dest_domains
                known_plan = target_mapped.lower() in {
                    d.lower() for d in self.target_domains
                }
                if not known_dest and not known_plan:
                    self.warnings.append(
                        "Domaine alias %s ignoré : la cible %s n'existe pas sur la "
                        "destination." % (origin["name"], target_mapped)
                    )
                    continue
                self.actions.append(
                    action(
                        "create_domain_alias",
                        target,
                        "Créer le domaine alias %s → %s"
                        % (target, target_mapped),
                        {
                            "name": target,
                            "alias_target": target_mapped,
                            "source_name": origin["name"],
                        },
                    )
                )
                continue
            self.actions.append(
                action(
                    "create_domain",
                    target,
                    "Créer le domaine %s" % target,
                    {
                        "name": target,
                        "attrs": attrs,
                        "source_name": origin["name"],
                    },
                )
            )

    # -- COS -----------------------------------------------------------
    def plan_cos(self) -> None:
        for source_name, cos in sorted(self.source_cos.items()):
            if (
                self.selected_cos is not None
                and source_name.lower() not in self.selected_cos
            ):
                continue
            target_name = cos["name"]
            if not target_name:
                continue
            if target_name.lower() not in self.dest_cos:
                attrs = filtered_attrs(
                    self.mapper,
                    cos.get("attrs", {}),
                    kind="cos",
                    handled={"cn"},
                )
                self.actions.append(
                    action(
                        "create_cos",
                        target_name,
                        "Créer la classe de service %s" % target_name,
                        {
                            "name": target_name,
                            "attrs": attrs,
                            "source_name": cos["name"],
                        },
                    )
                )
            elif target_name.lower() != "default":
                attrs = changed_attrs(
                    self.mapper,
                    cos.get("attrs", {}),
                    self.dest_cos[target_name.lower()].get("attrs", {}),
                    kind="cos",
                    handled={"cn"},
                )
                if attrs:
                    self.actions.append(
                        action(
                            "modify_cos",
                            target_name,
                            "Mettre à jour la classe de service %s"
                            % target_name,
                            {"name": target_name, "attrs": attrs},
                            risk="high",
                        )
                    )
            elif source_name != "default" and target_name.lower() == "default":
                self.warnings.append(
                    "La COS %s est mappée sur « default » : la COS destination "
                    "n'est pas modifiée par sécurité." % cos["name"]
                )

    # -- Accounts ------------------------------------------------------
    def plan_accounts(self) -> None:
        for account in sorted(
            self.source.get("accounts", []),
            key=lambda a: a.get("name", "").lower(),
        ):
            source_name = account.get("name", "")
            if not source_name:
                continue
            if account.get("is_system"):
                continue
            if (
                self.selected_emails
                and source_name.lower() not in self.selected_emails
            ):
                continue
            if not self.selected_emails and not self.picked.get("all_accounts"):
                continue
            target_name = self.mapper.map_email(source_name)
            key = target_name.lower()
            if key in self.planned_emails:
                self.warnings.append(
                    "Collision de mapping : %s et %s pointent tous deux vers %s ; "
                    "le second est ignoré."
                    % (self.planned_emails[key], source_name, target_name)
                )
                continue
            self.planned_emails[key] = source_name

            cos_name = account.get("cos_name") or ""
            host = self.target_host(account.get("mail_host") or "")
            alias_map = {}
            for alias in account.get("aliases", []):
                mapped_alias = self.mapper.map_email(alias)
                if mapped_alias:
                    alias_map[mapped_alias.lower()] = mapped_alias
            alias_list = sorted(
                alias
                for alias in alias_map.values()
                if alias.lower() != target_name.lower()
            )
            usable_aliases = []
            for alias in alias_list:
                owner = self.dest_alias_owner.get(alias.lower())
                if owner and owner.lower() != key:
                    self.warnings.append(
                        "Alias %s ignoré : déjà utilisé par le compte %s sur la "
                        "destination." % (alias, owner)
                    )
                    continue
                usable_aliases.append(alias)
            alias_list = usable_aliases

            existing = self.dest_accounts.get(key)
            if existing is None and key in self.dest_alias_owner:
                owner = self.dest_alias_owner[key]
                self.warnings.append(
                    "Compte %s non créé : l'adresse cible existe déjà comme alias du "
                    "compte %s sur la destination. Supprimez/renommez cet alias ou "
                    "changez le mapping de domaine." % (target_name, owner)
                )
                self.account_pairs.append(
                    {
                        "source": source_name,
                        "destination": target_name,
                        "status": "conflict",
                        "note": "Adresse cible déjà utilisée comme alias par %s"
                        % owner,
                    }
                )
                continue
            if existing is None:
                self._plan_new_account(
                    account,
                    source_name,
                    target_name,
                    key,
                    cos_name,
                    host,
                    alias_list,
                )
            else:
                self._plan_existing_account(
                    account,
                    source_name,
                    target_name,
                    cos_name,
                    host,
                    existing,
                    alias_list,
                )

            if self.include_mailbox:
                mailbox = account.get("mailbox") or {}
                self.mailboxes.append(
                    {
                        "source": source_name,
                        "destination": target_name,
                        "size": mailbox.get("size"),
                        "messages": mailbox.get("messages"),
                        "destination_status": "new"
                        if existing is None
                        else "existing",
                        "note": ""
                        if mailbox
                        else "Taille inconnue (boîte peut-être jamais ouverte)",
                    }
                )

            self.account_pairs.append(
                {
                    "source": source_name,
                    "destination": target_name,
                    "status": "new" if existing is None else "existing",
                }
            )

    def _plan_new_account(
        self,
        account,
        source_name,
        target_name,
        key,
        cos_name,
        host,
        alias_list,
    ) -> None:
        attrs = filtered_attrs(
            self.mapper,
            account.get("attrs", {}),
            kind="account",
            handled=ACCOUNT_HANDLED,
        )
        attrs, delegated_sender = map_account_address_attrs(
            attrs, self.mapper, self.source_domains
        )
        if host:
            attrs["zimbraMailHost"] = host
        if self.sync.get("force_password_change"):
            attrs["zimbraPasswordMustChange"] = "TRUE"
        payload = {
            "name": target_name,
            "attrs": attrs,
            "cos_name": cos_name,
            "source_name": source_name,
            "password_source": self.password_source,
            "aliases": alias_list,
            "mail_host": host,
            "delegated_sender_addresses": delegated_sender,
        }
        if not self.fixed_password and not self.random_password:
            self.warnings.append(
                "Aucun mot de passe fixe configuré : le compte %s sera créé "
                "sans mot de passe utilisable." % target_name
            )
        self.actions.append(
            action(
                "create_account",
                target_name,
                "Créer le compte %s" % target_name,
                payload,
                risk="normal"
                if (self.fixed_password or self.random_password)
                else "high",
            )
        )
        if cos_name:
            self.actions.append(
                action(
                    "modify_account",
                    target_name,
                    "Appliquer la COS %s a %s" % (cos_name, target_name),
                    {
                        "name": target_name,
                        "attrs": {},
                        "cos_name": cos_name,
                        "source_name": source_name,
                        "after_create": True,
                        "changes": ["COS -> %s" % cos_name],
                    },
                    depends_on=["create_account:%s" % key],
                )
            )
        alias_action_ids = []
        for alias in alias_list:
            alias_action_ids.append("add_alias:%s" % alias.lower())
            self.actions.append(
                action(
                    "add_alias",
                    alias,
                    "Ajouter l'alias %s → %s" % (alias, target_name),
                    {"account": target_name, "alias": alias},
                    depends_on=["create_account:%s" % key],
                )
            )
        if delegated_sender:
            self.actions.append(
                action(
                    "set_delegated_sender",
                    target_name,
                    "Restaurer les adresses d'envoi délégué de %s"
                    % target_name,
                    {"name": target_name, "values": delegated_sender},
                    depends_on=["create_account:%s" % key] + alias_action_ids,
                )
            )

    def _plan_existing_account(
        self,
        account,
        source_name,
        target_name,
        cos_name,
        host,
        existing,
        alias_list,
    ) -> None:
        attrs = changed_attrs(
            self.mapper,
            account.get("attrs", {}),
            existing.get("attrs", {}),
            kind="account",
            handled=ACCOUNT_HANDLED,
        )
        attrs, delegated_sender = map_account_address_attrs(
            attrs, self.mapper, self.source_domains
        )
        if host and not attr_maps_equal(host, existing.get("mail_host") or ""):
            attrs["zimbraMailHost"] = host
        existing_cos = (existing.get("cos_name") or "").lower()
        different_cos = bool(cos_name) and existing_cos != cos_name.lower()
        if attrs or different_cos:
            self.actions.append(
                action(
                    "modify_account",
                    target_name,
                    "Mettre à jour le compte %s" % target_name,
                    {
                        "name": target_name,
                        "id": existing.get("id", ""),
                        "attrs": attrs,
                        "cos_name": cos_name if different_cos else "",
                        "source_name": source_name,
                        "changes": sorted(attrs.keys())
                        + (["COS → %s" % cos_name] if different_cos else []),
                    },
                )
            )
        if self.reset_existing and self.fixed_password:
            self.actions.append(
                action(
                    "set_password",
                    target_name,
                    "Réinitialiser le mot de passe de %s" % target_name,
                    {
                        "name": target_name,
                        "id": existing.get("id", ""),
                        "password_source": "fixed",
                        "must_change": bool(
                            self.sync.get("force_password_change")
                        ),
                    },
                    risk="high",
                )
            )
        existing_aliases = {
            str(alias).lower() for alias in existing.get("aliases", [])
        }
        alias_action_ids = []
        for alias in alias_list:
            if alias.lower() not in existing_aliases:
                alias_action_ids.append("add_alias:%s" % alias.lower())
                self.actions.append(
                    action(
                        "add_alias",
                        alias,
                        "Ajouter l'alias %s → %s" % (alias, target_name),
                        {"account": target_name, "alias": alias},
                    )
                )
        if delegated_sender:
            self.actions.append(
                action(
                    "set_delegated_sender",
                    target_name,
                    "Restaurer les adresses d'envoi délégué de %s"
                    % target_name,
                    {"name": target_name, "values": delegated_sender},
                    depends_on=alias_action_ids,
                )
            )

    # -- Distribution lists -------------------------------------------
    def plan_distribution_lists(self) -> None:
        known_source_domains = set(self.source_domains.keys())
        for dl in sorted(
            self.source.get("distribution_lists", []),
            key=lambda d: d.get("name", ""),
        ):
            source_name = dl.get("name", "")
            if (
                self.selected_dls is not None
                and source_name.lower() not in self.selected_dls
            ):
                continue
            if not source_name:
                continue
            target_name = self.mapper.map_email(source_name)
            members = sorted(
                {
                    map_member(self.mapper, member, known_source_domains)
                    for member in dl.get("members", [])
                    if member
                }
            )
            existing = self.dest_dls.get(target_name.lower())
            if existing is None:
                attrs = dl_attrs(
                    self.mapper,
                    dl.get("attrs", {}),
                    dynamic=bool(dl.get("is_dynamic")),
                )
                self.actions.append(
                    action(
                        "create_dl",
                        target_name,
                        "Créer la liste de distribution %s" % target_name,
                        {
                            "name": target_name,
                            "attrs": attrs,
                            "members": members,
                            "source_name": source_name,
                            "dynamic": bool(dl.get("is_dynamic")),
                        },
                    )
                )
            else:
                attrs = changed_attrs(
                    self.mapper,
                    dl_attrs(
                        self.mapper,
                        dl.get("attrs", {}),
                        dynamic=bool(dl.get("is_dynamic")),
                    ),
                    existing.get("attrs", {}),
                    kind="dl",
                    handled=set(),
                )
                if attrs:
                    self.actions.append(
                        action(
                            "modify_dl",
                            target_name,
                            "Mettre à jour la liste %s" % target_name,
                            {
                                "name": target_name,
                                "attrs": attrs,
                                "source_name": source_name,
                            },
                        )
                    )
                missing = sorted(
                    set(members)
                    - {
                        str(m).lower()
                        for m in existing.get("members", [])
                    }
                )
                if missing:
                    self.actions.append(
                        action(
                            "add_dl_member",
                            target_name,
                            "Ajouter %d membre(s) à %s"
                            % (len(missing), target_name),
                            {"name": target_name, "members": missing},
                        )
                    )

    # -- Resources -----------------------------------------------------
    def plan_resources(self) -> None:
        for resource in sorted(
            self.source.get("resources", []),
            key=lambda r: r.get("name", ""),
        ):
            source_name = resource.get("name", "")
            if (
                self.selected_resources is not None
                and source_name.lower() not in self.selected_resources
            ):
                continue
            if not source_name:
                continue
            target_name = self.mapper.map_email(source_name)
            attrs = resource_attrs(self.mapper, resource.get("attrs", {}))
            existing = self.dest_resources.get(target_name.lower())
            if existing is None:
                self.actions.append(
                    action(
                        "create_resource",
                        target_name,
                        "Créer la ressource %s" % target_name,
                        {
                            "name": target_name,
                            "attrs": attrs,
                            "source_name": source_name,
                            "password_source": self.password_source,
                        },
                    )
                )
            else:
                changes = changed_attrs(
                    self.mapper,
                    resource_attrs(self.mapper, resource.get("attrs", {})),
                    existing.get("attrs", {}),
                    kind="resource",
                    handled=RESOURCE_HANDLED,
                )
                if changes:
                    self.actions.append(
                        action(
                            "modify_resource",
                            target_name,
                            "Mettre à jour la ressource %s" % target_name,
                            {
                                "name": target_name,
                                "attrs": changes,
                                "source_name": source_name,
                            },
                        )
                    )

    # -- Warnings ------------------------------------------------------
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


def _alias_target_name(
    domain: Dict[str, Any], source_domains: Dict[str, Dict[str, Any]]
) -> str:
    attrs = domain.get("attrs", {}) or {}
    catch_all = str(attrs.get("zimbraMailCatchAllForwardingAddress", "") or "")
    if catch_all.startswith("@"):
        return catch_all[1:]
    target_id = str(attrs.get("zimbraDomainAliasTargetId", "") or "")
    if target_id:
        for item in source_domains.values():
            if item.get("id") == target_id:
                return item.get("name", "")
    return ""


# ----------------------------------------------------------------------
# Plan persistence
# ----------------------------------------------------------------------
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


