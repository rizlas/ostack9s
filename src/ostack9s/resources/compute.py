"""Nova resources: servers, flavors, key pairs, server groups."""

from __future__ import annotations

import base64
from typing import Any

from openstack.connection import Connection

from ..gpu import flavor_gpus, gpu_text, server_gpus
from ..i18n import t
from .base import (
    Action,
    Child,
    Column,
    Field,
    Options,
    ResourceKind,
    attr,
    by_name,
)

# --- form options -------------------------------------------------------------


def flavor_options(conn: Connection, _item: Any) -> Options:
    def label(f: Any) -> str:
        return f"{f.name}  ({f.vcpus} vCPU, {f.ram // 1024} GiB RAM, {f.disk} GiB disk)"

    return by_name(conn.compute.flavors(), label)


def image_options(conn: Connection, _item: Any) -> Options:
    return by_name(i for i in conn.image.images() if i.status == "active")


def network_options(conn: Connection, _item: Any) -> Options:
    return by_name(conn.network.networks())


def keypair_options(conn: Connection, _item: Any) -> Options:
    return [(t("(none)"), ""), *((k.name, k.name) for k in conn.compute.keypairs())]


def security_group_options(conn: Connection, _item: Any) -> Options:
    return by_name(conn.network.security_groups())


def available_volume_options(conn: Connection, _item: Any) -> Options:
    vols = (v for v in conn.block_storage.volumes() if v.status == "available")
    return by_name(vols, lambda v: f"{v.name or v.id}  ({v.size} GiB)")


def attached_volume_options(conn: Connection, server: Any) -> Options:
    out = []
    for att in conn.compute.volume_attachments(server):
        out.append((f"{att.volume_id}  {att.device or ''}".strip(), att.volume_id))
    return out


def server_ports(conn: Connection, server: Any) -> list[Any]:
    return list(conn.network.ports(device_id=server.id))


def free_fip_options(conn: Connection, _item: Any) -> Options:
    fips = (f for f in conn.network.ips() if not f.port_id)
    return [(f.floating_ip_address, f.id) for f in fips]


def server_fip_options(conn: Connection, server: Any) -> Options:
    port_ids = {p.id for p in server_ports(conn, server)}
    fips = (f for f in conn.network.ips() if f.port_id in port_ids)
    return [(f.floating_ip_address, f.id) for f in fips]


def server_port_options(conn: Connection, server: Any) -> Options:
    out = []
    for p in server_ports(conn, server):
        ips = ", ".join(ip["ip_address"] for ip in p.fixed_ips or [])
        out.append((f"{ips}  ({p.id[:8]})", p.id))
    return out


def server_sg_options(conn: Connection, server: Any) -> Options:
    groups = conn.compute.fetch_server_security_groups(server).security_groups or []
    names = sorted({g["name"] for g in groups})
    return [(n, n) for n in names]


# --- columns ------------------------------------------------------------------


def server_addresses(server: Any) -> str:
    parts = []
    for net, addrs in (attr(server, "addresses") or {}).items():
        ips = []
        for a in addrs:
            ip = a.get("addr", "")
            if a.get("OS-EXT-IPS:type") == "floating":
                ip += "*"
            ips.append(ip)
        parts.append(f"{net}={','.join(ips)}")
    return "; ".join(parts)


def server_flavor(server: Any) -> str:
    flavor = attr(server, "flavor") or {}
    return attr(flavor, "original_name") or attr(flavor, "name") or attr(flavor, "id") or ""


def server_status(server: Any) -> str:
    status = attr(server, "status", "")
    task = attr(server, "task_state")
    return f"{status} ({task})" if task else status


# --- actions ------------------------------------------------------------------


def _simple(method: str, label: str, *args: Any) -> Any:
    def run(conn: Connection, item: Any, _v: dict[str, Any]) -> str:
        getattr(conn.compute, method)(item, *args)
        return f"{t(label)}: {item.name}"

    return run


