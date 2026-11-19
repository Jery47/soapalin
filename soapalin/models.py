"""Typed contracts for plans, options, snapshots and reports.

The runtime representation stays plain ``dict`` objects (JSON compatible);
these ``TypedDict`` definitions document the expected keys for type checkers
and future refactors.
"""

from typing import Any, Dict, List, TypedDict


class PlanOptions(TypedDict, total=False):
    domain_map: Dict[str, str]
    mail_host_map: Dict[str, str]
    default_mail_host: str
    fixed_password: str
    fixed_password_encrypted: str
    random_password: bool
    force_password_change: bool
    reset_existing_passwords: bool
    include_admin_accounts: bool
    include_system_accounts: bool
    keep_admin_permissions: bool
    copy_mailbox: bool
    copy_settings: bool
    copy_prefs: bool
    copy_folders: bool
    copy_shares: bool
    copy_permissions: bool
    copy_filters: bool
    copy_whitelists: bool
    attr_ignore: List[str]
    pref_ignore: List[str]
    provisioning_attr_ignore: List[str]
    provisioning_pref_ignore: List[str]
    settings_attr_ignore: List[str]
    settings_pref_ignore: List[str]
    mailbox_keep_archives: bool
    mailbox_keep_tgz_days: int
    diff_keep_archives: bool
    diff_keep_tgz_days: int
    keep_tgz_days: int
    mailbox_resolve: str
    skip_duplicates: bool
    parallelism: int
    max_accounts_per_plan: int
    content_types: List[str]


class PlanAction(TypedDict, total=False):
    id: str
    kind: str
    target: str
    description: str
    payload: Dict[str, Any]
    risk: str
    depends_on: List[str]


class AccountPair(TypedDict, total=False):
    source: str
    destination: str
    status: str
    note: str


class MailboxPair(TypedDict, total=False):
    source: str
    destination: str
    size: Any
    messages: Any
    destination_status: str
    note: str


class Plan(TypedDict, total=False):
    version: int
    planner_version: int
    category: str
    profile: str
    profile_name: str
    created_at: str
    source: Dict[str, Any]
    destination: Dict[str, Any]
    validation: Dict[str, Any]
    summary: Dict[str, int]
    total_actions: int
    actions: List[PlanAction]
    accounts: List[AccountPair]
    mailboxes: List[MailboxPair]
    mailbox_count: int
    warnings: List[str]
    info: List[str]
    mapping: Dict[str, Any]
    options: PlanOptions
    selection: Dict[str, Any]
    id_map: List[Dict[str, str]]


class Snapshot(TypedDict, total=False):
    version: int
    created_at: str
    side: str
    profile: str
    profile_name: str
    host: str
    path: str
    counts: Dict[str, int]
    domains: List[Dict[str, Any]]
    cos: List[Dict[str, Any]]
    servers: List[Dict[str, Any]]
    accounts: List[Dict[str, Any]]
    distribution_lists: List[Dict[str, Any]]
    resources: List[Dict[str, Any]]
    warnings: List[str]


class Report(TypedDict, total=False):
    created_at: str
    dry_run: bool
    profile: str
    profile_name: str
    source_host: str
    destination_host: str
    total: int
    summary: Dict[str, int]
    items: List[Dict[str, Any]]
    report_path: str
