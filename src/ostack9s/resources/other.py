"""Load balancers (Octavia), object storage (Swift), secrets (Barbican), identity."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from openstack.connection import Connection

from ..cloud import parse_time, time_left
from ..i18n import t
from .base import Action, Child, Column, Field, ResourceKind, attr


def _lb_delete(method: str, what: str, **kwargs: Any) -> Any:
    def run(conn: Connection, item: Any, _v: dict[str, Any]) -> str:
        getattr(conn.load_balancer, method)(item, **kwargs)
        return t("{what} {name} is being deleted", what=t(what), name=item.name or item.id)

    return run


def delete_member(conn: Connection, item: Any, _v: dict[str, Any]) -> str:
    conn.load_balancer.delete_member(item, item.pool_id)
    return t("Member {name} is being deleted", name=item.name or item.address)


def create_container(conn: Connection, _item: Any, v: dict[str, Any]) -> str:
    conn.object_store.create_container(name=v["name"])
    return t("Container {name} created", name=v["name"])


def delete_container(conn: Connection, item: Any, _v: dict[str, Any]) -> str:
    conn.object_store.delete_container(item)
    return t("Container {name} deleted", name=item.name)


def download_object(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    target = Path(v["path"]).expanduser()
    if target.is_dir():
        target = target / Path(item.name).name
    if target.exists():
        raise FileExistsError(t("{path} already exists", path=target))
    data = conn.object_store.download_object(item, container=item.container)
    target.write_bytes(data)
    return t("Saved to {path} ({size} bytes)", path=target, size=len(data))


def delete_object(conn: Connection, item: Any, _v: dict[str, Any]) -> str:
    conn.object_store.delete_object(item, container=item.container)
    return t("Object {name} deleted", name=item.name)


def delete_secret(conn: Connection, item: Any, _v: dict[str, Any]) -> str:
    conn.key_manager.delete_secret(item)
    return t("Secret {name} deleted", name=item.name or item.id)


def list_objects(conn: Connection, q: dict[str, Any]) -> Any:
    for obj in conn.object_store.objects(q["container"]):
        # Object resources do not always carry their container: actions need it.
        obj.container = q["container"]
        yield obj


def _identity(conn: Connection) -> Any:
    """Keystone v3 proxy (openstacksdk types it as a v2/v3 union)."""
    return conn.identity


def list_app_credentials(conn: Connection, q: dict[str, Any]) -> Any:
    return _identity(conn).application_credentials(conn.current_user_id, **q)


def delete_app_credential(conn: Connection, item: Any, _v: dict[str, Any]) -> str:
    _identity(conn).delete_application_credential(conn.current_user_id, item)
    return t("Application credential {name} deleted", name=item.name)


def expires_in(cred: Any) -> str:
    expires = parse_time(attr(cred, "expires_at"))
    if expires is None:
        return t("never")
    return time_left(expires) if time_left(expires) != "0m" else t("expired")


def create_app_credential(conn: Connection, _item: Any, v: dict[str, Any]) -> str:
    cred = _identity(conn).create_application_credential(
        conn.current_user_id,
        name=v["name"],
        description=v.get("description") or None,
        expires_at=v.get("expires_at") or None,
        unrestricted=bool(v.get("unrestricted")),
    )
    return (
        t("Application credential created (the secret cannot be retrieved later):") + "\n\n"
        f"application_credential_id: {cred.id}\n"
        f"application_credential_secret: {cred.secret}\n"
    )


LOADBALANCER = ResourceKind(
    key="load_balancer.loadbalancer",
    title="Load balancers",
    service="load-balancer",
    aliases=("loadbalancers", "lb"),
    status="provisioning_status",
    enter="l",
    list=lambda conn, q: conn.load_balancer.load_balancers(**q),
    columns=[
        Column("Name", "name"),
        Column("Provisioning", "provisioning_status"),
        Column("Operating", "operating_status"),
        Column("VIP", "vip_address"),
        Column("Provider", "provider"),
    ],
    children=[
        Child("l", "Listeners", "load_balancer.listener", lambda lb: {"load_balancer_id": lb.id}),
        Child("p", "Pools", "load_balancer.pool", lambda lb: {"loadbalancer_id": lb.id}),
    ],
    actions=[
        Action(
            "ctrl+d",
            "Delete (cascade)",
            _lb_delete("delete_load_balancer", "Load balancer", cascade=True),
            confirm=True,
            destructive=True,
        ),
    ],
)

LISTENER = ResourceKind(
    key="load_balancer.listener",
    title="LB listeners",
    service="load-balancer",
    aliases=("listeners",),
    status="operating_status",
    list=lambda conn, q: conn.load_balancer.listeners(**q),
    columns=[
        Column("Name", "name"),
        Column("Protocol", "protocol"),
        Column("Port", "protocol_port"),
        Column("Operating", "operating_status"),
        Column("Default pool", "default_pool_id"),
    ],
    actions=[
        Action(
            "ctrl+d",
            "Delete",
            _lb_delete("delete_listener", "Listener"),
            confirm=True,
            destructive=True,
        )
    ],
)

POOL = ResourceKind(
    key="load_balancer.pool",
    title="LB pools",
    service="load-balancer",
    aliases=("pools",),
    status="operating_status",
    enter="M",
    list=lambda conn, q: conn.load_balancer.pools(**q),
    columns=[
        Column("Name", "name"),
        Column("Protocol", "protocol"),
        Column("Algorithm", "lb_algorithm"),
        Column("Operating", "operating_status"),
        Column("Members", lambda p: len(attr(p, "members") or [])),
        Column("Health monitor", "health_monitor_id"),
    ],
    children=[Child("M", "Members", "load_balancer.member", lambda p: {"pool": p.id})],
    actions=[
        Action(
            "ctrl+d", "Delete", _lb_delete("delete_pool", "Pool"), confirm=True, destructive=True
        )
    ],
)


def list_members(conn: Connection, q: dict[str, Any]) -> Any:
    for member in conn.load_balancer.members(q["pool"]):
        member.pool_id = q["pool"]
        yield member


MEMBER = ResourceKind(
    key="load_balancer.member",
    title="LB pool members",
    service="load-balancer",
    requires_parent=True,
    status="operating_status",
    list=list_members,
    columns=[
        Column("Name", "name"),
        Column("Address", "address"),
        Column("Port", "protocol_port"),
        Column("Weight", "weight"),
        Column("Operating", "operating_status"),
    ],
    actions=[Action("ctrl+d", "Delete", delete_member, confirm=True, destructive=True)],
)

CONTAINER = ResourceKind(
    key="object_store.container",
    title="Containers",
    service="object-store",
    aliases=("containers", "swift", "buckets"),
    status=None,
    id_attr="name",
    enter="o",
    list=lambda conn, q: conn.object_store.containers(**q),
    columns=[
        Column("Name", "name"),
        Column("Objects", "count"),
        Column("Size MiB", lambda c: round((attr(c, "bytes") or 0) / 2**20, 1)),
    ],
    children=[Child("o", "Objects", "object_store.object", lambda c: {"container": c.name})],
    actions=[
        Action(
            "N",
            "Create container",
            create_container,
            needs_item=False,
            fields=[Field("name", "Name", required=True)],
        ),
        Action(
            "ctrl+d",
            "Delete (must be empty)",
            delete_container,
            confirm=True,
            destructive=True,
        ),
    ],
)

OBJECT = ResourceKind(
    key="object_store.object",
    title="Objects",
    service="object-store",
    requires_parent=True,
    status=None,
    id_attr="name",
    list=list_objects,
    columns=[
        Column("Name", "name"),
        Column("Size KiB", lambda o: round((attr(o, "content_length") or 0) / 1024, 1)),
        Column("Type", "content_type"),
        Column("Modified", "last_modified_at"),
    ],
    actions=[
        Action(
            "D",
            "Download",
            download_object,
            fields=[Field("path", "Destination (file or directory)", required=True, default=".")],
        ),
        Action("ctrl+d", "Delete", delete_object, confirm=True, destructive=True),
    ],
)

SECRET = ResourceKind(
    key="key_manager.secret",
    title="Secrets",
    service="key-manager",
    aliases=("secrets", "barbican"),
    id_attr="secret_ref",
    list=lambda conn, q: conn.key_manager.secrets(**q),
    columns=[
        Column("Name", "name"),
        Column("Type", "secret_type"),
        Column("Status", "status"),
        Column("Algorithm", "algorithm"),
        Column("Expiration", "expires_at"),
        Column("Created", "created_at"),
    ],
    actions=[Action("ctrl+d", "Delete", delete_secret, confirm=True, destructive=True)],
)

APP_CREDENTIAL = ResourceKind(
    key="identity.application_credential",
    title="Application credentials",
    service="identity",
    aliases=("app-creds", "application-credentials"),
    status=None,
    list=list_app_credentials,
    columns=[
        Column("Name", "name"),
        Column("Project", "project_id"),
        Column("Unrestricted", "unrestricted"),
        Column("Expires", "expires_at"),
        Column("Expires in", expires_in),
        Column("Description", "description"),
    ],
    actions=[
        Action(
            "N",
            "Create application credential",
            create_app_credential,
            needs_item=False,
            output=True,
            fields=[
                Field("name", "Name", required=True),
                Field("description", "Description"),
                Field("expires_at", "Expiration (e.g. 2027-01-01T00:00:00)"),
                Field("unrestricted", "Unrestricted", "bool"),
            ],
        ),
        Action("ctrl+d", "Delete", delete_app_credential, confirm=True, destructive=True),
    ],
)

KINDS = [LOADBALANCER, LISTENER, POOL, MEMBER, CONTAINER, OBJECT, SECRET, APP_CREDENTIAL]
