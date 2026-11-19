"""Shared settings constants, errors and small helpers."""

import fnmatch
from typing import Any, Callable, Dict, List, Optional

from . import config as config_module

Progress = Optional[Callable[[int, int, str], None]]


SETTING_PARTS = (
    "settings",
    "prefs",
    "folder",
    "folders",
    "color",
    "attributes",
    "tags",
    "cos",
    "filters",
    "lists",
    "shares",
    "permissions",
)


class SettingsError(Exception):
    pass


def a_entries(attrs: Dict[str, Any]) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    for name, value in (attrs or {}).items():
        if isinstance(value, (list, tuple)):
            for item in value:
                entries.append({"n": name, "_content": item})
        else:
            entries.append({"n": name, "_content": value})
    return entries


def _pref_value(value: Any) -> Any:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return value


def pref_entries(prefs: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build ``<pref name="...">value</pref>`` entries for ModifyPrefs."""
    entries: List[Dict[str, Any]] = []
    for name, value in (prefs or {}).items():
        if isinstance(value, (list, tuple)):
            for item in value:
                entries.append({"name": name, "_content": _pref_value(item)})
        else:
            entries.append({"name": name, "_content": _pref_value(value)})
    return entries


def _norm(value: Any) -> Any:
    if isinstance(value, list):
        return sorted(str(item) for item in value)
    return str(value).strip()


def _map_email(options: Dict[str, Any], email: str) -> str:
    if not email or "@" not in email:
        return email
    mapping = {
        str(key).lower(): value
        for key, value in (options.get("domain_map") or {}).items()
    }
    local, domain = email.rsplit("@", 1)
    return "%s@%s" % (local, mapping.get(str(domain).lower(), domain) or domain)


def _ignored_pref(options: Dict[str, Any], name: str) -> bool:
    patterns = (
        options.get("settings_attr_ignore")
        or options.get("settings_pref_ignore")
        or options.get("pref_ignore")
        or config_module.DEFAULT_PREF_IGNORE
    )
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def normalize_parts(
    parts: Optional[List[str]], options: Dict[str, Any]
) -> List[str]:
    if parts:
        return [part for part in parts if part in SETTING_PARTS]
    if any(
        key in options
        for key in (
            "copy_settings",
            "copy_prefs",
            "copy_filters",
            "copy_whitelists",
            "copy_shares",
            "copy_permissions",
        )
    ):
        selected: List[str] = []
        if options.get("copy_settings", True):
            selected.append("settings")
            selected.append("folders")
        if options.get("copy_prefs", False):
            selected.append("prefs")
        if options.get("copy_filters", True):
            selected.append("filters")
        if options.get("copy_whitelists", True):
            selected.append("lists")
        if options.get("copy_shares", True):
            selected.append("shares")
        if options.get("copy_permissions", True):
            selected.append("permissions")
        return selected
    return list(SETTING_PARTS)


def _options_with_defaults(
    config: config_module.Config,
    plan: Dict[str, Any],
    options: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    merged = dict(config.sync())
    merged.update(plan.get("options") or {})
    merged.update(options or {})
    return merged


FOLDER_RGB_COLORS = {
    "#0000CC": "1",
    "#00CCCC": "2",
    "#00CC00": "3",
    "#CC00CC": "4",
    "#CC0000": "5",
    "#CCCC00": "6",
    "#FF00FF": "7",
    "#666666": "8",
    "#CC6600": "9",
}


def _rgb_to_folder_color(value: str) -> str:
    text = (value or "").strip().upper()
    if text in FOLDER_RGB_COLORS:
        return FOLDER_RGB_COLORS[text]
    if not (len(text) == 7 and text.startswith("#")):
        return ""
    try:
        red, green, blue = (
            int(text[1:3], 16),
            int(text[3:5], 16),
            int(text[5:7], 16),
        )
    except ValueError:
        return ""
    best = ""
    best_distance = None
    for candidate, color in FOLDER_RGB_COLORS.items():
        cr, cg, cb = (
            int(candidate[1:3], 16),
            int(candidate[3:5], 16),
            int(candidate[5:7], 16),
        )
        distance = (red - cr) ** 2 + (green - cg) ** 2 + (blue - cb) ** 2
        if best_distance is None or distance < best_distance:
            best = color
            best_distance = distance
    return best


