"""Shared mailbox constants, errors and formatting helpers."""

import datetime
from typing import Any, Callable, Dict, List, Optional

Progress = Optional[Callable[[int, int, str], None]]


CONTENT_SPECS: Dict[str, Dict[str, str]] = {
    "all": {
        "label": "Tous les contenus",
        "query": "is:anywhere",
        "types": "",
    },
    "mail": {
        "label": "Mails",
        "query": "is:anywhere",
        "types": "message",
    },
    "calendar": {
        "label": "Agenda",
        "query": "is:anywhere",
        "types": "appointment",
    },
    "contacts": {
        "label": "Contacts",
        "query": "is:anywhere",
        "types": "contact",
    },
    "tasks": {
        "label": "Tâches",
        "query": "is:anywhere",
        "types": "task",
    },
    "briefcase": {
        "label": "Briefcase",
        "query": "is:anywhere",
        "types": "document",
    },
}


class MailboxError(Exception):
    pass


def normalize_content_types(values: Optional[List[str]]) -> List[str]:
    if values is None:
        return ["all"]
    if "all" in values:
        return ["all"]
    return [name for name in CONTENT_SPECS if name in values and name != "all"]


def since_query(value: str) -> str:
    """Convert an ISO date to the Zimbra ``date:>=`` search fragment."""
    value = str(value or "").strip()
    if not value:
        return ""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            parsed = datetime.datetime.strptime(value, fmt)
            return "date:>=%s" % parsed.strftime("%m/%d/%Y")
        except ValueError:
            continue
    raise MailboxError(
        "Date --differential invalide : utilisez le format YYYY-MM-DD."
    )


def human_size(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "?"
    for unit in ("o", "Ko", "Mo", "Go", "To"):
        if number < 1024 or unit == "To":
            return "%.1f %s" % (number, unit)
        number /= 1024
    return "%.1f To" % number


