"""Load weyland-mcp config from env vars."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    bearer_token_hash: str
    bind_host: str
    bind_port: int
    public_url: str
    log_path: str
    pi_name: str
    pi_repo: str
    pi_dir: str


_DEFAULTS = {
    "WEYLAND_BIND_HOST": "127.0.0.1",
    "WEYLAND_BIND_PORT": "5002",
    "WEYLAND_LOG_PATH": "/var/log/weyland-mcp.log",
}

_REQUIRED = (
    "WEYLAND_BEARER_TOKEN_HASH",
    "WEYLAND_PUBLIC_URL",
    "WEYLAND_PI_NAME",
    "WEYLAND_PI_REPO",
    "WEYLAND_PI_DIR",
)


def _env(name: str) -> str:
    val = os.environ.get(name) or _DEFAULTS.get(name)
    if val is None:
        raise ValueError(f"missing required env var: {name}")
    return val


def load_config() -> Config:
    for name in _REQUIRED:
        if not os.environ.get(name):
            raise ValueError(f"missing required env var: {name}")
    return Config(
        bearer_token_hash=_env("WEYLAND_BEARER_TOKEN_HASH").strip().lower(),
        bind_host=_env("WEYLAND_BIND_HOST"),
        bind_port=int(_env("WEYLAND_BIND_PORT")),
        public_url=_env("WEYLAND_PUBLIC_URL").rstrip("/"),
        log_path=_env("WEYLAND_LOG_PATH"),
        pi_name=_env("WEYLAND_PI_NAME"),
        pi_repo=_env("WEYLAND_PI_REPO"),
        pi_dir=_env("WEYLAND_PI_DIR"),
    )