def launch_server(conn: Connection, _item: Any, v: dict[str, Any]) -> str:
    attrs: dict[str, Any] = {"name": v["name"], "flavor_id": v["flavor"]}
    attrs["networks"] = [{"uuid": v["network"]}] if v.get("network") else "auto"
    if v.get("key_name"):
        attrs["key_name"] = v["key_name"]
    groups = [g.strip() for g in (v.get("security_groups") or "").split(",") if g.strip()]
    if groups:
        attrs["security_groups"] = [{"name": g} for g in groups]
    size = int(v.get("volume_size") or 0)
    if size > 0:
        attrs["block_device_mapping_v2"] = [
            {
                "boot_index": 0,
                "uuid": v["image"],
                "source_type": "image",
                "destination_type": "volume",
                "volume_size": size,
                "delete_on_termination": bool(v.get("delete_on_termination")),
            }
        ]
    else:
        attrs["image_id"] = v["image"]
    if v.get("user_data"):
        attrs["user_data"] = base64.b64encode(v["user_data"].encode()).decode()
    count = int(v.get("count") or 1)
    if count > 1:
        attrs["min_count"] = attrs["max_count"] = count
    server = conn.compute.create_server(**attrs)
    return t("Server {name} is being created ({id})", name=v["name"], id=server.id)


