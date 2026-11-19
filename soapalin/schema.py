"""Schema validation for persisted snapshots and migration plans."""

from typing import Any, Dict

SUPPORTED_SNAPSHOT_VERSIONS = {1}
SUPPORTED_PLAN_VERSIONS = {2}


class SchemaError(ValueError):
    """Raised when a persisted artefact uses an unsupported schema."""


def _version(payload: Dict[str, Any]) -> int:
    value = payload.get("version")
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        raise SchemaError("version invalide : %r" % value) from None


def validate_snapshot(snapshot: Any) -> Dict[str, Any]:
    if not isinstance(snapshot, dict):
        raise SchemaError("un snapshot doit etre un objet JSON.")
    version = _version(snapshot)
    if version and version not in SUPPORTED_SNAPSHOT_VERSIONS:
        raise SchemaError(
            "version de snapshot non supportee : %s (supportees : %s)"
            % (version, ", ".join(str(v) for v in sorted(SUPPORTED_SNAPSHOT_VERSIONS)))
        )
    for key in ("side", "host"):
        value = snapshot.get(key)
        if value is not None and not isinstance(value, str):
            raise SchemaError("champ snapshot invalide : %s" % key)
    for key in (
        "accounts",
        "domains",
        "cos",
        "servers",
        "distribution_lists",
        "resources",
        "warnings",
    ):
        value = snapshot.get(key)
        if value is not None and not isinstance(value, list):
            raise SchemaError("champ snapshot invalide : %s" % key)
    return snapshot


def validate_plan(plan: Any) -> Dict[str, Any]:
    if not isinstance(plan, dict):
        raise SchemaError("un plan doit etre un objet JSON.")
    version = _version(plan)
    if version and version not in SUPPORTED_PLAN_VERSIONS:
        raise SchemaError(
            "version de plan non supportee : %s (supportees : %s)"
            % (version, ", ".join(str(v) for v in sorted(SUPPORTED_PLAN_VERSIONS)))
        )
    actions = plan.get("actions")
    if actions is not None and not isinstance(actions, list):
        raise SchemaError("champ plan invalide : actions")
    return plan
