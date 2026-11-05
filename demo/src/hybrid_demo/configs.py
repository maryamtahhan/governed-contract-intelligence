"""One source of truth for the four routing-policy names.

``configs.json`` beside this module is the canonical record. The CLI, the UI
payload, the trace UI, the presenter guide and ``scripts/configs.sh`` all read
it, so a rename cannot leave one page behind.

The internal keys stay ``A``/``B``/``C``/``D``: they are recorded evidence in
``summary.arm`` and in historical run directories, and must not change.

Nothing here maps a config to hardware. A config names routing behavior; what
actually served a run is its recorded pool map.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

CONFIG_FILE = Path(__file__).with_name("configs.json")


@lru_cache(maxsize=1)
def _catalog() -> tuple[dict[str, Any], ...]:
    data = json.loads(CONFIG_FILE.read_text())
    return tuple(data["configs"])


def catalog() -> list[dict[str, Any]]:
    """A copy of the canonical config records, in A/B/C/D order."""
    return [dict(record) for record in _catalog()]


CONFIG_NAMES: dict[str, str] = {c["key"]: c["name"] for c in _catalog()}
CONFIG_MODES: dict[str, str] = {c["key"]: c["mode"] for c in _catalog()}
GOVERNED_CONFIGS: set[str] = {c["key"] for c in _catalog() if c.get("governance")}


def _aliases() -> dict[str, str]:
    aliases: dict[str, str] = {}
    for record in _catalog():
        key = record["key"]
        aliases[key.lower()] = key
        aliases[record["name"].lower()] = key
        for alias in record.get("aliases", []):
            aliases[alias.lower()] = key
    return aliases


CONFIG_ALIASES: dict[str, str] = _aliases()


def resolve_config(value: str) -> str:
    """Turn a policy name, legacy alias or letter into the internal key."""
    key = str(value).strip().lower()
    code = CONFIG_ALIASES.get(key)
    if code is None:
        choices = ", ".join(CONFIG_NAMES.values())
        raise ValueError(f"unknown config {value!r}; choose {choices}")
    return code


def name_for_key(value: str | None) -> str | None:
    """Name a recorded A/B/C/D key; return None for anything else.

    A direct run records ``arm: direct`` (or its pool directory), which is not
    a routing config. Callers decide how to label those runs; this function
    never invents a config name for them.
    """
    if not value:
        return None
    return CONFIG_NAMES.get(str(value).strip().upper())


def meta_for(value: str | None) -> dict[str, Any] | None:
    """The canonical record for a name, alias or key."""
    if not value:
        return None
    key = CONFIG_ALIASES.get(str(value).strip().lower())
    if key is None:
        return None
    return next(dict(c) for c in _catalog() if c["key"] == key)
