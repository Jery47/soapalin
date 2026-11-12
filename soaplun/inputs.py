"""CSV inputs and migration element selection helpers."""

import argparse
import csv
import datetime
import sys
from typing import Callable, Dict, Iterable, List, Optional

ACCOUNT_ELEMENTS = (
    "mail",
    "calendar",
    "contacts",
    "tasks",
    "briefcase",
    "cos",
    "folder",
    "color",
    "settings",
    "tags",
    "filter",
    "banlist",
    "whitelist",
    "share",
    "sendas",
    "sendonbehalfof",
)
RESOURCE_ELEMENTS = ("mail", "calendar", "folder", "share")
DL_ELEMENTS = ("members",)
ELEMENT_ALIASES = {
    "task": "tasks",
    "filters": "filter",
    "shares": "share",
    "sendas": "sendas",
    "sendonbehalfof": "sendonbehalfof",
}

OBJECT_HEADERS = {
    "name",
    "nom",
    "domain",
    "domaine",
    "cos",
    "list",
    "liste",
    "dl",
    "resource",
    "ressource",
    "room",
    "salle",
    "equipment",
    "equipement",
}


def iso_date(value: str) -> datetime.date:
    try:
        return datetime.datetime.strptime(str(value), "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(
            "date invalide %r : utilisez YYYY-MM-DD" % value
        ) from exc


def elements_for_type(kind: str) -> List[str]:
    if kind == "account":
        return list(ACCOUNT_ELEMENTS)
    if kind == "resource":
        return list(RESOURCE_ELEMENTS)
    if kind == "dl":
        return list(DL_ELEMENTS)
    return list(ACCOUNT_ELEMENTS + RESOURCE_ELEMENTS + DL_ELEMENTS)


def normalize_migrate_elements(
    kind: str,
    values: Optional[Iterable[str]],
    warn: Optional[Callable[[str], None]] = None,
) -> List[str]:
    values = list(values or ["all"])
    if "all" in values:
        if len(values) != 1:
            raise ValueError("'all' doit etre utilise seul.")
        return elements_for_type(kind)
    valid = set(ACCOUNT_ELEMENTS + RESOURCE_ELEMENTS + DL_ELEMENTS)
    applicable = set(elements_for_type(kind))
    normalized: List[str] = []
    ignored: List[str] = []
    for raw in values:
        value = str(raw or "").strip().lower()
        if not value:
            continue
        value = ELEMENT_ALIASES.get(value, value)
        if value not in valid:
            raise ValueError("element inconnu : %s" % raw)
        if value not in applicable:
            ignored.append(value)
            continue
        if value not in normalized:
            normalized.append(value)
    for value in ignored:
        message = (
            "AVERTISSEMENT : element %s inapplicable au type %s, ignore. "
            "Elements valides pour %s : %s."
            % (
                value,
                kind,
                kind,
                ", ".join(elements_for_type(kind)),
            )
        )
        if warn is not None:
            warn(message)
        else:
            print(message, file=sys.stderr)
    if not normalized:
        raise ValueError(
            "aucun element applicable au type %s ; elements valides : %s."
            % (kind, ", ".join(elements_for_type(kind)))
        )
    return normalized


def _read_rows(path: str) -> List[List[str]]:
    if not path:
        return []
    if path == "-":
        return list(csv.reader(sys.stdin))
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.reader(handle))


def read_users(path: str, warn: Optional[Callable[[str], None]] = None) -> List[str]:
    def emit(message: str) -> None:
        if warn is not None:
            warn(message)
        else:
            print(message, file=sys.stderr)

    users: List[str] = []
    seen = set()
    for line_no, row in enumerate(_read_rows(path), 1):
        if not row:
            continue
        raw = str(row[0] or "")
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        if value.lower() in {"email", "account", "mail", "user", "compte"}:
            continue
        if raw != value:
            emit(
                "AVERTISSEMENT ligne %d : espaces ignores autour de %r."
                % (line_no, value)
            )
        if "@" not in value:
            emit(
                "AVERTISSEMENT ligne %d ignoree : adresse invalide %r."
                % (line_no, value)
            )
            continue
        if any(str(column).strip() for column in row[1:]):
            emit(
                "AVERTISSEMENT ligne %d : colonnes supplementaires ignorees."
                % line_no
            )
        normalized = value.lower()
        if normalized not in seen:
            seen.add(normalized)
            users.append(value)
        else:
            emit(
                "AVERTISSEMENT ligne %d : doublon ignore %s."
                % (line_no, value)
            )
    return users


def read_mapping(path: str) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for row in _read_rows(path):
        if not row:
            continue
        if len(row) == 1 and "=" in row[0]:
            left, right = row[0].split("=", 1)
        elif len(row) >= 2:
            left, right = row[0], row[1]
        else:
            continue
        source = str(left or "").strip()
        destination = str(right or "").strip()
        if not source or not destination:
            continue
        if source.lower() in {"source", "from", "origine"}:
            continue
        mapping[source.lower()] = destination
    return mapping


def read_object_names(path: str, warn: Optional[Callable[[str], None]] = None) -> List[str]:
    def emit(message: str) -> None:
        if warn is not None:
            warn(message)
        else:
            print(message, file=sys.stderr)

    names: List[str] = []
    seen = set()
    for line_no, row in enumerate(_read_rows(path), 1):
        if not row:
            continue
        value = str(row[0] or "").strip()
        if not value or value.startswith("#"):
            continue
        if value.lower() in OBJECT_HEADERS:
            continue
        if value.lower() in seen:
            emit(
                "AVERTISSEMENT ligne %d : doublon ignore %s."
                % (line_no, value)
            )
            continue
        seen.add(value.lower())
        names.append(value)
    return names


def read_object_selection(value: str) -> Optional[List[str]]:
    """Return None for the special 'all' selector, otherwise a name list."""
    text = str(value or "").strip()
    if not text:
        return []
    if text.lower() == "all":
        return None
    return read_object_names(text)
