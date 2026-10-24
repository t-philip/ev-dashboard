"""Configuration. Secrets come from files, never from environment values.

Non-secret settings come from ``EVDASH_*`` environment variables. A secret is read
from ``$CREDENTIALS_DIRECTORY/<name>`` (the systemd ``LoadCredential=`` layout) or
from the file named by ``EVDASH_<NAME>_FILE``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from .auth import OAuthConfig

DEFAULT_AUTHORIZE_URL = "https://volvoid.eu.volvocars.com/as/authorization.oauth2"
DEFAULT_TOKEN_URL = "https://volvoid.eu.volvocars.com/as/token.oauth2"


class ConfigError(Exception):
    """A setting is missing or wrong. Names the setting, never a value."""


@dataclass(frozen=True)
class Settings:
    oauth: OAuthConfig
    listen_host: str
    listen_port: int
    token_file: Path


def read_credential(name: str, env: Mapping[str, str]) -> str:
    path: Optional[Path] = None
    explicit = env.get(f"EVDASH_{name.upper()}_FILE")
    if explicit:
        path = Path(explicit)
    elif env.get("CREDENTIALS_DIRECTORY"):
        path = Path(env["CREDENTIALS_DIRECTORY"]) / name
    if path is None:
        raise ConfigError(f"credential '{name}' not configured "
                          f"(set CREDENTIALS_DIRECTORY or EVDASH_{name.upper()}_FILE)")
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        raise ConfigError(f"credential '{name}' could not be read") from None
    if not value:
        raise ConfigError(f"credential '{name}' is empty")
    return value


def load(env: Mapping[str, str] = os.environ) -> Settings:
    redirect = env.get("EVDASH_REDIRECT_URI", "")
    if not redirect.startswith("https://"):
        raise ConfigError("EVDASH_REDIRECT_URI must be an https:// address")
    scopes = tuple(env.get("EVDASH_SCOPES", "").split())
    if not scopes:
        raise ConfigError("EVDASH_SCOPES must list the read scopes to request")
    authorize = env.get("EVDASH_AUTHORIZE_URL", DEFAULT_AUTHORIZE_URL)
    token = env.get("EVDASH_TOKEN_URL", DEFAULT_TOKEN_URL)
    for name, url in (("EVDASH_AUTHORIZE_URL", authorize), ("EVDASH_TOKEN_URL", token)):
        if not url.startswith("https://"):
            raise ConfigError(f"{name} must be an https:// address")
    host, _, port = env.get("EVDASH_LISTEN", "127.0.0.1:8100").rpartition(":")
    try:
        port_number = int(port)
    except ValueError:
        raise ConfigError("EVDASH_LISTEN must look like host:port") from None
    oauth = OAuthConfig(
        client_id=read_credential("client_id", env),
        client_secret=read_credential("client_secret", env),
        redirect_uri=redirect,
        authorize_url=authorize,
        token_url=token,
        scopes=scopes,
    )
    return Settings(oauth, host or "127.0.0.1", port_number,
                    Path(env.get("EVDASH_TOKEN_FILE", "data/tokens.json")))
