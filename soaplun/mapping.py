"""Pure domain/host mapping helpers shared by config and plan options."""

import fnmatch
from typing import Any, Dict, Iterable, Optional


def _normalized(mapping: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        str(key).lower(): value for key, value in (mapping or {}).items()
    }


def map_domain(mapping: Optional[Dict[str, Any]], domain: str) -> str:
    normalized = _normalized(mapping)
    return normalized.get(str(domain).lower(), domain) or domain


def map_email(mapping: Optional[Dict[str, Any]], email: str) -> str:
    if not email or "@" not in email:
        return email
    local, domain = email.rsplit("@", 1)
    return "%s@%s" % (local, map_domain(mapping, domain))


def map_host(
    mapping: Optional[Dict[str, Any]], default: str, host: str
) -> str:
    normalized = _normalized(mapping)
    return normalized.get(str(host).lower()) or default or host


def matches(name: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)
