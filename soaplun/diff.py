"""Compare a source snapshot with a destination snapshot and build a plan.

The plan only ever *creates* or *updates* destination objects.  Extra objects on
the destination are reported as "conservés" and never removed.
"""

import fnmatch
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import config as config_module
from .inventory import now_iso

# Attributes handled explicitly by the planner.
ACCOUNT_HANDLED = {
    "zimbraMailAlias",
    "zimbraMailHost",
    "zimbraCOSId",
    "zimbraPasswordMustChange",
}
RESOURCE_HANDLED = ACCOUNT_HANDLED | {"uid"}
RESOURCE_EXTRA_ATTRS = {
    "displayName",
    "description",
    "cn",
    "sn",
    "givenName",
    "l",
    "street",
    "postalCode",
    "zimbraAccountCalendarUserType",
    "zimbraCalendarResourceDoubleBookingAllowed",
}


def resource_attrs(config, attrs: Dict[str, Any]) -> Dict[str, Any]:
    filtered = filtered_attrs(
        config, attrs or {}, kind="resource", handled=RESOURCE_HANDLED
    )
    return {
        name: value
        for name, value in filtered.items()
        if name.startswith("zimbraCalRes")
        or name.startswith("zimbraCalendarResource")
        or name in RESOURCE_EXTRA_ATTRS
    }
DOMAIN_HANDLED = {
    "zimbraDomainType",
    "zimbraDomainAliasTargetId",
    "zimbraMailCatchAllForwardingAddress",
    "zimbraDomainName",
}


# ----------------------------------------------------------------------
# Action helpers
# ----------------------------------------------------------------------
DL_EXTRA_ATTRS = {
    "displayName",
    "description",
    "cn",
    "zimbraDLType",
    "zimbraMailStatus",
    "zimbraIsACLGroup",
    "zimbraHideInGal",
    "zimbraNotes",
}


def dl_attrs(config, attrs: Dict[str, Any], dynamic: bool = False) -> Dict[str, Any]:
    filtered = filtered_attrs(
        config, attrs or {}, kind="dl", handled={"zimbraMailAlias"}
    )
    return {
        name: value
        for name, value in filtered.items()
        if name.startswith("zimbraDistributionList")
        or name in DL_EXTRA_ATTRS
        or (dynamic and name == "memberURL")
    }


def action(
    kind: str,
    target: str,
    description: str,
    payload: Optional[Dict[str, Any]] = None,
    risk: str = "normal",
    depends_on: Optional[List[str]] = None,
) -> Dict[str, Any]:
    identifier = "%s:%s" % (kind, target.lower())
    return {
        "id": identifier,
        "kind": kind,
        "target": target,
        "description": description,
        "payload": payload or {},
        "risk": risk,
        "depends_on": depends_on or [],
    }


def action_sort_key(item: Dict[str, Any]) -> Tuple[int, str]:
    order = {
        "create_domain": 10,
        "create_domain_alias": 11,
        "modify_domain": 12,
        "create_cos": 20,
        "modify_cos": 21,
        "create_account": 30,
        "modify_account": 31,
        "set_password": 32,
        "add_alias": 33,
        "set_delegated_sender": 34,
        "create_dl": 40,
        "modify_dl": 41,
        "add_dl_member": 42,
        "create_resource": 50,
        "modify_resource": 51,
    }
    return (order.get(item.get("kind", ""), 99), item.get("target", "").lower())


# ----------------------------------------------------------------------
# Value normalization
# ----------------------------------------------------------------------
def normalize(value: Any) -> Any:
    """Normalize a Zimbra attribute value for comparison purposes."""
    if isinstance(value, list):
        return sorted(str(normalize(item)) for item in value)
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    text = str(value).strip()
    if text.upper() in {"TRUE", "FALSE"}:
        return text.upper()
    if text in {"1", "0"}:
        return text
    return text


def attr_maps_equal(left: Any, right: Any) -> bool:
    if isinstance(left, list) or isinstance(right, list):
        return normalize(left) == normalize(right)
    return normalize(left) == normalize(right)


def filtered_attrs(
    config: config_module.Config,
    attrs: Dict[str, Any],
    kind: str = "account",
    handled: Iterable[str] = (),
) -> Dict[str, Any]:
    """Keep only simple, copyable attributes (never empty values)."""
    handled = set(handled)
    result: Dict[str, Any] = {}
    for name, value in (attrs or {}).items():
        if name in handled:
            continue
        if config.ignored_attr(name, kind):
            continue
        if value is None:
            continue
        if isinstance(value, str) and value.strip() == "":
            continue
        result[name] = value
    return result


def changed_attrs(
    config: config_module.Config,
    source: Dict[str, Any],
    destination: Dict[str, Any],
    kind: str = "account",
    handled: Iterable[str] = (),
) -> Dict[str, Any]:
    """Attributes present in source, different (or absent) in destination."""
    wanted = filtered_attrs(config, source, kind, handled)
    changes: Dict[str, Any] = {}
    for name, value in wanted.items():
        if name not in destination or not attr_maps_equal(value, destination[name]):
            changes[name] = value
    return changes


# ----------------------------------------------------------------------
# Mapping
# ----------------------------------------------------------------------
def map_email(config: config_module.Config, email: str) -> str:
    return config.map_email(email or "")


