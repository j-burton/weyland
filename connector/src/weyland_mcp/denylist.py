"""Path denylist — the small set of paths the connector refuses to touch."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable

_DENIED_PREFIXES: tuple[str, ...] = (
    "/etc/shadow",
    "/etc/gshadow",
    "/etc/sudoers",
    "/etc/ssh/ssh_host_",
    "/root/",
)

_DENIED_SUBPATHS: tuple[str, ...] = (
    ".ssh/",
    ".git/config",   # contains the GitHub token on minions
    ".gnupg/",
    ".aws/credentials",
    ".npmrc",
)


def is_denied(path: str) -> bool:
    """Return True if the resolved path is on the denylist."""
    try:
        resolved = str(Path(path).expanduser().resolve(strict=False))
    except (OSError, RuntimeError):
        return True
    for prefix in _DENIED_PREFIXES:
        if resolved.startswith(prefix):
            return True
    for sub in _DENIED_SUBPATHS:
        if sub in resolved + "/":
            return True
    return False


def filter_denied(paths: Iterable[str]) -> list[str]:
    """Drop denied paths from an iterable."""
    return [p for p in paths if not is_denied(p)]
