"""Compare a source snapshot with a destination snapshot and build a plan.

The plan only ever *creates* or *updates* destination objects.  Extra objects on
the destination are reported as "conservés" and never removed.
"""

from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import config as config_module
from . import mapping

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


def action_sort_key(item: Any) -> Tuple[int, str]:
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
    return normalize(left) == normalize(right)


def filtered_attrs(
    config: Any,
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
    config: Any,
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


def map_member(config: Any, member: str, known_domains: set) -> str:
    """Map internal members, leave external addresses untouched."""
    if not member or "@" not in member:
        return member
    domain = member.rsplit("@", 1)[1].lower()
    if domain in known_domains:
        return config.map_email(member)
    return member


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
    "directly_clean": False,
    "resume": False,
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
        return mapping.map_domain(self.options.get("domain_map"), domain)

    def map_email(self, email: str) -> str:
        return mapping.map_email(self.options.get("domain_map"), email)

    def map_host(self, host: str) -> str:
        return mapping.map_host(
            self.options.get("mail_host_map"),
            self.options.get("default_mail_host") or "",
            host,
        )

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
        return mapping.matches(name, patterns)

    def ignored_pref(self, name: str) -> bool:
        patterns = (
            self.options.get("provisioning_pref_ignore")
            or self.options.get("pref_ignore")
            or config_module.DEFAULT_PREF_IGNORE
        )
        return mapping.matches(name, patterns)


def _as_option_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off"}
    return bool(value)


def normalize_options(
    config: Any, overrides: Optional[Any] = None
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


def __getattr__(name):
    if name in {"PlanBuilder", "generate_plan"}:
        from . import planner

        return getattr(planner, name)
    raise AttributeError("module %r has no attribute %r" % (__name__, name))
