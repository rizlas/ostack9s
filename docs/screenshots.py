"""Generate the README screenshots from fake data, in privacy mode.

Run with ``uv run python docs/screenshots.py``: it writes SVG files into
``docs/img/``. No cloud is contacted, every value below is made up.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from ostack9s import privacy
from ostack9s.cloud import Context, Project, Target
from ostack9s.ui.app import OstdApp

OUT = Path(__file__).parent / "img"
SIZE = (150, 38)


def uid(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{name}.example.org"))


def ns(name: str, **kw) -> SimpleNamespace:
    obj = SimpleNamespace(id=uid(name), name=name, **kw)
    obj.to_dict = lambda: {k: v for k, v in vars(obj).items() if k != "to_dict"}
    return obj


def flavor(name: str, gpu: str | None = None) -> dict:
    specs = {"pci_passthrough:alias": gpu} if gpu else {}
    return {"original_name": name, "vcpus": 4, "ram": 8192, "extra_specs": specs}


def server(name, status, net, ip, fip, flv, gpu=None, key="foo-key", task=None):
    addrs = [{"addr": ip, "OS-EXT-IPS:type": "fixed"}]
    if fip:
        addrs.append({"addr": fip, "OS-EXT-IPS:type": "floating"})
    return ns(
        name,
        status=status,
        task_state=task,
        addresses={net: addrs},
        flavor=flavor(flv, gpu),
        key_name=key,
        availability_zone="nova",
        is_locked=False,
        created_at="2026-09-14T08:12:44Z",
        fault=None,
        user_id=uid("foo-user"),
        image={"id": uid("ubuntu-24.04")},
    )


SERVERS = [
    server("web-01", "ACTIVE", "frontend", "10.0.0.11", "203.0.113.21", "m1.medium"),
    server("web-02", "ACTIVE", "frontend", "10.0.0.12", "203.0.113.22", "m1.medium"),
    server("api-01", "ACTIVE", "backend", "10.0.1.20", None, "m1.large"),
    server("db-primary", "ACTIVE", "backend", "10.0.1.30", None, "m1.xlarge"),
    server("db-replica", "SHUTOFF", "backend", "10.0.1.31", None, "m1.xlarge"),
    server("train-a100", "ACTIVE", "gpu", "10.0.2.5", None, "g1.a100x2", "gpu_a100:2"),
    server("infer-l40s", "ACTIVE", "gpu", "10.0.2.6", "203.0.113.40", "g1.l40s", "gpu_l40s:1"),
    server("batch-07", "BUILD", "backend", "10.0.1.47", None, "m1.small", task="spawning"),
    server("bastion", "ACTIVE", "frontend", "10.0.0.5", "198.51.100.7", "m1.small"),
]


def volume(name, size, status="in-use", server_name=None, bootable=False):
    att = [{"server_id": uid(server_name), "device": "/dev/vdb"}] if server_name else []
    return ns(
        name,
        status=status,
        size=size,
        volume_type="ssd",
        is_bootable=bootable,
        attachments=att,
        availability_zone="nova",
        created_at="2026-08-02T10:40:00Z",
    )


VOLUMES = [
    volume("db-primary-data", 500, server_name="db-primary"),
    volume("db-replica-data", 500, server_name="db-replica"),
    volume("datasets", 2000, server_name="train-a100"),
    volume("web-shared", 50, server_name="web-01"),
    volume("old-backup", 200, status="available"),
]


def network(name, external=False):
    return ns(
        name,
        status="ACTIVE",
        subnet_ids=[uid(name + "-subnet")],
        is_shared=external,
        is_router_external=external,
        mtu=1450,
        project_id=uid("proj-foo"),
    )


NETS = [network("public", True), network("frontend"), network("backend"), network("gpu")]
CIDRS = {
    "public": "203.0.113.0/24",
    "frontend": "10.0.0.0/24",
    "backend": "10.0.1.0/24",
    "gpu": "10.0.2.0/24",
}
SUBNETS = [
    SimpleNamespace(
        id=uid(n.name + "-subnet"),
        name=n.name + "-subnet",
        network_id=n.id,
        cidr=CIDRS[n.name],
        gateway_ip=CIDRS[n.name].replace("0/24", "1"),
        is_dhcp_enabled=True,
        dns_nameservers=["9.9.9.9"],
    )
    for n in NETS
]
NET_BY_NAME = {n.name: n for n in NETS}
ROUTER = SimpleNamespace(
    id=uid("router-main"),
    name="router-main",
    status="ACTIVE",
    external_gateway_info={
        "network_id": NET_BY_NAME["public"].id,
        "external_fixed_ips": [{"ip_address": "203.0.113.1"}],
    },
)


def ports():
    out = []
    for s in SERVERS:
        ((net, addrs),) = s.addresses.items()
        sub = next(x for x in SUBNETS if x.network_id == NET_BY_NAME[net].id)
        out.append(
            SimpleNamespace(
                id=uid(s.name + "-port"),
                network_id=NET_BY_NAME[net].id,
                device_owner="compute:nova",
                device_id=s.id,
                fixed_ips=[{"ip_address": addrs[0]["addr"], "subnet_id": sub.id}],
            )
        )
    for n in ("frontend", "backend", "gpu"):
        sub = next(x for x in SUBNETS if x.network_id == NET_BY_NAME[n].id)
        out.append(
            SimpleNamespace(
                id=uid(n + "-rport"),
                network_id=NET_BY_NAME[n].id,
                device_owner="network:router_interface",
                device_id=ROUTER.id,
                fixed_ips=[{"ip_address": sub.gateway_ip, "subnet_id": sub.id}],
            )
        )
    return out


PORTS = ports()
FIPS = [
    SimpleNamespace(
        id=uid(s.name + "-fip"),
        floating_ip_address=addrs[1]["addr"],
        fixed_ip_address=addrs[0]["addr"],
        port_id=uid(s.name + "-port"),
        status="ACTIVE",
        floating_network_id=NET_BY_NAME["public"].id,
    )
    for s in SERVERS
    for addrs in s.addresses.values()
    if len(addrs) > 1
]


def fake_conn(scale: float = 1.0) -> MagicMock:
    """Connection with the demo data; ``scale`` shrinks it for the other contexts."""
    n = round(len(SERVERS) * scale)
    servers = SERVERS[:n] if scale < 1 else SERVERS

    def used(value: int) -> int:
        return round(value * scale)

    conn = MagicMock()
    conn.compute.servers.side_effect = lambda **kw: list(servers)
    conn.compute.get_limits.return_value.absolute = SimpleNamespace(
        instances_used=n,
        instances=20,
        total_cores_used=used(46),
        total_cores=64,
        total_ram_used=used(180) * 1024,
        total_ram=256 * 1024,
    )
    conn.block_storage.volumes.side_effect = lambda **kw: list(VOLUMES)
    conn.block_storage.get_quota_set.return_value = SimpleNamespace(
        usage={"volumes": used(5), "gigabytes": used(3250)}, volumes=20, gigabytes=4000
    )
    conn.network.get_quota.return_value = SimpleNamespace(
        floating_ips={"used": used(4), "limit": 5},
        networks={"used": used(3), "limit": 10},
        security_groups={"used": used(6), "limit": 10},
    )
    conn.object_store.get_account_metadata.return_value = SimpleNamespace(
        account_bytes_used=used(120) * 2**30, metadata={"quota-bytes": str(500 * 2**30)}
    )
    conn.network.networks.side_effect = lambda **kw: list(NETS)
    conn.network.subnets.side_effect = lambda **kw: list(SUBNETS)
    conn.network.routers.side_effect = lambda **kw: [ROUTER]
    conn.network.ports.side_effect = lambda **kw: list(PORTS)
    conn.network.ips.side_effect = lambda **kw: list(FIPS)
    return conn


PROJECTS = [Project(uid(n), n) for n in ("proj-foo", "proj-bar", "proj-baz")]


class DemoManager:
    SCALES = {
        ("proj-foo", "region-a"): 1.0,
        ("proj-foo", "region-b"): 0.4,
        ("proj-bar", "region-a"): 0.7,
        ("proj-bar", "region-b"): 0.2,
        ("proj-baz", "region-a"): 0.5,
        ("proj-baz", "region-b"): 0.0,
    }

    def __init__(self) -> None:
        self.conns = {k: fake_conn(v) for k, v in self.SCALES.items()}

    def cloud_names(self):
        return ["acme"]

    def context(self, cloud, project_id=None, region=None):
        project = next((p for p in PROJECTS if p.id == project_id), PROJECTS[0])
        return Context(cloud, project.id, project.name, region or "region-a")

    def regions(self, cloud, project_id):
        return ["region-a", "region-b"]

    def connection(self, ctx):
        return self.conns[(ctx.project_name, ctx.region)]

    def is_project_locked(self, cloud):
        return False

    def user_name(self, cloud):
        return "foo@example.com"

    def auth_type(self, cloud):
        return "password"

    def projects(self, cloud):
        return list(PROJECTS)

    def targets(self, cloud):
        return [Target(cloud, p) for p in PROJECTS]

    def needs_password(self, cloud):
        return False

    def set_password(self, cloud, password):
        pass

    def all_contexts(self):
        return [
            Context("acme", p.id, p.name, r) for p in PROJECTS for r in ("region-a", "region-b")
        ]


async def shoot(name: str, keys: list[str], pause: float = 1.0) -> None:
    privacy.set_enabled(True)
    app = OstdApp(DemoManager(), refresh=0)  # type: ignore[arg-type]
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(pause)
        for key in keys:
            await pilot.press(key)
            await pilot.pause(0.3)
        await pilot.pause(pause)
        app.save_screenshot(f"{name}.svg", path=str(OUT))
    print(OUT / f"{name}.svg")


SHOTS = {
    "servers": [],
    "describe": ["down", "down", "down", "d"],
    "volumes": ["colon", *"volumes", "enter"],
    "topology": ["colon", *"topology", "enter"],
    "overview": ["f1"],
}


async def main() -> None:
    OUT.mkdir(exist_ok=True)
    names = sys.argv[1:] or list(SHOTS)
    for name in names:
        await shoot(name, SHOTS[name])


if __name__ == "__main__":
    asyncio.run(main())
