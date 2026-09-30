"""Quota and usage summary per context (cloud, project, region)."""

from __future__ import annotations

import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from typing import Any

from openstack.connection import Connection

from .cloud import MISSING_SERVICE, Context
from .gpu import count_gpus


@dataclass
class Usage:
    used: float
    limit: float | None  # None or negative = unlimited

    @property
    def ratio(self) -> float | None:
        if self.limit is None or self.limit <= 0:
            return None
        return self.used / self.limit

    def text(self) -> str:
        used = _num(self.used)
        if self.limit is None or self.limit < 0:
            return f"{used}/∞"
        return f"{used}/{_num(self.limit)}"


def _num(value: float) -> str:
    rounded = round(float(value), 1)
    return str(int(rounded)) if rounded.is_integer() else f"{rounded:.1f}"


@dataclass
class Summary:
    ctx: Context
    usage: dict[str, Usage] = field(default_factory=dict)
    servers: Counter[str] | None = None
    # GPUs in use by model (no GPU quota is readable by non-admin users).
    gpus: Counter[str] | None = None
    errors: dict[str, str] = field(default_factory=dict)
    # Services whose answer is still missing.
    pending: set[str] = field(default_factory=set)
    elapsed: float = 0.0

    @property
    def worst_ratio(self) -> float:
        ratios = [u.ratio for u in self.usage.values() if u.ratio is not None]
        return max(ratios, default=0.0)


def _compute(conn: Connection, _ctx: Context) -> dict[str, Usage]:
    lim = conn.compute.get_limits().absolute
    ram_limit = lim.total_ram / 1024 if lim.total_ram > 0 else -1
    return {
        "instances": Usage(lim.instances_used, lim.instances),
        "cores": Usage(lim.total_cores_used, lim.total_cores),
        "ram": Usage(lim.total_ram_used / 1024, ram_limit),
    }


# Cinder quotas per volume type are keyed "<metric>_<type>", e.g. "gigabytes_Ceph-SSD".
VOLUME_TYPE_METRICS = ("volumes", "gigabytes")


def _volume(conn: Connection, ctx: Context) -> dict[str, Usage]:
    q = conn.block_storage.get_quota_set(ctx.project_id, usage=True)
    usage = q.usage or {}
    out = {
        "volumes": Usage(usage.get("volumes", 0), q.volumes),
        "gigabytes": Usage(usage.get("gigabytes", 0), q.gigabytes),
    }
    # Horizon shows only the totals. Per type limits are the ones that usually
    # run out first; unlimited types are skipped to keep the panel short.
    for key, used in usage.items():
        metric, _, vtype = key.partition("_")
        if metric not in VOLUME_TYPE_METRICS or not vtype:
            continue
        limit = getattr(q, key, None)
        if isinstance(limit, int) and limit >= 0:
            out[f"{metric}:{vtype}"] = Usage(used, limit)
    return out


def _network(conn: Connection, ctx: Context) -> dict[str, Usage]:
    q = conn.network.get_quota(ctx.project_id, details=True)

    def u(name: str) -> Usage:
        d: dict[str, Any] = getattr(q, name) or {}
        return Usage(d.get("used", 0), d.get("limit"))

    return {n: u(n) for n in ("floating_ips", "networks", "security_groups")}


def _object(conn: Connection, _ctx: Context) -> dict[str, Usage]:
    """Swift account usage against its quota (``X-Account-Meta-Quota-Bytes``)."""
    from .resources.swift import account_usage  # late import: resources import this module

    try:
        used, limit = account_usage(conn)
    except MISSING_SERVICE:
        return {}  # no object storage in this cloud or region
    except Exception as exc:
        # Swift often needs a role (e.g. swiftoperator): no access, no row.
        if getattr(exc, "status_code", None) in (401, 403):
            return {}
        raise
    return {"object_gigabytes": Usage(used, limit)}


def server_counts(servers: Any) -> Counter[str]:
    return Counter(s.status for s in servers)


def _servers(conn: Connection, _ctx: Context) -> tuple[Counter[str], Counter[str]]:
    servers = list(conn.compute.servers())
    return server_counts(servers), count_gpus(servers)


QUOTA_JOBS: dict[str, Callable[[Connection, Context], dict[str, Usage]]] = {
    "compute": _compute,
    "volume": _volume,
    "network": _network,
    "object": _object,
}


def summarize(
    conn: Connection,
    ctx: Context,
    on_update: Callable[[Summary], None] | None = None,
    include_servers: bool = True,
) -> Summary:
    """Fetch quotas (and server states) in parallel. Errors are not fatal.

    ``on_update`` is called after each service answers, so the UI can show
    partial results: the slowest APIs (Neutron quota details, Nova server
    list) can take several seconds on the first call.
    """
    start = time.monotonic()
    summary = Summary(ctx, pending=set(QUOTA_JOBS) | ({"servers"} if include_servers else set()))
    lock = threading.Lock()
    jobs: dict[str, Callable[[Connection, Context], Any]] = dict(QUOTA_JOBS)
    if include_servers:
        jobs["servers"] = _servers
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {pool.submit(fn, conn, ctx): name for name, fn in jobs.items()}
        for fut in as_completed(futures):
            name = futures[fut]
            with lock:
                try:
                    result = fut.result()
                except Exception as exc:  # noqa: BLE001 - each service may fail on its own
                    summary.errors[name] = short_error(exc)
                else:
                    if name == "servers":
                        summary.servers, summary.gpus = result
                    else:
                        summary.usage.update(result)
                summary.pending.discard(name)
                summary.elapsed = time.monotonic() - start
                snapshot = replace(
                    summary,
                    usage=dict(summary.usage),
                    errors=dict(summary.errors),
                    servers=Counter(summary.servers) if summary.servers is not None else None,
                    gpus=Counter(summary.gpus) if summary.gpus is not None else None,
                    pending=set(summary.pending),
                )
            if on_update is not None:
                on_update(snapshot)
    return summary


def short_error(exc: BaseException) -> str:
    code = getattr(exc, "status_code", None)
    if code == 403:
        return "forbidden"
    if code == 404:
        return "not found"
    text = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    return f"{code}: {text}" if code else text[:80]
