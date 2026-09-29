"""Neutron resources: networks, subnets, routers, ports, floating IPs, security groups."""

from __future__ import annotations

from collections import Counter
from typing import Any

from openstack.connection import Connection

from ..i18n import t
from .base import Action, Child, Column, Field, Options, ResourceKind, attr, by_name, none_if_empty


def external_network_options(conn: Connection, _item: Any) -> Options:
    return by_name(conn.network.networks(is_router_external=True))


def network_options(conn: Connection, _item: Any) -> Options:
    return by_name(conn.network.networks())


def subnet_options(conn: Connection, _item: Any) -> Options:
    return by_name(conn.network.subnets(), lambda s: f"{s.name or s.id}  {s.cidr}")


def router_subnet_options(conn: Connection, router: Any) -> Options:
    ids = set()
    for port in conn.network.ports(device_id=router.id):
        for ip in port.fixed_ips or []:
            ids.add(ip["subnet_id"])
    subnets = (conn.network.get_subnet(i) for i in ids)
    return by_name(subnets, lambda s: f"{s.name or s.id}  {s.cidr}")


def port_options(conn: Connection, _item: Any) -> Options:
    out = []
    for p in conn.network.ports():
        if p.device_owner and not p.device_owner.startswith("compute:"):
            continue
        ips = ", ".join(ip["ip_address"] for ip in p.fixed_ips or [])
        out.append((f"{ips}  {p.name or p.device_id[:8] or p.id[:8]}", p.id))
    return out


def sg_options(conn: Connection, _item: Any) -> Options:
    return [(t("(none)"), ""), *by_name(conn.network.security_groups())]


def choices(*values: str) -> Any:
    return lambda _c, _i: [(v, v) for v in values]


def fixed_ips(port: Any) -> str:
    return ", ".join(ip["ip_address"] for ip in attr(port, "fixed_ips") or [])


def address_pairs(port: Any) -> str:
    pairs = attr(port, "allowed_address_pairs") or []
    return ", ".join(p.get("ip_address", "") for p in pairs)


def trunk_info(port: Any) -> str:
    """Trunk parent ports: number of subports and their VLANs."""
    details = attr(port, "trunk_details")
    if not details:
        return ""
    vlans = sorted(
        str(s.get("segmentation_id"))
        for s in details.get("sub_ports") or []
        if s.get("segmentation_id")
    )
    return t("parent, VLAN {vlans}", vlans=",".join(vlans)) if vlans else t("parent")


def list_security_groups(conn: Connection, q: dict[str, Any]) -> Any:
    """Security groups with the number of ports using them (Horizon does not show it)."""
    groups: list[Any] = list(conn.network.security_groups(**q))
    used: Counter[str] = Counter()
    try:
        for port in conn.network.ports():
            used.update(port.security_group_ids or [])
    except Exception:  # noqa: BLE001 - the list is still useful without the counts
        return groups
    for group in groups:
        group.port_count = used.get(group.id, 0)
    return groups


def rule_ports(rule: Any) -> str:
    lo, hi = attr(rule, "port_range_min"), attr(rule, "port_range_max")
    if lo is None and hi is None:
        return "any"
    return str(lo) if lo == hi else f"{lo}-{hi}"


def rule_remote(rule: Any) -> str:
    return attr(rule, "remote_ip_prefix") or attr(rule, "remote_group_id") or "any"


def _delete(method: str, what: str) -> Any:
    def run(conn: Connection, item: Any, _v: dict[str, Any]) -> str:
        getattr(conn.network, method)(item)
        return t("{what} {name} deleted", what=t(what), name=attr(item, "name") or item.id)

    return run


def _rename(method: str) -> Any:
    def run(conn: Connection, item: Any, v: dict[str, Any]) -> str:
        attrs = {"name": v["name"]}
        if "description" in v:
            attrs["description"] = v.get("description") or ""
        getattr(conn.network, method)(item, **attrs)
        return t("Updated")

    return run


def _name_fields() -> list[Field]:
    return [
        Field("name", "Name", required=True, default=lambda i: i.name),
        Field("description", "Description", default=lambda i: i.description),
    ]


# --- actions ------------------------------------------------------------------