def map_member(config: config_module.Config, member: str, known_domains: set) -> str:
    """Map internal members, leave external addresses untouched."""
    if not member or "@" not in member:
        return member
    domain = member.rsplit("@", 1)[1].lower()
    if domain in known_domains:
        return config.map_email(member)
    return member


def map_host(config: config_module.Config, host: str, destination_servers: List[str]) -> str:
    mapped = config.map_host(host or "")
    if mapped and destination_servers and mapped not in destination_servers:
        # Still return the mapping: the destination may use a different host
        # name that GetServer did not list (single-server setups, VIPs...).
        return mapped
    return mapped


ACCOUNT_ADDRESS_ATTRS = {
    "zimbraPrefFromAddress",
    "zimbraPrefReplyToAddress",
    "zimbraPrefCalendarReminderEmail",
    "zimbraAllowFromAddress",
}
DELEGATED_SENDER_ATTR = "zimbraPrefAllowAddressForDelegatedSender"


def _map_address_value(mapper, value, source_domains):
    if not isinstance(value, str) or "@" not in value:
        return value
    domain = value.rsplit("@", 1)[1].lower()
    if domain in source_domains:
        return mapper.map_email(value)
    return value


def map_account_address_attrs(attrs, mapper, source_domains):
    """Map email values, and defer the delegated-sender attribute.

    Zimbra's AllowAddressForDelegatedSender callback rejects values that are not
    the account's primary address or one of its aliases.  At account creation
    only the primary address is known, so that attribute must be applied after
    the aliases have been added.
    """
    delegated = attrs.pop(DELEGATED_SENDER_ATTR, None)
    for name in ACCOUNT_ADDRESS_ATTRS:
        if name not in attrs:
            continue
        value = attrs[name]
        if isinstance(value, list):
            attrs[name] = [
                _map_address_value(mapper, item, source_domains) for item in value
            ]
        else:
            attrs[name] = _map_address_value(mapper, value, source_domains)
    if isinstance(delegated, list):
        values = delegated
    elif delegated:
        values = [delegated]
    else:
        values = []
    mapped = [_map_address_value(mapper, item, source_domains) for item in values]
    return attrs, [item for item in mapped if item]


# ----------------------------------------------------------------------
# Plan options and selection
# ----------------------------------------------------------------------
PLANNER_VERSION = 3

PLAN_CATEGORIES = ("account", "configuration", "distribution_lists", "resources")

PLAN_CATEGORY_GROUPS = {
    "account": {"accounts"},
    "configuration": {"domains", "cos"},
    "distribution_lists": {"distribution_lists"},
    "resources": {"resources"},
}

PLAN_CONTENT_TYPES = ("mail", "calendar", "contacts", "tasks", "briefcase")

PLAN_OPTION_DEFAULTS: Dict[str, Any] = {
    "domain_map": {},
    "mail_host_map": {},
    "default_mail_host": "",
    "fixed_password": "",
    "fixed_password_encrypted": "",
    "random_password": False,
    "force_password_change": True,
    "reset_existing_passwords": False,
    "include_admin_accounts": True,
    "include_system_accounts": False,
    "keep_admin_permissions": False,
    "copy_mailbox": True,
    "copy_settings": True,
    "copy_prefs": True,
    "copy_folders": True,
    "copy_shares": True,
    "copy_permissions": True,
    "copy_filters": True,
    "copy_whitelists": True,
    "attr_ignore": list(config_module.DEFAULT_ATTR_IGNORE),
    "pref_ignore": list(config_module.DEFAULT_PREF_IGNORE),
    "provisioning_attr_ignore": list(config_module.DEFAULT_ATTR_IGNORE),
    "provisioning_pref_ignore": list(config_module.DEFAULT_PREF_IGNORE),
    "settings_attr_ignore": list(config_module.DEFAULT_ATTR_IGNORE),
    "settings_pref_ignore": list(config_module.DEFAULT_PREF_IGNORE),
    "mailbox_keep_archives": False,
    "mailbox_keep_tgz_days": 0,
    "diff_keep_archives": False,
    "diff_keep_tgz_days": 0,
    "keep_tgz_days": 7,
    "mailbox_resolve": "skip",
    "skip_duplicates": True,
    "parallelism": 1,
    "max_accounts_per_plan": 1000,
    "content_types": list(PLAN_CONTENT_TYPES),
}


