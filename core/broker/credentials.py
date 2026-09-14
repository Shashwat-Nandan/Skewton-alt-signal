"""Shared credential resolution for broker adapters.

Mirrors `KiteAuthManager._resolve_credential` / `_validate_no_placeholders`
so Kotak/Groww/Dhan fail the same way Zerodha does when an operator leaves
`YOUR_*` template values in place (those would otherwise be posted to the
broker as a password).
"""
from __future__ import annotations

import os
import re
from configparser import ConfigParser
from typing import Mapping

from .errors import BrokerConfigError

_PLACEHOLDER_RE = re.compile(r"^\$\{.+\}$")


def resolve_credential(
    env_var: str,
    config: ConfigParser,
    section: str,
    key: str,
    default: str = "",
) -> str:
    """Env var wins, then config[section][key], then default."""
    env_val = os.environ.get(env_var)
    if env_val:
        return env_val
    if config.has_option(section, key):
        return config.get(section, key)
    return default


def is_placeholder(value: str) -> bool:
    stripped = (value or "").strip()
    return (
        not stripped
        or bool(_PLACEHOLDER_RE.match(stripped))
        or stripped.startswith("YOUR_")
    )


def reject_placeholders(creds: Mapping[str, str]) -> None:
    bad = [name for name, val in creds.items() if is_placeholder(val)]
    if bad:
        raise BrokerConfigError(
            "Credentials not configured: "
            + ", ".join(bad)
            + ". Set the corresponding environment variables or update config.ini."
        )