def console_log(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    length = int(v.get("length") or 0) or None
    return conn.compute.get_server_console_output(item, length=length)["output"]


# Remote consoles tried in order: each cloud enables different ones.
CONSOLE_TYPES = [("vnc", "novnc"), ("spice", "spice-html5"), ("serial", "serial")]


def console_url(conn: Connection, item: Any, _v: dict[str, Any]) -> str:
    # remote-consoles API (microversion 2.6+); os-getVNCConsole was removed from Nova.
    errors = []
    for protocol, kind in CONSOLE_TYPES:
        try:
            console = conn.compute.create_server_remote_console(item, protocol=protocol, type=kind)
        except Exception as exc:  # noqa: BLE001 - type not enabled, try the next one
            errors.append(f"{kind}: {getattr(exc, 'details', None) or exc}")
            continue
        return f"{kind}: {console.url}"
    raise RuntimeError(t("No remote console available") + "\n" + "\n".join(errors))


def snapshot_server(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    image = conn.compute.create_server_image(item, v["name"])
    return t("Snapshot {name} is being created ({id})", name=v["name"], id=image.id)


def resize_server(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    conn.compute.resize_server(item, v["flavor"])
    return t("Resize of {name} started: confirm with Z once VERIFY_RESIZE", name=item.name)


def rebuild_server(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    conn.compute.rebuild_server(item, v["image"])
    return t("Rebuild of {name} started", name=item.name)


def rename_server(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    conn.compute.update_server(item, name=v["name"])
    return t("Renamed to {name}", name=v["name"])


def attach_volume(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    conn.compute.create_volume_attachment(item, volume_id=v["volume"])
    return t("Attaching volume {volume} to {name}", volume=v["volume"], name=item.name)


def detach_volume(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    conn.compute.delete_volume_attachment(item, v["volume"])
    return t("Detaching volume {volume} from {name}", volume=v["volume"], name=item.name)


def associate_fip(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    conn.network.update_ip(v["fip"], port_id=v["port"])
    return t("Floating IP associated to {name}", name=item.name)


def disassociate_fip(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    conn.network.update_ip(v["fip"], port_id=None)
    return t("Floating IP removed from {name}", name=item.name)


def add_sg(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    sg = conn.network.get_security_group(v["group"])
    conn.compute.add_security_group_to_server(item, sg.name)
    return t("Security group {name} added", name=sg.name)


def remove_sg(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    conn.compute.remove_security_group_from_server(item, v["group"])
    return t("Security group {name} removed", name=v["group"])


def attach_interface(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    conn.compute.create_server_interface(item, net_id=v["network"])
    return t("Interface added to {name}", name=item.name)


def detach_interface(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    conn.compute.delete_server_interface(v["port"], server=item)
    return t("Interface removed from {name}", name=item.name)


def delete_server(conn: Connection, item: Any, _v: dict[str, Any]) -> str:
    conn.compute.delete_server(item)
    return t("Server {name} is being deleted", name=item.name)


def create_keypair(conn: Connection, _item: Any, v: dict[str, Any]) -> str:
    attrs: dict[str, Any] = {"name": v["name"]}
    if v.get("public_key"):
        attrs["public_key"] = v["public_key"].strip()
    kp = conn.compute.create_keypair(**attrs)
    if getattr(kp, "private_key", None):
        header = t("Private key generated (it cannot be retrieved later, save it now):")
        return f"{header}\n\n{kp.private_key}"
    return t("Key pair {name} created", name=kp.name)


def delete_keypair(conn: Connection, item: Any, _v: dict[str, Any]) -> str:
    conn.compute.delete_keypair(item)
    return t("Key pair {name} deleted", name=item.name)


def create_server_group(conn: Connection, _item: Any, v: dict[str, Any]) -> str:
    group = conn.compute.create_server_group(name=v["name"], policies=[v["policy"]])
    return t("Server group {name} created", name=group.name)


def delete_server_group(conn: Connection, item: Any, _v: dict[str, Any]) -> str:
    conn.compute.delete_server_group(item)
    return t("Server group {name} deleted", name=item.name)


# --- definitions --------------------------------------------------------------


def _fip_fields() -> list[Field]:
    return [
        Field("fip", "Floating IP", "select", required=True, options=free_fip_options),
        Field("port", "Server port", "select", required=True, options=server_port_options),
    ]


SERVER = ResourceKind(
    key="compute.server",
    title="Servers",
    service="compute",
    aliases=("servers", "instances", "vm"),
    list=lambda conn, q: conn.compute.servers(**q),
    columns=[
        Column("Name", "name"),
        Column("Status", server_status),
        Column("Addresses", server_addresses),
        Column("Flavor", server_flavor),
        Column("GPU", lambda s: gpu_text(server_gpus(s))),
        Column("Key", "key_name"),
        Column("AZ", "availability_zone"),
        Column("Locked", "is_locked"),
        Column("Created", "created_at"),
    ],
    children=[
        Child("e", "Instance actions", "compute.server_action", lambda s: {"server": s.id}),
        Child("w", "Ports", "network.port", lambda s: {"device_id": s.id}),
    ],
    actions=[
        Action(
            "N",
            "Launch instance",
            launch_server,
            needs_item=False,
            fields=[
                Field("name", "Name", required=True),
                Field("flavor", "Flavor", "select", required=True, options=flavor_options),
                Field("image", "Image", "select", required=True, options=image_options),
                Field("network", "Network", "select", options=network_options),
                Field("key_name", "Key pair", "select", options=keypair_options),
                Field("security_groups", "Security groups (comma separated)", default="default"),
                Field(
                    "volume_size",
                    "Boot from volume: size in GiB",
                    "int",
                    default=0,
                    help="0 = ephemeral disk from the flavor",
                ),
                Field("delete_on_termination", "Delete volume with the instance", "bool"),
                Field("count", "Number of instances", "int", default=1),
                Field("user_data", "User data (cloud-init)", "textarea"),
            ],
        ),
        Action("s", "Start", _simple("start_server", "Start")),
        Action("S", "Stop", _simple("stop_server", "Stop"), confirm=True),
        Action("r", "Soft reboot", _simple("reboot_server", "Soft reboot", "SOFT"), confirm=True),
        Action("R", "Hard reboot", _simple("reboot_server", "Hard reboot", "HARD"), confirm=True),
        Action("p", "Pause", _simple("pause_server", "Pause")),
        Action("P", "Unpause", _simple("unpause_server", "Unpause")),
        Action("u", "Suspend", _simple("suspend_server", "Suspend"), confirm=True),
        Action("U", "Resume", _simple("resume_server", "Resume")),
        Action("h", "Shelve", _simple("shelve_server", "Shelve"), confirm=True),
        Action("H", "Unshelve", _simple("unshelve_server", "Unshelve")),
        Action("l", "Lock", _simple("lock_server", "Lock")),
        Action("L", "Unlock", _simple("unlock_server", "Unlock")),
        Action(
            "c",
            "Console log",
            console_log,
            output=True,
            fields=[Field("length", "Lines (0 = all)", "int", default=200)],
        ),
        Action("v", "Console URL", console_url, output=True),
        Action(
            "i",
            "Create snapshot",
            snapshot_server,
            fields=[Field("name", "Snapshot name", required=True)],
        ),
        Action(
            "z",
            "Resize",
            resize_server,
            confirm=True,
            fields=[Field("flavor", "New flavor", "select", True, options=flavor_options)],
        ),
        Action("Z", "Confirm resize", _simple("confirm_server_resize", "Resize confirmed")),
        Action("X", "Revert resize", _simple("revert_server_resize", "Resize reverted")),
        Action(
            "B",
            "Rebuild",
            rebuild_server,
            confirm=True,
            destructive=True,
            fields=[Field("image", "Image", "select", True, options=image_options)],
        ),
        Action(
            "n",
            "Rename",
            rename_server,
            fields=[Field("name", "New name", required=True, default=lambda i: i.name)],
        ),
        Action(
            "o",
            "Attach volume",
            attach_volume,
            fields=[Field("volume", "Volume", "select", True, options=available_volume_options)],
        ),
        Action(
            "O",
            "Detach volume",
            detach_volume,
            confirm=True,
            fields=[Field("volume", "Volume", "select", True, options=attached_volume_options)],
        ),
        Action("f", "Associate floating IP", associate_fip, fields=_fip_fields()),
        Action(
            "F",
            "Disassociate floating IP",
            disassociate_fip,
            confirm=True,
            fields=[Field("fip", "Floating IP", "select", True, options=server_fip_options)],
        ),
        Action(
            "g",
            "Add security group",
            add_sg,
            fields=[
                Field("group", "Security group", "select", True, options=security_group_options)
            ],
        ),
        Action(
            "G",
            "Remove security group",
            remove_sg,
            confirm=True,
            fields=[Field("group", "Security group", "select", True, options=server_sg_options)],
        ),
        Action(
            "t",
            "Attach interface",
            attach_interface,
            fields=[Field("network", "Network", "select", True, options=network_options)],
        ),
        Action(
            "T",
            "Detach interface",
            detach_interface,
            confirm=True,
            fields=[Field("port", "Port", "select", True, options=server_port_options)],
        ),
        Action("ctrl+d", "Delete", delete_server, confirm=True, destructive=True),
    ],
)

SERVER_ACTION = ResourceKind(
    key="compute.server_action",
    title="Instance actions",
    service="compute",
    requires_parent=True,
    status=None,
    id_attr="request_id",
    list=lambda conn, q: conn.compute.server_actions(q["server"]),
    columns=[
        Column("Action", "action"),
        Column("Request", "request_id"),
        Column("Start", "start_time"),
        Column("User", "user_id"),
        Column("Message", "message"),
    ],
)

FLAVOR = ResourceKind(
    key="compute.flavor",
    title="Flavors",
    service="compute",
    aliases=("flavors",),
    status=None,
    list=lambda conn, q: conn.compute.flavors(**q),
    columns=[
        Column("Name", "name"),
        Column("vCPU", "vcpus"),
        Column("RAM GiB", lambda f: round(f.ram / 1024, 1)),
        Column("Disk GiB", "disk"),
        Column("Ephemeral GiB", "ephemeral"),
        Column("Swap MiB", "swap"),
        Column("GPU", lambda f: gpu_text(flavor_gpus(f))),
        Column("Public", "is_public"),
    ],
)

KEYPAIR = ResourceKind(
    key="compute.keypair",
    title="Key pairs",
    service="compute",
    aliases=("keypairs", "keys", "ssh"),
    status=None,
    id_attr="name",
    list=lambda conn, q: conn.compute.keypairs(**q),
    columns=[
        Column("Name", "name"),
        Column("Type", "type"),
        Column("Fingerprint", "fingerprint"),
    ],
    actions=[
        Action(
            "N",
            "Create / import key pair",
            create_keypair,
            needs_item=False,
            output=True,
            fields=[
                Field("name", "Name", required=True),
                Field(
                    "public_key",
                    "Public key",
                    "textarea",
                    help="Empty = generate a new key pair (if the cloud allows it)",
                ),
            ],
        ),
        Action("ctrl+d", "Delete", delete_keypair, confirm=True, destructive=True),
    ],
)

SERVER_GROUP_POLICIES = ("anti-affinity", "affinity", "soft-anti-affinity", "soft-affinity")

SERVER_GROUP = ResourceKind(
    key="compute.server_group",
    title="Server groups",
    service="compute",
    aliases=("server-groups",),
    status=None,
    list=lambda conn, q: conn.compute.server_groups(**q),
    columns=[
        Column("Name", "name"),
        Column("Policy", lambda g: attr(g, "policy") or attr(g, "policies")),
        Column("Members", lambda g: len(attr(g, "member_ids") or [])),
    ],
    actions=[
        Action(
            "N",
            "Create server group",
            create_server_group,
            needs_item=False,
            fields=[
                Field("name", "Name", required=True),
                Field(
                    "policy",
                    "Policy",
                    "select",
                    True,
                    options=lambda c, i: [(p, p) for p in SERVER_GROUP_POLICIES],
                ),
            ],
        ),
        Action("ctrl+d", "Delete", delete_server_group, confirm=True, destructive=True),
    ],
)

KINDS = [SERVER, SERVER_ACTION, FLAVOR, KEYPAIR, SERVER_GROUP]