class PlanMapper:
    """Apply the mappings and ignore lists stored in a plan's options."""

    def __init__(self, config: config_module.Config, options: Dict[str, Any]):
        self.config = config
        self.options = options

    def map_domain(self, domain: str) -> str:
        mapping = {
            str(key).lower(): value
            for key, value in (self.options.get("domain_map") or {}).items()
        }
        return mapping.get(str(domain).lower(), domain) or domain

    def map_email(self, email: str) -> str:
        if not email or "@" not in email:
            return email
        local, domain = email.rsplit("@", 1)
        return "%s@%s" % (local, self.map_domain(domain))

    def map_host(self, host: str) -> str:
        mapping = {
            str(key).lower(): value
            for key, value in (self.options.get("mail_host_map") or {}).items()
        }
        default = self.options.get("default_mail_host") or ""
        return mapping.get(str(host).lower()) or default or host

    def ignored_attr(self, name: str, kind: str = "account") -> bool:
        if kind == "domain":
            patterns: Iterable[str] = config_module.DOMAIN_ATTR_IGNORE
        elif kind == "dl":
            patterns = config_module.DL_ATTR_IGNORE
        elif kind == "resource":
            patterns = config_module.RESOURCE_ATTR_IGNORE
        elif kind == "cos":
            patterns = config_module.COS_ATTR_IGNORE
        else:
            if name in config_module.ALWAYS_IGNORE_ATTRS:
                return True
            if name in {
                "zimbraIsAdminAccount",
                "zimbraIsDelegatedAdminAccount",
                "zimbraIsExternalVirtualAccount",
            }:
                return not bool(self.options.get("keep_admin_permissions", False))
            patterns = (
                self.options.get("provisioning_attr_ignore")
                or self.options.get("attr_ignore")
                or config_module.DEFAULT_ATTR_IGNORE
            )
        return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)

    def ignored_pref(self, name: str) -> bool:
        patterns = (
            self.options.get("provisioning_pref_ignore")
            or self.options.get("pref_ignore")
            or config_module.DEFAULT_PREF_IGNORE
        )
        return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def _as_option_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)


