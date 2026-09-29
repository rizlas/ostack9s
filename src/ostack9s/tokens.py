"""On-disk cache of Keystone tokens.

Getting a token costs about two seconds on some clouds, and each OpenStack
service validates a new token against Keystone on its first call. Reusing the
token across runs makes startup faster. Files are private to the user (0600 in a
0700 directory) and are ignored once the token is about to expire.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# A token closer than this to its expiry is not reused.
MIN_VALIDITY = timedelta(minutes=10)
SECRET_KEYS = ("password", "secret", "token")


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "ostack9s" / "tokens"


def cache_key(cloud: str, auth: dict[str, Any], project_id: str | None = None) -> str:
    """Stable key from the non secret parts of the auth configuration."""
    public = {k: v for k, v in auth.items() if not any(s in k for s in SECRET_KEYS)}
    raw = json.dumps([cloud, public, project_id], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _expires_at(state: str) -> datetime | None:
    try:
        body = json.loads(state)["body"]
        value = body["token"]["expires_at"]
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None


def is_fresh(state: str | None) -> bool:
    if not state:
        return False
    expires = _expires_at(state)
    return expires is not None and expires - datetime.now(UTC) > MIN_VALIDITY


class TokenCache:
    def __init__(self, directory: Path | None = None, enabled: bool = True) -> None:
        self.directory = directory or cache_dir()
        self.enabled = enabled

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def load(self, key: str) -> str | None:
        if not self.enabled:
            return None
        try:
            state = self._path(key).read_text()
        except OSError:
            return None
        return state if is_fresh(state) else None

    def save(self, key: str, state: str | None) -> None:
        if not self.enabled or not state or not is_fresh(state):
            return
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = self._path(key)
            tmp = path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(state)
            tmp.replace(path)
        except OSError:
            pass  # the cache is an optimisation only
