"""Search a name, ID or IP address in every project and region.

Horizon searches only inside the current project: finding who owns an address
means switching project by project. Here every context is listed in parallel
and matches come back as soon as each context answers.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from openstack.connection import Connection

from .cloud import MISSING_SERVICE, Context
from .resources.base import attr

MIN_QUERY = 3


def _ips(values: Iterable[dict[str, Any]] | None, key: str = "ip_address") -> list[str]:
    return [v.get(key, "") for v in values or []]


def _server_ips(server: Any) -> list[str]:
    return [
        a.get("addr", "") for addrs in (attr(server, "addresses") or {}).values() for a in addrs
    ]


def _router_ips(router: Any) -> list[str]:
    return _ips(attr(router, "external_gateway_info.external_fixed_ips"))


# (resource kind, list call, searchable values of an item)
SEARCHES: list[tuple[str, Callable[[Connection], Iterable[Any]], Callable[[Any], list[str]]]] = [
    ("compute.server", lambda c: c.compute.servers(), lambda s: [s.name, s.id, *_server_ips(s)]),
    (
        "network.port",
        lambda c: c.network.ports(),
        lambda p: [p.name, p.id, p.mac_address, *_ips(p.fixed_ips)],
    ),
    (
        "network.floating_ip",
        lambda c: c.network.ips(),
        lambda f: [f.id, f.floating_ip_address, f.fixed_ip_address],
    ),
    ("block_storage.volume", lambda c: c.block_storage.volumes(), lambda v: [v.name, v.id]),
    ("network.network", lambda c: c.network.networks(), lambda n: [n.name, n.id]),
    ("network.subnet", lambda c: c.network.subnets(), lambda s: [s.name, s.id, s.cidr]),
    ("network.router", lambda c: c.network.routers(), lambda r: [r.name, r.id, *_router_ips(r)]),
    ("network.security_group", lambda c: c.network.security_groups(), lambda g: [g.name, g.id]),
    (
        "load_balancer.loadbalancer",
        lambda c: c.load_balancer.load_balancers(),
        lambda lb: [lb.name, lb.id, lb.vip_address],
    ),
]


@dataclass(frozen=True)
class Hit:
    ctx: Context
    kind: str
    id: str
    name: str
    match: str
    exact: bool

    @property
    def filter(self) -> str:
        """Text that finds the row again in the resource view."""
        return self.name or self.match


def match_item(query: str, values: Iterable[Any]) -> tuple[str, bool] | None:
    """(matching value, exact) of the first value containing ``query``."""
    q = query.lower()
    found = None
    for value in values:
        text = str(value or "")
        if text.lower() == q:
            return text, True
        if found is None and q in text.lower():
            found = text
    return (found, False) if found is not None else None


def search_context(conn: Connection, ctx: Context, query: str) -> tuple[list[Hit], list[str]]:
    """Matches in one context and the resource kinds that could not be listed."""

    def one(
        kind: str, lister: Callable[[Connection], Iterable[Any]], values: Callable[[Any], list[str]]
    ) -> list[Hit]:
        hits = []
        for item in lister(conn):
            found = match_item(query, values(item))
            if found is not None:
                name = str(attr(item, "name") or "")
                hits.append(Hit(ctx, kind, str(item.id), name, found[0], found[1]))
        return hits

    hits: list[Hit] = []
    failed: list[str] = []
    with ThreadPoolExecutor(max_workers=len(SEARCHES)) as pool:
        futures = {
            kind: pool.submit(one, kind, lister, values) for kind, lister, values in SEARCHES
        }
        for kind, fut in futures.items():
            try:
                hits.extend(fut.result())
            except MISSING_SERVICE:
                continue
            except Exception:  # noqa: BLE001 - forbidden or failing in this context
                failed.append(kind)
    return hits, failed
