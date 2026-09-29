"""Clouds, projects and regions.

One authenticated session is opened per ``clouds.yaml`` entry. Per-region
connections reuse that session (and therefore its token), so switching region is
immediate. A password based credential can switch to any project of the user.
An application credential is bound to one project: to work on several projects
with application credentials, add one ``clouds.yaml`` entry per project. Entries
with the same ``auth_url`` and user are grouped, so ``:project`` lists them all.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from openstack.config import OpenStackConfig, cloud_region
from openstack.connection import Connection

from .tokens import TokenCache, cache_key

DEFAULT_TIMEOUT = 30
REGION_SERVICES = {"compute", "network", "volumev3", "block-storage", "image"}


def access_info(conn: Connection) -> Any:
    """Current token (with the service catalog) of the connection."""
    auth: Any = conn.session.auth
    return auth.get_access(conn.session)


@dataclass(frozen=True)
class Context:
    cloud: str
    project_id: str
    project_name: str
    region: str

    def label(self) -> str:
        return f"{self.cloud} ▸ {self.project_name} ▸ {self.region}"


@dataclass(frozen=True)
class Project:
    id: str
    name: str


@dataclass(frozen=True)
class Target:
    """A project reachable through a given ``clouds.yaml`` entry."""

    cloud: str
    project: Project


class CloudManager:
    def __init__(
        self,
        config_file: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        token_cache: TokenCache | None = None,
    ) -> None:
        self._config_files = [config_file] if config_file else None
        self._config = OpenStackConfig(config_files=self._config_files)
        self._timeout = timeout
        self._tokens = token_cache or TokenCache()
        self._lock = threading.RLock()
        self._passwords: dict[str, str] = {}
        self._raw_cache: dict[str, Any] = {}
        self._base: dict[str, Connection] = {}
        self._scoped: dict[tuple[str, str], Connection] = {}
        self._regional: dict[Context, Connection] = {}
        self._regions: dict[tuple[str, str], list[str]] = {}
        self._projects: dict[str, list[Project]] = {}

    # --- clouds -----------------------------------------------------------

    def cloud_names(self) -> list[str]:
        return sorted(self._config.get_cloud_names())

    def _raw(self, cloud: str) -> Any:
        """Cloud configuration without authenticating (cached)."""
        with self._lock:
            if cloud not in self._raw_cache:
                self._raw_cache[cloud] = self._config.get_one(cloud=cloud)
            return self._raw_cache[cloud]

    def needs_password(self, cloud: str) -> bool:
        """True when the entry uses password auth and has no password configured."""
        with self._lock:
            if cloud in self._passwords or cloud in self._base:
                return False
        raw = self._raw(cloud)
        auth_type = raw.config.get("auth_type") or "password"
        if "password" not in auth_type or raw.auth.get("password"):
            return False
        # A cached token still valid avoids asking for the password.
        return self._tokens.load(self._token_key(cloud)) is None

    def _token_key(self, cloud: str, project_id: str | None = None) -> str:
        return cache_key(cloud, dict(self._raw(cloud).auth), project_id)

    def _authorize(self, conn: Connection, key: str) -> None:
        """Reuse a cached token if still valid, otherwise authenticate and cache it."""
        auth: Any = conn.session.auth
        state = self._tokens.load(key)
        if state:
            auth.set_auth_state(state)
        auth.get_access(conn.session)  # authenticates when the state is missing or stale
        self._tokens.save(key, auth.get_auth_state())

    def set_password(self, cloud: str, password: str) -> None:
        """Keep a password in memory only (never written to disk)."""
        with self._lock:
            self._passwords[cloud] = password
            self._base.pop(cloud, None)

    def base(self, cloud: str) -> Connection:
        with self._lock:
            if cloud not in self._base:
                extra: dict[str, Any] = {}
                if cloud in self._passwords:
                    extra["password"] = self._passwords[cloud]
                region = self._config.get_one(cloud=cloud, api_timeout=self._timeout, **extra)
                conn = Connection(config=region, app_name="ostack9s")
                self._authorize(conn, self._token_key(cloud))
                self._base[cloud] = conn
            return self._base[cloud]

    def is_project_locked(self, cloud: str) -> bool:
        """True when the credential is bound to a single project (application credential)."""
        return "applicationcredential" in self.auth_type(cloud).replace("_", "")

    def auth_type(self, cloud: str) -> str:
        return self._raw(cloud).config.get("auth_type") or "password"

    def auth_url(self, cloud: str) -> str:
        return str(self._raw(cloud).auth.get("auth_url") or "").rstrip("/")

    def user_name(self, cloud: str) -> str:
        access = access_info(self.base(cloud))
        return access.username or access.user_id or ""

    def user_id(self, cloud: str) -> str:
        return access_info(self.base(cloud)).user_id or ""

    # --- projects ---------------------------------------------------------

    def current_project(self, cloud: str) -> Project:
        access = access_info(self.base(cloud))
        return Project(access.project_id, access.project_name)

    def projects(self, cloud: str) -> list[Project]:
        """Projects reachable with the credential of this entry."""
        with self._lock:
            if cloud in self._projects:
                return self._projects[cloud]
        current = self.current_project(cloud)
        if self.is_project_locked(cloud):
            result = [current]
        else:
            conn = self.base(cloud)
            identity: Any = conn.identity
            found = identity.user_projects(conn.current_user_id)
            result = sorted(
                (Project(p.id, p.name) for p in found if p.is_enabled is not False),
                key=lambda p: p.name.lower(),
            ) or [current]
        with self._lock:
            self._projects[cloud] = result
        return result

    def siblings(self, cloud: str) -> list[str]:
        """Other entries with the same Keystone endpoint (not yet checked for user)."""
        url = self.auth_url(cloud)
        return [c for c in self.cloud_names() if c != cloud and self.auth_url(c) == url]

    def targets(self, cloud: str) -> list[Target]:
        """Projects of this entry plus those of sibling entries of the same user.

        Sibling entries needing a password that was not given are skipped.
        """
        result = [Target(cloud, p) for p in self.projects(cloud)]
        seen = {t.project.id for t in result}
        user = self.user_id(cloud)
        candidates = [c for c in self.siblings(cloud) if not self.needs_password(c)]

        def probe(other: str) -> list[Target]:
            try:
                if self.user_id(other) != user:
                    return []
                return [Target(other, p) for p in self.projects(other)]
            except Exception:  # noqa: BLE001 - unreachable or invalid entry
                return []

        if candidates:
            with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as pool:
                for found in pool.map(probe, candidates):
                    for target in found:
                        if target.project.id not in seen:
                            seen.add(target.project.id)
                            result.append(target)
        return sorted(result, key=lambda t: t.project.name.lower())

    def scoped(self, cloud: str, project_id: str) -> Connection:
        base = self.base(cloud)
        if project_id == base.current_project_id:
            return base
        with self._lock:
            key = (cloud, project_id)
            if key not in self._scoped:
                conn = base.connect_as_project(project_id)
                self._authorize(conn, self._token_key(cloud, project_id))
                self._scoped[key] = conn
            return self._scoped[key]

    # --- regions ----------------------------------------------------------

    def regions(self, cloud: str, project_id: str) -> list[str]:
        key = (cloud, project_id)
        with self._lock:
            if key in self._regions:
                return self._regions[key]
        conn = self.scoped(cloud, project_id)
        access = access_info(conn)
        interface = conn.config.config.get("interface") or "public"
        found: set[str] = set()
        # Only regions with "workload" services: identity often has endpoints in
        # technical regions that host no resources.
        for service in access.service_catalog.catalog or []:
            if service.get("type") not in REGION_SERVICES:
                continue
            for ep in service.get("endpoints", []):
                region = ep.get("region_id") or ep.get("region")
                if region and ep.get("interface", interface) == interface:
                    found.add(region)
        result = sorted(found) or [conn.config.region_name or ""]
        with self._lock:
            self._regions[key] = result
        return result

    def default_region(self, cloud: str) -> str | None:
        return self.base(cloud).config.region_name

    # --- per-context connections -------------------------------------------

    def context(
        self, cloud: str, project_id: str | None = None, region: str | None = None
    ) -> Context:
        projects = self.projects(cloud)
        project = next((p for p in projects if p.id == project_id), None)
        if project is None:
            project = self.current_project(cloud)
        regions = self.regions(cloud, project.id)
        if region not in regions:
            region = self.default_region(cloud)
            if region not in regions:
                region = regions[0]
        return Context(cloud, project.id, project.name, region or "")

    def connection(self, ctx: Context) -> Connection:
        with self._lock:
            if ctx in self._regional:
                return self._regional[ctx]
        scoped = self.scoped(ctx.cloud, ctx.project_id)
        if scoped.config.region_name == ctx.region:
            conn = scoped
        else:
            config = cloud_region.from_session(
                scoped.session,
                name=ctx.cloud,
                region_name=ctx.region,
                app_name="ostack9s",
                interface=scoped.config.config.get("interface"),
            )
            conn = Connection(config=config)
        with self._lock:
            self._regional[ctx] = conn
        return conn

    def all_contexts(self) -> list[Context]:
        """Every reachable cloud × project × region combination.

        Entries needing a password that was not given are skipped.
        """
        out = []
        seen: set[tuple[str, str]] = set()
        for cloud in self.cloud_names():
            if self.needs_password(cloud):
                continue
            try:
                projects = self.projects(cloud)
            except Exception:  # noqa: BLE001 - unreachable or invalid entry
                continue
            for project in projects:
                for region in self.regions(cloud, project.id):
                    if (project.id, region) in seen:
                        continue
                    seen.add((project.id, region))
                    out.append(Context(cloud, project.id, project.name, region))
        return out