def create_network(conn: Connection, _item: Any, v: dict[str, Any]) -> str:
    net = conn.network.create_network(
        name=v["name"], is_admin_state_up=bool(v.get("admin_up", True))
    )
    msg = t("Network {name} created", name=net.name)
    if v.get("cidr"):
        conn.network.create_subnet(
            network_id=net.id,
            name=v.get("subnet_name") or f"{v['name']}-subnet",
            cidr=v["cidr"],
            ip_version=6 if ":" in v["cidr"] else 4,
            enable_dhcp=bool(v.get("dhcp", True)),
        )
        msg = t("Network {name} created with a subnet", name=net.name)
    return msg


def create_subnet(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    dns = [d.strip() for d in (v.get("dns") or "").split(",") if d.strip()]
    attrs: dict[str, Any] = {
        "network_id": v.get("network") or (item.id if item is not None else None),
        "name": v["name"],
        "cidr": v["cidr"],
        "ip_version": 6 if ":" in v["cidr"] else 4,
        "enable_dhcp": bool(v.get("dhcp", True)),
    }
    if dns:
        attrs["dns_nameservers"] = dns
    if v.get("no_gateway"):
        attrs["gateway_ip"] = None
    elif v.get("gateway"):
        attrs["gateway_ip"] = v["gateway"]
    subnet = conn.network.create_subnet(**attrs)
    return t("Subnet {name} created", name=subnet.name)


def create_router(conn: Connection, _item: Any, v: dict[str, Any]) -> str:
    attrs: dict[str, Any] = {"name": v["name"]}
    if v.get("external"):
        attrs["external_gateway_info"] = {"network_id": v["external"]}
    router = conn.network.create_router(**attrs)
    return t("Router {name} created", name=router.name)


def set_gateway(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    conn.network.update_router(item, external_gateway_info={"network_id": v["external"]})
    return t("Gateway set")


def clear_gateway(conn: Connection, item: Any, _v: dict[str, Any]) -> str:
    conn.network.update_router(item, external_gateway_info={})
    return t("Gateway cleared")


def add_router_interface(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    conn.network.add_interface_to_router(item, subnet=v["subnet"])
    return t("Interface added")


def remove_router_interface(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    conn.network.remove_interface_from_router(item, subnet=v["subnet"])
    return t("Interface removed")


def allocate_fip(conn: Connection, _item: Any, v: dict[str, Any]) -> str:
    fip = conn.network.create_ip(
        floating_network_id=v["network"], description=none_if_empty(v.get("description"))
    )
    return t("Floating IP {ip} allocated", ip=fip.floating_ip_address)


def associate_fip(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    conn.network.update_ip(item, port_id=v["port"])
    return t("{ip} associated", ip=item.floating_ip_address)


def disassociate_fip(conn: Connection, item: Any, _v: dict[str, Any]) -> str:
    conn.network.update_ip(item, port_id=None)
    return t("{ip} disassociated", ip=item.floating_ip_address)


def release_fip(conn: Connection, item: Any, _v: dict[str, Any]) -> str:
    conn.network.delete_ip(item)
    return t("{ip} released", ip=item.floating_ip_address)


def set_address_pairs(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    pairs = []
    for entry in (v.get("pairs") or "").replace("\n", ",").split(","):
        ip, _, mac = entry.strip().partition(" ")
        if ip:
            pairs.append(
                {"ip_address": ip, **({"mac_address": mac.strip()} if mac.strip() else {})}
            )
    conn.network.update_port(item, allowed_address_pairs=pairs)
    return t("Allowed address pairs updated ({count})", count=len(pairs))


def set_port_security(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    enabled = bool(v.get("enabled"))
    attrs: dict[str, Any] = {"is_port_security_enabled": enabled}
    if not enabled:
        # Neutron refuses to disable port security while groups are attached.
        attrs["security_group_ids"] = []
    conn.network.update_port(item, **attrs)
    return t("Port security enabled") if enabled else t("Port security disabled")


def create_rbac(conn: Connection, _item: Any, v: dict[str, Any]) -> str:
    conn.network.create_rbac_policy(
        object_type="network",
        object_id=v["network"],
        action=v["action"],
        target_project_id=v["target"].strip(),
    )
    return t("Network shared with {target}", target=v["target"].strip())


def delete_rbac(conn: Connection, item: Any, _v: dict[str, Any]) -> str:
    conn.network.delete_rbac_policy(item)
    return t("Sharing with {target} removed", target=item.target_project_id)


RBAC_ACTIONS = ("access_as_shared", "access_as_external")


def create_sg(conn: Connection, _item: Any, v: dict[str, Any]) -> str:
    sg = conn.network.create_security_group(name=v["name"], description=v.get("description") or "")
    return t("Security group {name} created", name=sg.name)


def create_rule(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    sg_id = v.get("security_group") or item_sg_id(item)
    proto = v.get("protocol") or None
    if proto == "any":
        proto = None
    attrs: dict[str, Any] = {
        "security_group_id": sg_id,
        "direction": v.get("direction") or "ingress",
        "ethertype": v.get("ethertype") or "IPv4",
        "protocol": proto,
        "description": v.get("description") or "",
    }
    if v.get("port_min") not in (None, ""):
        attrs["port_range_min"] = int(v["port_min"])
        attrs["port_range_max"] = int(v.get("port_max") or v["port_min"])
    if v.get("remote_group"):
        attrs["remote_group_id"] = v["remote_group"]
    elif v.get("remote_ip"):
        attrs["remote_ip_prefix"] = v["remote_ip"]
    conn.network.create_security_group_rule(**attrs)
    return t("Rule created")


def item_sg_id(item: Any) -> str | None:
    """Reference security group: the parent (when the item is a rule) or the item."""
    if item is None:
        return None
    return attr(item, "security_group_id") or attr(item, "id")


RULE_FIELDS = [
    Field("direction", "Direction", "select", True, options=choices("ingress", "egress")),
    Field("ethertype", "Ethertype", "select", True, options=choices("IPv4", "IPv6")),
    Field(
        "protocol",
        "Protocol",
        "select",
        True,
        options=choices("tcp", "udp", "icmp", "any", "ipv6-icmp", "gre", "esp", "vrrp"),
    ),
    Field("port_min", "Port (or range start)", "int"),
    Field("port_max", "Port range end", "int"),
    Field("remote_ip", "Remote CIDR", default="0.0.0.0/0"),
    Field("remote_group", "Remote security group", "select", options=sg_options),
    Field("description", "Description"),
]


# --- definitions --------------------------------------------------------------

NETWORK = ResourceKind(
    key="network.network",
    title="Networks",
    service="network",
    aliases=("networks", "nets"),
    list=lambda conn, q: conn.network.networks(**q),
    enter="s",
    columns=[
        Column("Name", "name"),
        Column("Status", "status"),
        Column("Subnets", lambda n: len(attr(n, "subnet_ids") or [])),
        Column("Shared", "is_shared"),
        Column("External", "is_router_external"),
        Column("MTU", "mtu"),
        Column("Project", "project_id"),
    ],
    children=[
        Child("s", "Subnets", "network.subnet", lambda n: {"network_id": n.id}),
        Child("w", "Ports", "network.port", lambda n: {"network_id": n.id}),
        Child("b", "Sharing (RBAC)", "network.rbac_policy", lambda n: {"object_id": n.id}),
    ],
    actions=[
        Action(
            "N",
            "Create network",
            create_network,
            needs_item=False,
            fields=[
                Field("name", "Name", required=True),
                Field("admin_up", "Admin state up", "bool", default=True),
                Field("cidr", "Subnet CIDR (empty = no subnet)"),
                Field("subnet_name", "Subnet name"),
                Field("dhcp", "DHCP", "bool", default=True),
            ],
        ),
        Action("n", "Edit", _rename("update_network"), fields=_name_fields()),
        Action(
            "ctrl+d", "Delete", _delete("delete_network", "Network"), confirm=True, destructive=True
        ),
    ],
)

SUBNET_FIELDS = [
    Field("name", "Name", required=True),
    Field("cidr", "CIDR", required=True),
    Field("gateway", "Gateway IP (empty = automatic)"),
    Field("no_gateway", "No gateway", "bool"),
    Field("dhcp", "DHCP", "bool", default=True),
    Field("dns", "DNS servers (comma separated)"),
]

SUBNET = ResourceKind(
    key="network.subnet",
    title="Subnets",
    service="network",
    aliases=("subnets",),
    status=None,
    list=lambda conn, q: conn.network.subnets(**q),
    columns=[
        Column("Name", "name"),
        Column("CIDR", "cidr"),
        Column("Gateway", "gateway_ip"),
        Column("DHCP", "is_dhcp_enabled"),
        Column("Network", "network_id"),
        Column("DNS", "dns_nameservers"),
    ],
    actions=[
        Action(
            "N",
            "Create subnet",
            create_subnet,
            needs_item=False,
            fields=[
                Field(
                    "network",
                    "Network",
                    "select",
                    True,
                    default=lambda parent: attr(parent, "id"),
                    options=network_options,
                ),
                *SUBNET_FIELDS,
            ],
        ),
        Action("n", "Edit", _rename("update_subnet"), fields=_name_fields()),
        Action(
            "ctrl+d", "Delete", _delete("delete_subnet", "Subnet"), confirm=True, destructive=True
        ),
    ],
)

ROUTER = ResourceKind(
    key="network.router",
    title="Routers",
    service="network",
    aliases=("routers",),
    enter="w",
    list=lambda conn, q: conn.network.routers(**q),
    columns=[
        Column("Name", "name"),
        Column("Status", "status"),
        Column(
            "External gateway",
            lambda r: attr(r, "external_gateway_info.network_id") or "",
        ),
        Column(
            "Gateway IP",
            lambda r: ", ".join(
                ip["ip_address"] for ip in attr(r, "external_gateway_info.external_fixed_ips") or []
            ),
        ),
        Column("HA", "is_ha"),
    ],
    children=[
        Child("w", "Interfaces (ports)", "network.port", lambda r: {"device_id": r.id}),
    ],
    actions=[
        Action(
            "N",
            "Create router",
            create_router,
            needs_item=False,
            fields=[
                Field("name", "Name", required=True),
                Field("external", "External network", "select", options=external_network_options),
            ],
        ),
        Action(
            "g",
            "Set gateway",
            set_gateway,
            fields=[
                Field(
                    "external", "External network", "select", True, options=external_network_options
                )
            ],
        ),
        Action("G", "Clear gateway", clear_gateway, confirm=True),
        Action(
            "i",
            "Add interface",
            add_router_interface,
            fields=[Field("subnet", "Subnet", "select", True, options=subnet_options)],
        ),
        Action(
            "I",
            "Remove interface",
            remove_router_interface,
            confirm=True,
            fields=[Field("subnet", "Subnet", "select", True, options=router_subnet_options)],
        ),
        Action("n", "Edit", _rename("update_router"), fields=_name_fields()),
        Action(
            "ctrl+d", "Delete", _delete("delete_router", "Router"), confirm=True, destructive=True
        ),
    ],
)

PORT = ResourceKind(
    key="network.port",
    title="Ports",
    service="network",
    aliases=("ports",),
    list=lambda conn, q: conn.network.ports(**q),
    columns=[
        Column("Name", "name"),
        Column("Status", "status"),
        Column("Fixed IPs", fixed_ips),
        Column("MAC", "mac_address"),
        Column("Owner", "device_owner"),
        Column("Device", "device_id"),
        Column("Network", "network_id"),
        Column("Port security", "is_port_security_enabled"),
        Column("Security groups", lambda p: len(attr(p, "security_group_ids") or [])),
        Column("Allowed address pairs", address_pairs),
        Column("Trunk", trunk_info),
        Column("QoS policy", "qos_policy_id"),
    ],
    actions=[
        Action("n", "Edit", _rename("update_port"), fields=_name_fields()),
        Action(
            "p",
            "Set allowed address pairs",
            set_address_pairs,
            confirm=True,
            fields=[
                Field(
                    "pairs",
                    "Address pairs",
                    "textarea",
                    default=lambda i: "\n".join(
                        " ".join(filter(None, (p.get("ip_address"), p.get("mac_address"))))
                        for p in attr(i, "allowed_address_pairs") or []
                    ),
                    help="One per line: IP or CIDR, optionally followed by a MAC",
                )
            ],
        ),
        Action(
            "s",
            "Set port security",
            set_port_security,
            confirm=True,
            fields=[
                Field(
                    "enabled",
                    "Port security enabled",
                    "bool",
                    default=lambda i: attr(i, "is_port_security_enabled", True),
                    help="Disabling it also removes the security groups of the port",
                )
            ],
        ),
        Action("ctrl+d", "Delete", _delete("delete_port", "Port"), confirm=True, destructive=True),
    ],
)

FLOATING_IP = ResourceKind(
    key="network.floating_ip",
    title="Floating IPs",
    service="network",
    aliases=("floating-ips", "fip", "fips"),
    list=lambda conn, q: conn.network.ips(**q),
    columns=[
        Column("Address", "floating_ip_address"),
        Column("Status", "status"),
        Column("Fixed IP", "fixed_ip_address"),
        Column("Port", "port_id"),
        Column("Network", "floating_network_id"),
        Column("Description", "description"),
    ],
    actions=[
        Action(
            "N",
            "Allocate floating IP",
            allocate_fip,
            needs_item=False,
            fields=[
                Field(
                    "network",
                    "Pool (external network)",
                    "select",
                    True,
                    options=external_network_options,
                ),
                Field("description", "Description"),
            ],
        ),
        Action(
            "f",
            "Associate",
            associate_fip,
            fields=[Field("port", "Port", "select", True, options=port_options)],
        ),
        Action("F", "Disassociate", disassociate_fip, confirm=True),
        Action("ctrl+d", "Release", release_fip, confirm=True, destructive=True),
    ],
)

SECURITY_GROUP = ResourceKind(
    key="network.security_group",
    title="Security groups",
    service="network",
    aliases=("security-groups", "sg", "secgroups"),
    status=None,
    enter="r",
    list=list_security_groups,
    columns=[
        Column("Name", "name"),
        Column("Rules", lambda g: len(attr(g, "security_group_rules") or [])),
        Column("Ports", "port_count"),
        Column("Stateful", "stateful"),
        Column("Description", "description"),
    ],
    children=[
        Child(
            "r",
            "Rules",
            "network.security_group_rule",
            lambda g: {"security_group_id": g.id},
        ),
        Child("w", "Ports using it", "network.port", lambda g: {"security_groups": [g.id]}),
    ],
    actions=[
        Action(
            "N",
            "Create security group",
            create_sg,
            needs_item=False,
            fields=[Field("name", "Name", required=True), Field("description", "Description")],
        ),
        Action("A", "Add rule", create_rule, fields=RULE_FIELDS),
        Action("n", "Edit", _rename("update_security_group"), fields=_name_fields()),
        Action(
            "ctrl+d",
            "Delete",
            _delete("delete_security_group", "Security group"),
            confirm=True,
            destructive=True,
        ),
    ],
)

SECURITY_GROUP_RULE = ResourceKind(
    key="network.security_group_rule",
    title="Security group rules",
    service="network",
    aliases=("rules", "sg-rules"),
    status=None,
    list=lambda conn, q: conn.network.security_group_rules(**q),
    columns=[
        Column("Direction", "direction"),
        Column("Ethertype", "ethertype"),
        Column("Protocol", lambda r: attr(r, "protocol") or "any"),
        Column("Ports", rule_ports),
        Column("Remote", rule_remote),
        Column("Description", "description"),
    ],
    actions=[
        Action(
            "N",
            "Add rule",
            create_rule,
            needs_item=False,
            fields=[
                Field(
                    "security_group",
                    "Security group",
                    "select",
                    True,
                    default=item_sg_id,
                    options=lambda c, i: by_name(c.network.security_groups()),
                ),
                *RULE_FIELDS,
            ],
        ),
        Action(
            "ctrl+d",
            "Delete",
            _delete("delete_security_group_rule", "Rule"),
            confirm=True,
            destructive=True,
        ),
    ],
)

RBAC_POLICY = ResourceKind(
    key="network.rbac_policy",
    title="RBAC policies",
    service="network",
    aliases=("rbac", "sharing"),
    status=None,
    list=lambda conn, q: conn.network.rbac_policies(**q),
    columns=[
        Column("Type", "object_type"),
        Column("Object", "object_id"),
        Column("Action", "action"),
        Column("Target project", "target_project_id"),
        Column("Owner project", "project_id"),
    ],
    actions=[
        Action(
            "N",
            "Share network",
            create_rbac,
            needs_item=False,
            fields=[
                Field(
                    "network",
                    "Network",
                    "select",
                    True,
                    default=lambda parent: attr(parent, "id"),
                    options=network_options,
                ),
                Field("action", "Access", "select", True, options=choices(*RBAC_ACTIONS)),
                Field(
                    "target",
                    "Target project ID",
                    required=True,
                    help="* = every project",
                ),
            ],
        ),
        Action("ctrl+d", "Stop sharing", delete_rbac, confirm=True, destructive=True),
    ],
)

KINDS = [
    NETWORK,
    SUBNET,
    ROUTER,
    PORT,
    FLOATING_IP,
    SECURITY_GROUP,
    SECURITY_GROUP_RULE,
    RBAC_POLICY,
]