def normalize_options(
    config: config_module.Config, overrides: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Merge global defaults with plan-level overrides and decrypt secrets."""
    merged: Dict[str, Any] = dict(PLAN_OPTION_DEFAULTS)
    merged.update(dict(config.sync() or {}))
    merged.update(dict(overrides or {}))

    options = dict(merged)
    for key in ("domain_map", "mail_host_map"):
        options[key] = dict(merged.get(key) or {})
    legacy_attrs = set(config_module.LEGACY_DEFAULT_ATTR_IGNORE)
    for key in ("attr_ignore", "pref_ignore"):
        value = merged.get(key)
        if isinstance(value, str):
            value = [line.strip() for line in value.splitlines() if line.strip()]
        options[key] = list(value or PLAN_OPTION_DEFAULTS[key])
    for key in ("attr_ignore", "provisioning_attr_ignore", "settings_attr_ignore"):
        if key not in options:
            continue
        value = options.get(key)
        if isinstance(value, str):
            value = [line.strip() for line in value.splitlines() if line.strip()]
        if isinstance(value, list):
            value_set = set(str(item) for item in value)
            if value_set in (legacy_attrs, legacy_attrs - {"zimbraAuth*"}):
                options[key] = list(config_module.DEFAULT_ATTR_IGNORE)
    for key, legacy in (
        ("provisioning_attr_ignore", "attr_ignore"),
        ("provisioning_pref_ignore", "pref_ignore"),
        ("settings_attr_ignore", "attr_ignore"),
        ("settings_pref_ignore", "pref_ignore"),
    ):
        value = merged.get(key)
        if value is None:
            value = options.get(legacy)
        if isinstance(value, str):
            value = [line.strip() for line in value.splitlines() if line.strip()]
        options[key] = list(value or PLAN_OPTION_DEFAULTS[legacy])
    for key in ("attr_ignore", "provisioning_attr_ignore", "settings_attr_ignore"):
        value = options.get(key)
        if isinstance(value, list):
            value_set = set(str(item) for item in value)
            if value_set in (legacy_attrs, legacy_attrs - {"zimbraAuth*"}):
                options[key] = list(config_module.DEFAULT_ATTR_IGNORE)
    for key in ("mailbox_keep_archives", "diff_keep_archives"):
        options[key] = _as_option_bool(options.get(key), False)
    for key in ("mailbox_keep_tgz_days", "diff_keep_tgz_days"):
        try:
            options[key] = max(0, int(options.get(key) or 0))
        except (TypeError, ValueError):
            options[key] = 0
    content = merged.get("content_types")
    if content is None:
        content = list(PLAN_CONTENT_TYPES)
    elif isinstance(content, str):
        content = [item.strip() for item in content.split(",") if item.strip()]
    content = [item for item in content if item in PLAN_CONTENT_TYPES]
    if "all" in content:
        content = list(PLAN_CONTENT_TYPES)
    options["content_types"] = content

    skip_duplicates = options.get("skip_duplicates", True)
    if isinstance(skip_duplicates, str):
        skip_duplicates = skip_duplicates.strip().lower() not in {
            "",
            "0",
            "false",
            "no",
            "off",
        }
    options["skip_duplicates"] = bool(skip_duplicates)

    try:
        parallelism = int(options.get("parallelism", 1) or 1)
    except (TypeError, ValueError):
        parallelism = 1
    options["parallelism"] = max(1, parallelism)

    if (
        not isinstance(options.get("fixed_password"), str)
        or not options.get("fixed_password")
    ) and options.get("fixed_password_encrypted"):
        options["fixed_password"] = config.decrypt_secret(
            options.get("fixed_password_encrypted") or ""
        )
    return options


def _selected_items(selection, key, legacy_key):
    if key in selection:
        raw = selection.get(key)
        if raw is None:
            return None
        if isinstance(raw, str):
            raw = [raw]
        return sorted(
            {str(value).strip().lower() for value in raw if str(value).strip()}
        )
    return None if selection.get(legacy_key, True) else []


def normalize_selection(
    selection: Optional[Dict[str, Any]], source: Dict[str, Any]
) -> Dict[str, Any]:
    selection = dict(selection or {})
    accounts = selection.get("accounts")
    if accounts is None:
        selected_accounts = None
    else:
        selected_accounts = sorted(
            {str(value).lower() for value in accounts if str(value).strip()}
        )
    domains = _selected_items(selection, "domains", "include_domains")
    cos = _selected_items(selection, "cos", "include_cos")
    dls = _selected_items(
        selection, "distribution_lists", "include_distribution_lists"
    )
    resources = _selected_items(selection, "resources", "include_resources")
    return {
        "accounts": selected_accounts,
        "all_accounts": selected_accounts is None,
        "domains": domains,
        "cos": cos,
        "distribution_lists": dls,
        "resources": resources,
        "include_domains": domains is None or bool(domains),
        "include_cos": cos is None or bool(cos),
        "include_distribution_lists": dls is None or bool(dls),
        "include_resources": resources is None or bool(resources),
    }


def validate_snapshots(
    config: config_module.Config,
    source: Dict[str, Any],
    destination: Dict[str, Any],
    options: Dict[str, Any],
) -> Dict[str, Any]:
    """Detect stale or same-server inventories.

    The result separates hard errors from informational warnings.
    """
    source_host = str((source or {}).get("host") or "")
    destination_host = str((destination or {}).get("host") or "")
    configured_source = config_module.store_host(config.connection("source"))
    configured_destination = config_module.store_host(config.connection("destination"))
    hard_errors: List[str] = []
    warnings: List[str] = []

    if not source_host:
        hard_errors.append("L'inventaire source n'indique aucun hote.")
    if not destination_host:
        hard_errors.append("L'inventaire destination n'indique aucun hote.")

    hosts_identical = (
        bool(source_host)
        and bool(destination_host)
        and source_host.lower() == destination_host.lower()
    )

    # Only enforce the destination-host consistency check when the two hosts are
    # different.  In same-host mode the destination snapshot was intentionally
    # produced from the source server.
    if (
        not hosts_identical
        and destination_host
        and configured_destination
        and destination_host.lower() != configured_destination.lower()
    ):
        hard_errors.append(
            "L'inventaire destination (%s) ne correspond pas à la destination "
            "configurée (%s). Relancez l'inventaire destination."
            % (destination_host, configured_destination)
        )

    if source_host and configured_source and source_host.lower() != configured_source.lower():
        warnings.append(
            "L'inventaire source (%s) ne correspond pas à la source configurée "
            "(%s)." % (source_host, configured_source)
        )

    errors = hard_errors
    return {
        "ok": not errors,
        "errors": errors,
        "hard_errors": hard_errors,
        "same_host_errors": [],
        "same_host": hosts_identical,
        "warnings": warnings,
        "source_host": source_host,
        "destination_host": destination_host,
        "configured_source_host": configured_source,
        "configured_destination_host": configured_destination,
    }

def plan_option_summary(plan: Dict[str, Any]) -> Dict[str, Any]:
    return dict(plan.get("options") or {})


def action_group(step: str, kind: str) -> bool:
    return step == "provisioning" and kind in PROVISIONING_KINDS


PROVISIONING_KINDS = {
    "create_domain",
    "create_domain_alias",
    "modify_domain",
    "create_cos",
    "modify_cos",
    "create_account",
    "modify_account",
    "set_password",
    "add_alias",
    "set_delegated_sender",
    "create_dl",
    "modify_dl",
    "add_dl_member",
    "create_resource",
    "modify_resource",
}


def selected_accounts(plan: Dict[str, Any]) -> List[str]:
    selection = plan.get("selection") or {}
    values = selection.get("accounts")
    if values is None:
        values = [
            pair.get("source", "")
            for pair in plan.get("accounts", [])
            if pair.get("source")
        ]
    return [str(value) for value in values if str(value).strip()]


# ----------------------------------------------------------------------
# Plan generation
# ----------------------------------------------------------------------
def generate_plan(
    config: config_module.Config,
    source: Dict[str, Any],
    destination: Dict[str, Any],
    include_mailbox: Optional[bool] = None,
    options: Optional[Dict[str, Any]] = None,
    selection: Optional[Dict[str, Any]] = None,
    category: str = "global",
) -> Dict[str, Any]:
    base_config = config
    sync = normalize_options(config, options)
    picked = normalize_selection(selection, source)
    allowed_groups = PLAN_CATEGORY_GROUPS.get(category)
    if allowed_groups is not None:
        for key in (
            "accounts",
            "domains",
            "cos",
            "distribution_lists",
            "resources",
        ):
            if key not in allowed_groups:
                picked[key] = []
        if "accounts" not in allowed_groups:
            picked["all_accounts"] = False
        picked["include_domains"] = (
            picked["domains"] is None or bool(picked["domains"])
        )
        picked["include_cos"] = picked["cos"] is None or bool(picked["cos"])
        picked["include_distribution_lists"] = (
            picked["distribution_lists"] is None
            or bool(picked["distribution_lists"])
        )
        picked["include_resources"] = (
            picked["resources"] is None or bool(picked["resources"])
        )
    validation = validate_snapshots(config, source, destination, sync)
    mapper = PlanMapper(config, sync)
    config = mapper  # mapping/ignore helpers below must use the plan options
    if include_mailbox is None:
        include_mailbox = bool(sync.get("copy_mailbox", True))

    source_domains = {
        item["name"].lower(): item for item in source.get("domains", [])
    }
    dest_domains = {
        item["name"].lower(): item for item in destination.get("domains", [])
    }
    source_cos = {item["name"].lower(): item for item in source.get("cos", [])}
    dest_cos = {item["name"].lower(): item for item in destination.get("cos", [])}
    dest_accounts = {
        account["name"].lower(): account
        for account in destination.get("accounts", [])
        if account.get("name")
    }
    dest_alias_owner = {}
    for account in destination.get("accounts", []):
        for alias in account.get("aliases", []) or []:
            if alias:
                dest_alias_owner[str(alias).lower()] = account.get("name", "")
    dest_dls = {
        item["name"].lower(): item
        for item in destination.get("distribution_lists", [])
        if item.get("name")
    }
    dest_resources = {
        item["name"].lower(): item
        for item in destination.get("resources", [])
        if item.get("name")
    }
    dest_servers = [item.get("name", "") for item in destination.get("servers", [])]
    dest_servers_lower = {name.lower() for name in dest_servers}
    host_map_keys = {str(key).lower() for key in (sync.get("mail_host_map") or {})}
    unknown_hosts: set = set()

    def target_host(source_host: str) -> str:
        if not source_host:
            return ""
        if source_host.lower() in host_map_keys or sync.get("default_mail_host"):
            return config.map_host(source_host)
        if source_host.lower() in dest_servers_lower:
            return source_host
        unknown_hosts.add(source_host)
        return ""

    actions: List[Dict[str, Any]] = []
    warnings: List[str] = []
    info: List[str] = []

    # -- Domains -------------------------------------------------------
    selected_domain_names = (
        set(picked["domains"]) if picked.get("domains") is not None else None
    )
    target_domains = sorted(
        {
            config.map_domain(name)
            for name in source_domains
            if name
            and (
                selected_domain_names is None
                or name in selected_domain_names
            )
        }
    )
    for target in target_domains:
        if target.lower() in dest_domains:
            continue
        # Find the source domain(s) mapping to this target.
        origins = [name for name in source_domains if config.map_domain(name) == target]
        origin = source_domains[origins[0]]
        attrs = filtered_attrs(
            config, origin.get("attrs", {}), kind="domain", handled=DOMAIN_HANDLED
        )
        domain_type = str(origin.get("attrs", {}).get("zimbraDomainType", "")).lower()
        alias_target = _alias_target_name(origin, source_domains)
        if domain_type == "alias" or alias_target:
            if not alias_target:
                warnings.append(
                    "Domaine alias %s sans cible identifiable : ignoré." % origin["name"]
                )
                continue
            target_mapped = config.map_domain(alias_target)
            known_dest = target_mapped.lower() in dest_domains
            known_plan = target_mapped.lower() in {
                d.lower() for d in target_domains
            }
            if not known_dest and not known_plan:
                warnings.append(
                    "Domaine alias %s ignoré : la cible %s n'existe pas sur la "
                    "destination." % (origin["name"], target_mapped)
                )
                continue
            actions.append(
                action(
                    "create_domain_alias",
                    target,
                    "Créer le domaine alias %s → %s" % (target, target_mapped),
                    {
                        "name": target,
                        "alias_target": target_mapped,
                        "source_name": origin["name"],
                    },
                )
            )
            continue
        actions.append(
            action(
                "create_domain",
                target,
                "Créer le domaine %s" % target,
                {"name": target, "attrs": attrs, "source_name": origin["name"]},
            )
        )

    # -- COS -----------------------------------------------------------
    selected_cos = (
        set(picked["cos"]) if picked.get("cos") is not None else None
    )
    for source_name, cos in sorted(source_cos.items()):
        if selected_cos is not None and source_name.lower() not in selected_cos:
            continue
        target_name = cos["name"]
        if not target_name:
            continue
        if target_name.lower() not in dest_cos:
            attrs = filtered_attrs(
                config, cos.get("attrs", {}), kind="cos", handled={"cn"}
            )
            actions.append(
                action(
                    "create_cos",
                    target_name,
                    "Créer la classe de service %s" % target_name,
                    {"name": target_name, "attrs": attrs, "source_name": cos["name"]},
                )
            )
        elif target_name.lower() != "default":
            attrs = changed_attrs(
                config,
                cos.get("attrs", {}),
                dest_cos[target_name.lower()].get("attrs", {}),
                kind="cos",
                handled={"cn"},
            )
            if attrs:
                actions.append(
                    action(
                        "modify_cos",
                        target_name,
                        "Mettre à jour la classe de service %s" % target_name,
                        {"name": target_name, "attrs": attrs},
                        risk="high",
                    )
                )
        elif source_name != "default" and target_name.lower() == "default":
            warnings.append(
                "La COS %s est mappée sur « default » : la COS destination "
                "n'est pas modifiée par sécurité." % cos["name"]
            )

    # -- Accounts ------------------------------------------------------
    planned_emails: Dict[str, str] = {}
    account_pairs: List[Dict[str, Any]] = []
    mailboxes: List[Dict[str, Any]] = []
    fixed_password = bool(sync.get("fixed_password"))
    random_password = bool(sync.get("random_password"))
    password_source = (
        "random" if random_password else ("fixed" if fixed_password else "source_unknown")
    )
    reset_existing = bool(sync.get("reset_existing_passwords"))
    selected_emails = {str(value).lower() for value in picked.get("accounts") or []}

    for account in sorted(source.get("accounts", []), key=lambda a: a.get("name", "").lower()):
        source_name = account.get("name", "")
        if not source_name:
            continue
        if account.get("is_system"):
            continue
        if selected_emails and source_name.lower() not in selected_emails:
            continue
        if not selected_emails and not picked.get("all_accounts"):
            continue
        target_name = map_email(config, source_name)
        key = target_name.lower()
        if key in planned_emails:
            warnings.append(
                "Collision de mapping : %s et %s pointent tous deux vers %s ; "
                "le second est ignoré."
                % (planned_emails[key], source_name, target_name)
            )
            continue
        planned_emails[key] = source_name

        cos_name = account.get("cos_name") or ""
        host = target_host(account.get("mail_host") or "")
        alias_map = {}
        for alias in account.get("aliases", []):
            mapped_alias = map_email(config, alias)
            if mapped_alias:
                alias_map[mapped_alias.lower()] = mapped_alias
        alias_list = sorted(
            alias
            for alias in alias_map.values()
            if alias.lower() != target_name.lower()
        )
        usable_aliases = []
        for alias in alias_list:
            owner = dest_alias_owner.get(alias.lower())
            if owner and owner.lower() != key:
                warnings.append(
                    "Alias %s ignoré : déjà utilisé par le compte %s sur la "
                    "destination." % (alias, owner)
                )
                continue
            usable_aliases.append(alias)
        alias_list = usable_aliases

        existing = dest_accounts.get(key)
        if existing is None and key in dest_alias_owner:
            owner = dest_alias_owner[key]
            warnings.append(
                "Compte %s non créé : l'adresse cible existe déjà comme alias du "
                "compte %s sur la destination. Supprimez/renommez cet alias ou "
                "changez le mapping de domaine." % (target_name, owner)
            )
            account_pairs.append(
                {
                    "source": source_name,
                    "destination": target_name,
                    "status": "conflict",
                    "note": "Adresse cible déjà utilisée comme alias par %s" % owner,
                }
            )
            continue
        if existing is None:
            attrs = filtered_attrs(
                config,
                account.get("attrs", {}),
                kind="account",
                handled=ACCOUNT_HANDLED,
            )
            attrs, delegated_sender = map_account_address_attrs(
                attrs, config, source_domains
            )
            if host:
                attrs["zimbraMailHost"] = host
            if sync.get("force_password_change"):
                attrs["zimbraPasswordMustChange"] = "TRUE"
            payload = {
                "name": target_name,
                "attrs": attrs,
                "cos_name": cos_name,
                "source_name": source_name,
                "password_source": password_source,
                "aliases": alias_list,
                "mail_host": host,
                "delegated_sender_addresses": delegated_sender,
            }
            if not fixed_password and not random_password:
                warnings.append(
                    "Aucun mot de passe fixe configuré : le compte %s sera créé "
                    "sans mot de passe utilisable." % target_name
                )
            actions.append(
                action(
                    "create_account",
                    target_name,
                    "Créer le compte %s" % target_name,
                    payload,
                    risk="normal" if (fixed_password or random_password) else "high",
                )
            )
            alias_action_ids = []
            for alias in alias_list:
                alias_action_ids.append("add_alias:%s" % alias.lower())
                actions.append(
                    action(
                        "add_alias",
                        alias,
                        "Ajouter l'alias %s → %s" % (alias, target_name),
                        {"account": target_name, "alias": alias},
                        depends_on=["create_account:%s" % key],
                    )
                )
            if delegated_sender:
                actions.append(
                    action(
                        "set_delegated_sender",
                        target_name,
                        "Restaurer les adresses d'envoi délégué de %s" % target_name,
                        {"name": target_name, "values": delegated_sender},
                        depends_on=["create_account:%s" % key] + alias_action_ids,
                    )
                )
        else:
            attrs = changed_attrs(
                config,
                account.get("attrs", {}),
                existing.get("attrs", {}),
                kind="account",
                handled=ACCOUNT_HANDLED,
            )
            attrs, delegated_sender = map_account_address_attrs(
                attrs, config, source_domains
            )
            if host and not attr_maps_equal(host, existing.get("mail_host") or ""):
                attrs["zimbraMailHost"] = host
            existing_cos = (existing.get("cos_name") or "").lower()
            different_cos = bool(cos_name) and existing_cos != cos_name.lower()
            if attrs or different_cos:
                actions.append(
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
            if reset_existing and fixed_password:
                actions.append(
                    action(
                        "set_password",
                        target_name,
                        "Réinitialiser le mot de passe de %s" % target_name,
                        {
                            "name": target_name,
                            "id": existing.get("id", ""),
                            "password_source": "fixed",
                            "must_change": bool(sync.get("force_password_change")),
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
                    actions.append(
                        action(
                            "add_alias",
                            alias,
                            "Ajouter l'alias %s → %s" % (alias, target_name),
                            {"account": target_name, "alias": alias},
                        )
                    )
            if delegated_sender:
                actions.append(
                    action(
                        "set_delegated_sender",
                        target_name,
                        "Restaurer les adresses d'envoi délégué de %s" % target_name,
                        {"name": target_name, "values": delegated_sender},
                        depends_on=alias_action_ids,
                    )
                )

        if include_mailbox:
            mailbox = account.get("mailbox") or {}
            mailboxes.append(
                {
                    "source": source_name,
                    "destination": target_name,
                    "size": mailbox.get("size"),
                    "messages": mailbox.get("messages"),
                    "destination_status": "new" if existing is None else "existing",
                    "note": ""
                    if mailbox
                    else "Taille inconnue (boîte peut-être jamais ouverte)",
                }
            )

        account_pairs.append(
            {
                "source": source_name,
                "destination": target_name,
                "status": "new" if existing is None else "existing",
            }
        )

    # -- Distribution lists -------------------------------------------
    known_source_domains = set(source_domains.keys())
    selected_dls = (
        set(picked["distribution_lists"])
        if picked.get("distribution_lists") is not None
        else None
    )
    for dl in sorted(source.get("distribution_lists", []), key=lambda d: d.get("name", "")):
        source_name = dl.get("name", "")
        if selected_dls is not None and source_name.lower() not in selected_dls:
            continue
        if not source_name:
            continue
        target_name = map_email(config, source_name)
        members = sorted(
            {
                map_member(config, member, known_source_domains)
                for member in dl.get("members", [])
                if member
            }
        )
        existing = dest_dls.get(target_name.lower())
        if existing is None:
            attrs = dl_attrs(
                config,
                dl.get("attrs", {}),
                dynamic=bool(dl.get("is_dynamic")),
            )
            actions.append(
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
                config,
                dl_attrs(
                    config,
                    dl.get("attrs", {}),
                    dynamic=bool(dl.get("is_dynamic")),
                ),
                existing.get("attrs", {}),
                kind="dl",
                handled=set(),
            )
            if attrs:
                actions.append(
                    action(
                        "modify_dl",
                        target_name,
                        "Mettre à jour la liste %s" % target_name,
                        {"name": target_name, "attrs": attrs, "source_name": source_name},
                    )
                )
            missing = sorted(
                set(members) - {str(m).lower() for m in existing.get("members", [])}
            )
            if missing:
                actions.append(
                    action(
                        "add_dl_member",
                        target_name,
                        "Ajouter %d membre(s) à %s" % (len(missing), target_name),
                        {"name": target_name, "members": missing},
                    )
                )

    # -- Resources -----------------------------------------------------
    selected_resources = (
        set(picked["resources"]) if picked.get("resources") is not None else None
    )
    for resource in sorted(source.get("resources", []), key=lambda r: r.get("name", "")):
        source_name = resource.get("name", "")
        if (
            selected_resources is not None
            and source_name.lower() not in selected_resources
        ):
            continue
        if not source_name:
            continue
        target_name = map_email(config, source_name)
        attrs = resource_attrs(config, resource.get("attrs", {}))
        existing = dest_resources.get(target_name.lower())
        if existing is None:
            actions.append(
                action(
                    "create_resource",
                    target_name,
                    "Créer la ressource %s" % target_name,
                    {
                        "name": target_name,
                        "attrs": attrs,
                        "source_name": source_name,
                        "password_source": password_source,
                    },
                )
            )
        else:
            changes = changed_attrs(
                config,
                resource_attrs(config, resource.get("attrs", {})),
                existing.get("attrs", {}),
                kind="resource",
                handled=RESOURCE_HANDLED,
            )
            if changes:
                actions.append(
                    action(
                        "modify_resource",
                        target_name,
                        "Mettre à jour la ressource %s" % target_name,
                        {"name": target_name, "attrs": changes, "source_name": source_name},
                    )
                )

    # -- Extras kept on destination -----------------------------------
    source_emails = {map_email(config, a.get("name", "")).lower() for a in source.get("accounts", [])}
    extras = sorted(
        name for name in dest_accounts if name not in source_emails
    )
    if extras and account_pairs:
        info.append(
            "%d compte(s) présent(s) uniquement sur la destination seront "
            "conservés (aucune suppression)." % len(extras)
        )
    source_dl_names = {
        map_email(config, d.get("name", "")).lower()
        for d in source.get("distribution_lists", [])
    }
    extra_dls = sorted(name for name in dest_dls if name not in source_dl_names)
    if extra_dls and (selected_dls is None or selected_dls):
        info.append(
            "%d liste(s) de distribution supplémentaire(s) seront conservées."
            % len(extra_dls)
        )

    # -- Warnings about unmapped domains ------------------------------
    for name in sorted(source_domains):
        if selected_domain_names is not None and name not in selected_domain_names:
            continue
        mapped = config.map_domain(name)
        if mapped.lower() not in dest_domains and mapped.lower() not in {
            d.lower() for d in target_domains
        }:
            warnings.append(
                "Le domaine %s n'existe pas sur la destination et ne sera pas "
                "créé : vérifiez le mapping." % mapped
            )

    for host in sorted(unknown_hosts):
        warnings.append(
            "L'hôte mail %s n'existe pas sur la destination et n'est pas mappé : "
            "les comptes seront créés sans zimbraMailHost explicite (Zimbra "
            "choisira l'hôte par défaut)." % host
        )

    limit = int(sync.get("max_accounts_per_plan") or 0)
    selected_account_count = (
        len(picked.get("accounts") or [])
        if picked.get("accounts") is not None
        else len(source.get("accounts", []))
    )
    if limit and selected_account_count > limit:
        warnings.append(
            "Le plan couvre %d comptes, au-delà de la limite configurée (%d). "
            "Appliquez-le par lots ou augmentez la limite."
            % (selected_account_count, limit)
        )

    if not picked["include_domains"] and account_pairs:
        warnings.append(
            "Les domaines ne sont pas inclus dans ce plan : les comptes ne "
            "pourront être créés que si leurs domaines existent déjà sur la "
            "destination."
        )
    if picked.get("accounts") is not None:
        wanted = {str(value).lower() for value in picked["accounts"]}
        known = {
            str(account.get("name", "")).lower()
            for account in source.get("accounts", [])
            if account.get("name")
        }
        missing = sorted(wanted - known)
        if missing:
            sample = ", ".join(missing[:10])
            if len(missing) > 10:
                sample += ", ..."
            warnings.append(
                "%d compte(s) saisi(s) introuvable(s) dans l'inventaire source : %s"
                % (len(missing), sample)
            )

    if not picked["include_cos"] and account_pairs:
        warnings.append(
            "Les COS ne sont pas incluses dans ce plan : vérifiez que les COS "
            "cibles existent déjà sur la destination."
        )

    actions.sort(key=action_sort_key)

    summary: Dict[str, int] = {}
    for item in actions:
        summary[item["kind"]] = summary.get(item["kind"], 0) + 1

    source_accounts_by_name = {
        str(account.get("name", "")).lower(): account
        for account in source.get("accounts", [])
        if account.get("name")
    }
    id_map: List[Dict[str, str]] = []

    def add_id(kind, source_name, source_id, destination_name, destination_id):
        id_map.append(
            {
                "kind": kind,
                "source": source_name,
                "source_id": str(source_id or ""),
                "destination": destination_name,
                "destination_id": str(destination_id or ""),
            }
        )

    for pair in account_pairs:
        src = source_accounts_by_name.get(pair["source"].lower(), {})
        dst = dest_accounts.get(pair["destination"].lower(), {})
        add_id(
            "account",
            pair["source"],
            src.get("id"),
            pair["destination"],
            dst.get("id"),
        )
    for item in source.get("domains", []):
        name = str(item.get("name", ""))
        if not name:
            continue
        if selected_domain_names is not None and name.lower() not in selected_domain_names:
            continue
        target = config.map_domain(name)
        dst = dest_domains.get(target.lower(), {})
        add_id("domain", name, item.get("id"), target, dst.get("id"))
    for item in source.get("cos", []):
        name = str(item.get("name", ""))
        if not name:
            continue
        if selected_cos is not None and name.lower() not in selected_cos:
            continue
        target = name
        dst = dest_cos.get(target.lower(), {})
        add_id("cos", name, item.get("id"), target, dst.get("id"))
    for item in source.get("distribution_lists", []):
        name = str(item.get("name", ""))
        if not name:
            continue
        if selected_dls is not None and name.lower() not in selected_dls:
            continue
        target = config.map_email(name)
        dst = dest_dls.get(target.lower(), {})
        add_id("distribution_list", name, item.get("id"), target, dst.get("id"))
    for item in source.get("resources", []):
        name = str(item.get("name", ""))
        if not name:
            continue
        if (
            selected_resources is not None
            and name.lower() not in selected_resources
        ):
            continue
        target = config.map_email(name)
        dst = dest_resources.get(target.lower(), {})
        add_id("resource", name, item.get("id"), target, dst.get("id"))

    stored_options_data = stored_options(base_config, sync)
    stored_options_data["content_types"] = list(
        sync.get("content_types") or PLAN_CONTENT_TYPES
    )
    stored_options_data["copy_mailbox"] = bool(include_mailbox)

    active_profile = base_config.current_profile()
    plan = {
        "version": 2,
        "planner_version": PLANNER_VERSION,
        "category": category,
        "profile": active_profile,
        "profile_name": base_config.profile_name(active_profile),
        "created_at": now_iso(),
        "source": {
            "host": source.get("host", ""),
            "created_at": source.get("created_at", ""),
            "counts": source.get("counts", {}),
            "path": source.get("path", ""),
        },
        "destination": {
            "host": destination.get("host", ""),
            "created_at": destination.get("created_at", ""),
            "counts": destination.get("counts", {}),
            "path": destination.get("path", ""),
        },
        "validation": validation,
        "summary": summary,
        "total_actions": len(actions),
        "actions": actions,
        "accounts": account_pairs,
        "mailboxes": mailboxes,
        "mailbox_count": len(mailboxes),
        "warnings": warnings + validation["warnings"],
        "info": info,
        "mapping": {
            "domains": sync.get("domain_map", {}),
            "hosts": sync.get("mail_host_map", {}),
                "default_mail_host": sync.get("default_mail_host", ""),
        },
        "options": stored_options_data,
        "selection": picked,
        "id_map": id_map,
    }
    return plan


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


