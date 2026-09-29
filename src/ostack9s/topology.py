"""Network topology of a project in a region.

The model links external networks, routers, internal networks and servers, the
same information Horizon shows in its "Network Topology" panel. It is rendered
as a tree in the terminal and can be exported to Mermaid or Graphviz DOT.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

from openstack.connection import Connection
from rich.text import Text
from rich.tree import Tree

from .i18n import t
from .resources.base import attr

ROUTER_INTERFACE_OWNERS = (
    "network:router_interface",
    "network:router_interface_distributed",
    "network:ha_router_replicated_interface",
)


@dataclass
class ServerPort:
    server: str
    status: str
    ips: list[str]
    floating: list[str]


@dataclass
class NetworkNode:
    id: str
    name: str
    external: bool
    shared: bool
    cidrs: list[str]
    servers: list[ServerPort] = field(default_factory=list)
    other_ports: dict[str, int] = field(default_factory=dict)


@dataclass
class RouterNode:
    id: str
    name: str
    status: str
    gateway_network: str | None
    gateway_ips: list[str]
    networks: list[str] = field(default_factory=list)


@dataclass
class Topology:
    title: str
    networks: dict[str, NetworkNode]
    routers: list[RouterNode]
    warnings: list[str] = field(default_factory=list)

    def routed_networks(self) -> set[str]:
        return {n for r in self.routers for n in r.networks}

    def detached_networks(self) -> list[NetworkNode]:
        """Networks with no router of the project (isolated or provider networks)."""
        routed = self.routed_networks()
        gateways = {r.gateway_network for r in self.routers}
        out = []
        for net in self.networks.values():
            if net.id in routed or net.id in gateways:
                continue
            if net.external and not net.servers:
                continue
            if net.shared and not net.servers and not net.other_ports:
                continue
            out.append(net)
        return sorted(out, key=lambda n: n.name.lower())


def _owner_kind(owner: str) -> str:
    if owner.startswith("network:dhcp"):
        return "dhcp"
    if "octavia" in owner or owner.startswith("Octavia"):
        return "load balancer"
    if owner.startswith("network:floatingip"):
        return "floating ip"
    if owner == "network:distributed":
        return "metadata"
    return owner or "unbound"


def build(conn: Connection, title: str) -> Topology:
    """Collect the topology with one list call per resource type."""
    warnings: list[str] = []
    networks = list(conn.network.networks())
    subnets = list(conn.network.subnets())
    routers = list(conn.network.routers())
    ports = list(conn.network.ports())
    fips = list(conn.network.ips())
    try:
        servers = {s.id: s for s in conn.compute.servers()}
    except Exception as exc:  # noqa: BLE001 - e.g. 403 on servers:detail
        servers = {}
        warnings.append(t("Server names unavailable: {error}", error=exc))

    cidrs: dict[str, list[str]] = defaultdict(list)
    subnet_network = {}
    for sn in subnets:
        cidrs[sn.network_id].append(sn.cidr)
        subnet_network[sn.id] = sn.network_id

    nodes = {
        n.id: NetworkNode(
            id=n.id,
            name=n.name or n.id[:8],
            external=bool(n.is_router_external),
            shared=bool(n.is_shared),
            cidrs=sorted(cidrs.get(n.id, [])),
        )
        for n in networks
    }
    floating_by_port: dict[str, list[str]] = defaultdict(list)
    for fip in fips:
        if fip.port_id:
            floating_by_port[fip.port_id].append(fip.floating_ip_address)

    router_nets: dict[str, set[str]] = defaultdict(set)
    for port in ports:
        owner = port.device_owner or ""
        net = nodes.get(port.network_id)
        if owner in ROUTER_INTERFACE_OWNERS:
            for ip in port.fixed_ips or []:
                net_id = subnet_network.get(ip["subnet_id"], port.network_id)
                router_nets[port.device_id].add(net_id)
            continue
        if net is None or owner == "network:router_gateway":
            continue
        if owner.startswith("compute:"):
            server = servers.get(port.device_id)
            net.servers.append(
                ServerPort(
                    server=(server.name if server else port.device_id[:8]),
                    status=(server.status if server else ""),
                    ips=[ip["ip_address"] for ip in port.fixed_ips or []],
                    floating=floating_by_port.get(port.id, []),
                )
            )
        else:
            kind = _owner_kind(owner)
            net.other_ports[kind] = net.other_ports.get(kind, 0) + 1

    router_nodes = []
    for r in routers:
        gw = attr(r, "external_gateway_info") or {}
        router_nodes.append(
            RouterNode(
                id=r.id,
                name=r.name or r.id[:8],
                status=r.status or "",
                gateway_network=gw.get("network_id"),
                gateway_ips=[ip["ip_address"] for ip in gw.get("external_fixed_ips") or []],
                networks=sorted(
                    router_nets.get(r.id, set()),
                    key=lambda n: nodes[n].name.lower() if n in nodes else n,
                ),
            )
        )
    for net in nodes.values():
        net.servers.sort(key=lambda s: s.server.lower())
    router_nodes.sort(key=lambda r: r.name.lower())
    return Topology(title, nodes, router_nodes, warnings)


# --- terminal rendering ------------------------------------------------------


def _net_label(net: NetworkNode, m: Callable[[str], str]) -> Text:
    text = Text("▤ ", style="bold cyan")
    text.append(m(net.name), style="bold cyan")
    if net.cidrs:
        text.append(f"  {m(', '.join(net.cidrs))}", style="grey70")
    flags = [t("external")] if net.external else []
    if net.shared:
        flags.append(t("shared"))
    if flags:
        text.append(f"  ({', '.join(flags)})", style="grey50")
    return text


def _server_label(port: ServerPort, m: Callable[[str], str]) -> Text:
    text = Text("▣ ", style="bold green")
    text.append(m(port.server), style="bold")
    text.append(f"  {m(', '.join(port.ips))}", style="grey70")
    if port.floating:
        text.append(f"  ⇢ {m(', '.join(port.floating))}", style="bold magenta")
    if port.status:
        style = (
            "green" if port.status == "ACTIVE" else "red" if port.status == "ERROR" else "grey62"
        )
        text.append(f"  {port.status}", style=style)
    return text


def _add_network(parent: Tree, net: NetworkNode, m: Callable[[str], str]) -> None:
    node = parent.add(_net_label(net, m))
    for port in net.servers:
        node.add(_server_label(port, m))
    for kind, count in sorted(net.other_ports.items()):
        node.add(Text(f"· {kind} × {count}", style="grey50"))


def _same(text: str) -> str:
    return text


def render(topo: Topology, m: Callable[[str], str] = _same) -> Tree:
    """Tree of the topology; ``m`` transforms every label (privacy masking)."""
    tree = Tree(Text(m(topo.title), style="bold"), guide_style="grey42")
    by_gateway: dict[str | None, list[RouterNode]] = defaultdict(list)
    for router in topo.routers:
        by_gateway[router.gateway_network].append(router)
    for gw_id, routers in sorted(by_gateway.items(), key=lambda kv: (kv[0] is None, str(kv[0]))):
        if gw_id is None:
            branch = tree.add(Text(t("Routers without gateway"), style="bold yellow"))
        else:
            ext = topo.networks.get(gw_id)
            label = Text("◎ ", style="bold magenta")
            label.append(m(ext.name if ext else gw_id), style="bold magenta")
            label.append(f"  ({t('external')})", style="grey50")
            branch = tree.add(label)
        for router in routers:
            label = Text("⇄ ", style="bold yellow")
            label.append(m(router.name), style="bold yellow")
            if router.gateway_ips:
                label.append(f"  gw {m(', '.join(router.gateway_ips))}", style="grey70")
            if router.status and router.status != "ACTIVE":
                label.append(f"  {router.status}", style="red")
            rnode = branch.add(label)
            for net_id in router.networks:
                net = topo.networks.get(net_id)
                if net is not None:
                    _add_network(rnode, net, m)
            if not router.networks:
                rnode.add(Text(t("no interfaces"), style="grey50"))
    detached = topo.detached_networks()
    if detached:
        branch = tree.add(Text(t("Networks without router"), style="bold"))
        for net in detached:
            _add_network(branch, net, m)
    if not topo.routers and not detached:
        tree.add(Text(t("No networks"), style="grey50"))
    for warning in topo.warnings:
        tree.add(Text(m(warning), style="yellow"))
    return tree


# --- export ------------------------------------------------------------------


def _mid(prefix: str, value: str) -> str:
    return prefix + re.sub(r"[^A-Za-z0-9]", "_", value)


def _q(text: str) -> str:
    return text.replace('"', "'")


def to_mermaid(topo: Topology) -> str:
    lines = ["graph LR"]
    used: set[str] = set()

    def net_node(net: NetworkNode) -> str:
        nid = _mid("n", net.id)
        if nid not in used:
            used.add(nid)
            cidr = "<br/>".join(net.cidrs)
            shape = f'(("{_q(net.name)}"))' if net.external else f'["{_q(net.name)}<br/>{cidr}"]'
            lines.append(f"  {nid}{shape}")
            for port in net.servers:
                sid = _mid("s", f"{port.server}_{net.id}")
                label = f"{_q(port.server)}<br/>{', '.join(port.ips)}"
                if port.floating:
                    label += f"<br/>⇢ {', '.join(port.floating)}"
                lines.append(f'  {sid}("{label}")')
                lines.append(f"  {nid} --- {sid}")
        return nid

    for router in topo.routers:
        rid = _mid("r", router.id)
        lines.append(f'  {rid}{{{{"{_q(router.name)}"}}}}')
        if router.gateway_network and router.gateway_network in topo.networks:
            lines.append(f"  {net_node(topo.networks[router.gateway_network])} --- {rid}")
        for net_id in router.networks:
            if net_id in topo.networks:
                lines.append(f"  {rid} --- {net_node(topo.networks[net_id])}")
    for net in topo.detached_networks():
        net_node(net)
    return "\n".join(lines) + "\n"


def to_dot(topo: Topology) -> str:
    lines = [
        "graph topology {",
        "  rankdir=LR;",
        '  node [fontname="Helvetica", fontsize=10];',
        f'  label="{_q(topo.title)}"; labelloc=t;',
    ]
    used: set[str] = set()

    def net_node(net: NetworkNode) -> str:
        nid = _mid("n", net.id)
        if nid not in used:
            used.add(nid)
            shape = "ellipse" if net.external else "box"
            label = "\\n".join([net.name, *net.cidrs])
            lines.append(f'  {nid} [shape={shape}, label="{_q(label)}"];')
            for port in net.servers:
                sid = _mid("s", f"{port.server}_{net.id}")
                parts = [port.server, *port.ips, *(f"FIP {f}" for f in port.floating)]
                label = "\\n".join(parts)
                lines.append(f'  {sid} [shape=component, label="{_q(label)}"];')
                lines.append(f"  {nid} -- {sid};")
        return nid

    for router in topo.routers:
        rid = _mid("r", router.id)
        lines.append(f'  {rid} [shape=hexagon, label="{_q(router.name)}"];')
        if router.gateway_network and router.gateway_network in topo.networks:
            lines.append(f"  {net_node(topo.networks[router.gateway_network])} -- {rid};")
        for net_id in router.networks:
            if net_id in topo.networks:
                lines.append(f"  {rid} -- {net_node(topo.networks[net_id])};")
    for net in topo.detached_networks():
        net_node(net)
    lines.append("}")
    return "\n".join(lines) + "\n"
