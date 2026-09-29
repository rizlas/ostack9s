"""Checks Horizon does not do: unused resources and risky security group rules.

Both work on the lists of one context (project and region) and return
``Finding`` rows, shown by the generic resource view.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from openstack.connection import Connection

from ..cloud import parse_time
from ..i18n import t
from .base import Action, Column, ResourceKind, attr, to_plain

WORLD = {None, "", "0.0.0.0/0", "::/0"}
# Ports that should never be reachable from the whole internet.
SENSITIVE_PORTS = {
    22: ("SSH", "MEDIUM"),
    23: ("Telnet", "HIGH"),
    445: ("SMB", "HIGH"),
    1433: ("MSSQL", "HIGH"),
    2375: ("Docker API", "HIGH"),
    3306: ("MySQL", "HIGH"),
    3389: ("RDP", "HIGH"),
    5432: ("PostgreSQL", "HIGH"),
    5900: ("VNC", "HIGH"),
    6379: ("Redis", "HIGH"),
    9200: ("Elasticsearch", "HIGH"),
    11211: ("Memcached", "HIGH"),
    27017: ("MongoDB", "HIGH"),
}
WIDE_RANGE = 100
SEVERITY_ORDER = {"ERROR": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
STOPPED = {"SHUTOFF", "SHELVED", "SHELVED_OFFLOADED", "SUSPENDED", "PAUSED"}
STOPPED_DAYS = 7
OLD_SNAPSHOT_DAYS = 30


@dataclass
class Finding:
    severity: str
    type: str
    name: str
    id: str
    issue: str
    detail: str = ""
    age_days: int | None = None
    size: int | None = None
    resource: Any = None

    def to_dict(self) -> dict[str, Any]:
        out = {k: v for k, v in vars(self).items() if k != "resource"}
        out["resource"] = to_plain(self.resource) if self.resource is not None else None
        return out


def age_days(value: Any, now: datetime | None = None) -> int | None:
    """Days since an API timestamp."""
    when = parse_time(value)
    if when is None:
        return None
    return max(0, ((now or datetime.now(UTC)) - when).days)


def fetch_lists(
    conn: Connection, jobs: dict[str, Callable[[Connection], Iterable[Any]]]
) -> tuple[dict[str, list[Any]], list[Finding]]:
    """Run the list calls in parallel; a failing one becomes an ERROR finding."""
    from ..overview import short_error  # late import: overview imports this package

    results: dict[str, list[Any]] = {}
    errors: list[Finding] = []
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = {name: pool.submit(lambda f=fn: list(f(conn))) for name, fn in jobs.items()}
        for name, fut in futures.items():
            try:
                results[name] = fut.result()
            except Exception as exc:  # noqa: BLE001 - policies differ between clouds
                results[name] = []
                errors.append(Finding("ERROR", name, "", name, t("cannot list"), short_error(exc)))
    return results, errors


def _sorted(findings: list[Finding]) -> list[Finding]:
    return sorted(
        findings,
        key=lambda f: (SEVERITY_ORDER.get(f.severity, 9), f.type, -(f.age_days or 0), f.name),
    )


# --- security groups ------------------------------------------------------------


def rule_risk(rule: Any) -> tuple[str, str] | None:
    """(severity, issue) of an ingress rule open to the whole internet, or None."""
    if attr(rule, "direction") != "ingress" or attr(rule, "remote_group_id"):
        return None
    if attr(rule, "remote_ip_prefix") not in WORLD:
        return None
    proto = attr(rule, "protocol")
    if proto in (None, "", "any", "0"):
        return "HIGH", t("all traffic open to the internet")
    if proto not in ("tcp", "udp", "6", "17"):
        return None
    lo, hi = attr(rule, "port_range_min"), attr(rule, "port_range_max")
    if lo is None:
        return "HIGH", t("all {proto} ports open to the internet", proto=proto.upper())
    hi = lo if hi is None else hi
    exposed = [(name, sev) for port, (name, sev) in SENSITIVE_PORTS.items() if lo <= port <= hi]
    if exposed:
        severity = "HIGH" if any(sev == "HIGH" for _, sev in exposed) else "MEDIUM"
        names = ", ".join(name for name, _ in exposed)
        return severity, t("{services} open to the internet", services=names)
    if hi - lo + 1 > WIDE_RANGE:
        return "MEDIUM", t("{count} ports open to the internet", count=hi - lo + 1)
    return None


def _rule_text(rule: Any) -> str:
    proto = attr(rule, "protocol") or "any"
    lo, hi = attr(rule, "port_range_min"), attr(rule, "port_range_max")
    ports = "" if lo is None else (f":{lo}" if lo == hi or hi is None else f":{lo}-{hi}")
    remote = attr(rule, "remote_ip_prefix") or "any"
    return f"{attr(rule, 'ethertype') or ''} {proto}{ports} ← {remote}".strip()


def sg_usage(ports: Iterable[Any]) -> Counter[str]:
    """Number of ports using each security group."""
    used: Counter[str] = Counter()
    for port in ports:
        used.update(attr(port, "security_group_ids") or [])
    return used


def security_findings(groups: Iterable[Any], ports: Iterable[Any]) -> list[Finding]:
    used = sg_usage(ports)
    out = []
    for group in groups:
        for rule in attr(group, "security_group_rules") or []:
            risk = rule_risk(rule)
            if risk is None:
                continue
            severity, issue = risk
            count = used.get(group.id, 0)
            if not count and severity == "HIGH":
                severity = "MEDIUM"  # nothing exposed yet, still worth fixing
            detail = _rule_text(rule) + "  · " + t("ports: {count}", count=count)
            out.append(Finding(severity, "security group", group.name, group.id, issue, detail))
    return _sorted(out)


def list_security_audit(conn: Connection, _q: dict[str, Any]) -> list[Finding]:
    lists, errors = fetch_lists(
        conn,
        {
            "security groups": lambda c: c.network.security_groups(),
            "ports": lambda c: c.network.ports(),
        },
    )
    return errors + security_findings(lists["security groups"], lists["ports"])


# --- unused resources ------------------------------------------------------------


def unused_findings(lists: dict[str, list[Any]], now: datetime | None = None) -> list[Finding]:
    out: list[Finding] = []

    def add(severity: str, kind: str, item: Any, issue: str, **extra: Any) -> None:
        name = attr(item, "name") or attr(item, "floating_ip_address") or ""
        out.append(Finding(severity, kind, name, item.id, issue, resource=item, **extra))

    for fip in lists.get("floating IPs", []):
        if not attr(fip, "port_id"):
            add(
                "MEDIUM",
                "floating IP",
                fip,
                t("not associated"),
                age_days=age_days(fip.updated_at, now),
            )
    for vol in lists.get("volumes", []):
        if attr(vol, "status") == "available" and not attr(vol, "attachments"):
            add(
                "MEDIUM",
                "volume",
                vol,
                t("not attached"),
                age_days=age_days(attr(vol, "updated_at") or attr(vol, "created_at"), now),
                size=attr(vol, "size"),
                detail=attr(vol, "volume_type") or "",
            )
    for snap in lists.get("snapshots", []):
        days = age_days(attr(snap, "created_at"), now)
        if days is not None and days >= OLD_SNAPSHOT_DAYS:
            add(
                "LOW",
                "volume snapshot",
                snap,
                t("older than {days} days", days=OLD_SNAPSHOT_DAYS),
                age_days=days,
                size=attr(snap, "size"),
            )
    for server in lists.get("servers", []):
        days = age_days(attr(server, "updated_at"), now)
        if attr(server, "status") in STOPPED and days is not None and days >= STOPPED_DAYS:
            add(
                "LOW",
                "server",
                server,
                t("{status} since {days} days", status=server.status, days=days),
                age_days=days,
            )
    ports = lists.get("ports", [])
    for port in ports:
        owner = attr(port, "device_owner") or ""
        if not owner and not attr(port, "device_id"):
            add(
                "LOW",
                "port",
                port,
                t("no device"),
                detail=", ".join(ip["ip_address"] for ip in attr(port, "fixed_ips") or []),
                age_days=age_days(attr(port, "updated_at"), now),
            )
    router_ports = Counter(
        attr(p, "device_id")
        for p in ports
        if (attr(p, "device_owner") or "").startswith("network:router_interface")
    )
    for router in lists.get("routers", []):
        if attr(router, "external_gateway_info") and not router_ports.get(router.id):
            add("LOW", "router", router, t("gateway set but no interfaces"))
    used = sg_usage(ports)
    for group in lists.get("security groups", []):
        if group.name != "default" and not used.get(group.id):
            add("LOW", "security group", group, t("not used by any port"))
    return _sorted(out)


UNUSED_JOBS: dict[str, Callable[[Connection], Iterable[Any]]] = {
    "floating IPs": lambda c: c.network.ips(),
    "volumes": lambda c: c.block_storage.volumes(),
    "snapshots": lambda c: c.block_storage.snapshots(),
    "servers": lambda c: c.compute.servers(),
    "ports": lambda c: c.network.ports(),
    "routers": lambda c: c.network.routers(),
    "security groups": lambda c: c.network.security_groups(),
}


def list_unused(conn: Connection, _q: dict[str, Any]) -> list[Finding]:
    lists, errors = fetch_lists(conn, UNUSED_JOBS)
    return errors + unused_findings(lists)


# Delete call for each finding type (servers are left to the server view).
DELETERS: dict[str, Callable[[Connection, Any], Any]] = {
    "floating IP": lambda c, r: c.network.delete_ip(r),
    "volume": lambda c, r: c.block_storage.delete_volume(r),
    "volume snapshot": lambda c, r: c.block_storage.delete_snapshot(r),
    "port": lambda c, r: c.network.delete_port(r),
    "security group": lambda c, r: c.network.delete_security_group(r),
}


def delete_finding(conn: Connection, item: Finding, _v: dict[str, Any]) -> str:
    deleter = DELETERS.get(item.type)
    if deleter is None or item.resource is None:
        raise ValueError(t("Delete a {type} from its own view", type=t(item.type)))
    deleter(conn, item.resource)
    return t("{what} {name} deleted", what=t(item.type), name=item.name or item.id)


# --- views ----------------------------------------------------------------------

FINDING_COLUMNS = [
    Column("Severity", "severity"),
    Column("Type", lambda f: t(f.type)),
    Column("Name", "name"),
    Column("Issue", "issue"),
    Column("Detail", "detail"),
]

UNUSED = ResourceKind(
    key="checks.unused",
    title="Unused resources",
    service="network",
    aliases=("unused", "waste", "cleanup"),
    status="severity",
    list=list_unused,
    columns=[
        *FINDING_COLUMNS,
        Column("Size GiB", "size"),
        Column("Age (days)", "age_days"),
    ],
    actions=[
        Action("ctrl+d", "Delete", delete_finding, confirm=True, destructive=True),
    ],
)

SECURITY_AUDIT = ResourceKind(
    key="checks.security",
    title="Security group audit",
    service="network",
    aliases=("audit", "sg-audit", "exposed"),
    status="severity",
    list=list_security_audit,
    columns=FINDING_COLUMNS,
)

KINDS = [UNUSED, SECURITY_AUDIT]
