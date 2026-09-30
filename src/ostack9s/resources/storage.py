"""Cinder resources (volumes, snapshots, backups) and Glance images."""

from __future__ import annotations

from typing import Any

from openstack.connection import Connection

from ..i18n import t
from .base import Action, Column, Field, Options, ResourceKind, attr, by_name, none_if_empty


def volume_type_options(conn: Connection, _item: Any) -> Options:
    return [(t("(default)"), ""), *by_name(conn.block_storage.types())]


def image_options(conn: Connection, _item: Any) -> Options:
    active = (i for i in conn.image.images() if i.status == "active")
    return [(t("(empty volume)"), ""), *by_name(active)]


def server_options(conn: Connection, _item: Any) -> Options:
    return by_name(conn.compute.servers())


def volume_attachments(volume: Any) -> str:
    return ", ".join(
        a.get("server_id", "")[:8] + ":" + a.get("device", "") for a in volume.attachments or []
    )


# --- volume actions -------------------------------------------------------------


def create_volume(conn: Connection, _item: Any, v: dict[str, Any]) -> str:
    vol = conn.block_storage.create_volume(
        name=v["name"],
        size=int(v["size"]),
        description=none_if_empty(v.get("description")),
        volume_type=none_if_empty(v.get("volume_type")),
        image_id=none_if_empty(v.get("image")),
    )
    return t("Volume {name} is being created", name=vol.name)


def extend_volume(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    conn.block_storage.extend_volume(item, int(v["size"]))
    return t("Extending {name} to {size} GiB", name=item.name, size=v["size"])


def update_volume(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    conn.block_storage.update_volume(item, name=v["name"], description=v.get("description") or "")
    return t("Volume updated")


def snapshot_volume(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    snap = conn.block_storage.create_snapshot(
        volume_id=item.id, name=v["name"], force=bool(v.get("force"))
    )
    return t("Snapshot {name} is being created", name=snap.name)


def backup_volume(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    backup = conn.block_storage.create_backup(
        volume_id=item.id,
        name=v["name"],
        incremental=bool(v.get("incremental")),
        force=bool(v.get("force")),
    )
    return t("Backup {name} is being created", name=backup.name)


def attach_to_server(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    conn.compute.create_volume_attachment(v["server"], volume_id=item.id)
    return t("Attaching volume {name}", name=item.name)


def detach_from_server(conn: Connection, item: Any, _v: dict[str, Any]) -> str:
    for att in item.attachments or []:
        conn.compute.delete_volume_attachment(att["server_id"], item.id)
    return t("Detaching volume {name}", name=item.name)


def set_bootable(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    conn.block_storage.set_volume_bootable_status(item, bool(v.get("bootable")))
    return t("Bootable flag updated")


def upload_to_image(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    conn.block_storage.upload_volume_to_image(
        item, v["image_name"], force=bool(v.get("force")), disk_format=v.get("disk_format")
    )
    return t("Uploading {name} as image {image}", name=item.name, image=v["image_name"])


def retype_volume(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    conn.block_storage.retype_volume(item, v["volume_type"], migration_policy="on-demand")
    return t("Retype started")


def delete_volume(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    conn.block_storage.delete_volume(item, cascade=bool(v.get("cascade")))
    return t("Volume {name} is being deleted", name=item.name)


# --- snapshot and backup actions -------------------------------------------


def volume_from_snapshot(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    vol = conn.block_storage.create_volume(
        name=v["name"], size=int(v.get("size") or item.size), snapshot_id=item.id
    )
    return t("Volume {name} is being created from the snapshot", name=vol.name)


def update_snapshot(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    conn.block_storage.update_snapshot(item, name=v["name"], description=v.get("description") or "")
    return t("Snapshot updated")


def delete_snapshot(conn: Connection, item: Any, _v: dict[str, Any]) -> str:
    conn.block_storage.delete_snapshot(item)
    return t("Snapshot {name} is being deleted", name=item.name)


def restore_backup(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    conn.block_storage.restore_backup(item, name=none_if_empty(v.get("name")))
    return t("Restoring {name} to a new volume", name=item.name)


def delete_backup(conn: Connection, item: Any, _v: dict[str, Any]) -> str:
    conn.block_storage.delete_backup(item)
    return t("Backup {name} is being deleted", name=item.name)


# --- image actions --------------------------------------------------------------


def update_image(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    conn.image.update_image(item, name=v["name"], visibility=v["visibility"])
    return t("Image updated")


def delete_image(conn: Connection, item: Any, _v: dict[str, Any]) -> str:
    conn.image.delete_image(item)
    return t("Image {name} deleted", name=item.name)


def visibility_options(_c: Connection, _i: Any) -> Options:
    return [(x, x) for x in ("private", "shared", "community", "public")]


# --- definitions --------------------------------------------------------------

VOLUME = ResourceKind(
    key="block_storage.volume",
    title="Volumes",
    service="block-storage",
    aliases=("volumes", "vol", "disks"),
    list=lambda conn, q: conn.block_storage.volumes(**q),
    columns=[
        Column("Name", lambda i: attr(i, "name") or attr(i, "id")),
        Column("Status", "status"),
        Column("Size GiB", "size"),
        Column("Type", "volume_type"),
        Column("Bootable", "is_bootable"),
        Column("Attached", volume_attachments),
        Column("AZ", "availability_zone"),
        Column("Created", "created_at"),
    ],
    actions=[
        Action(
            "N",
            "Create volume",
            create_volume,
            needs_item=False,
            fields=[
                Field("name", "Name", required=True),
                Field("size", "Size (GiB)", "int", required=True, default=10),
                Field("volume_type", "Type", "select", options=volume_type_options),
                Field("image", "Source image", "select", options=image_options),
                Field("description", "Description"),
            ],
        ),
        Action(
            "x",
            "Extend",
            extend_volume,
            confirm=True,
            fields=[Field("size", "New size (GiB)", "int", True, default=lambda i: i.size + 10)],
        ),
        Action(
            "n",
            "Edit name/description",
            update_volume,
            fields=[
                Field("name", "Name", required=True, default=lambda i: i.name),
                Field("description", "Description", default=lambda i: i.description),
            ],
        ),
        Action(
            "i",
            "Create snapshot",
            snapshot_volume,
            fields=[
                Field("name", "Name", required=True),
                Field("force", "Force (volume in use)", "bool"),
            ],
        ),
        Action(
            "b",
            "Create backup",
            backup_volume,
            fields=[
                Field("name", "Name", required=True),
                Field("incremental", "Incremental", "bool"),
                Field("force", "Force (volume in use)", "bool"),
            ],
        ),
        Action(
            "o",
            "Attach to server",
            attach_to_server,
            fields=[Field("server", "Server", "select", True, options=server_options)],
        ),
        Action("O", "Detach", detach_from_server, confirm=True),
        Action(
            "k",
            "Set bootable",
            set_bootable,
            fields=[Field("bootable", "Bootable", "bool", default=True)],
        ),
        Action(
            "U",
            "Upload to image",
            upload_to_image,
            fields=[
                Field("image_name", "Image name", required=True),
                Field(
                    "disk_format",
                    "Format",
                    "select",
                    options=lambda c, i: [(f, f) for f in ("raw", "qcow2", "vmdk", "vdi")],
                ),
                Field("force", "Force (volume in use)", "bool"),
            ],
        ),
        Action(
            "t",
            "Change type",
            retype_volume,
            confirm=True,
            fields=[
                Field(
                    "volume_type",
                    "New type",
                    "select",
                    True,
                    options=lambda c, i: by_name(c.block_storage.types()),
                )
            ],
        ),
        Action(
            "ctrl+d",
            "Delete",
            delete_volume,
            confirm=True,
            destructive=True,
            fields=[Field("cascade", "Also delete snapshots", "bool")],
        ),
    ],
)

SNAPSHOT = ResourceKind(
    key="block_storage.snapshot",
    title="Volume snapshots",
    service="block-storage",
    aliases=("snapshots", "volume-snapshots"),
    list=lambda conn, q: conn.block_storage.snapshots(**q),
    columns=[
        Column("Name", "name"),
        Column("Status", "status"),
        Column("Size GiB", "size"),
        Column("Volume", "volume_id"),
        Column("Created", "created_at"),
    ],
    actions=[
        Action(
            "N",
            "Create volume from snapshot",
            volume_from_snapshot,
            fields=[
                Field("name", "Volume name", required=True),
                Field("size", "Size in GiB (empty = same as the snapshot)", "int"),
            ],
        ),
        Action(
            "n",
            "Edit name/description",
            update_snapshot,
            fields=[
                Field("name", "Name", required=True, default=lambda i: i.name),
                Field("description", "Description", default=lambda i: i.description),
            ],
        ),
        Action("ctrl+d", "Delete", delete_snapshot, confirm=True, destructive=True),
    ],
)

BACKUP = ResourceKind(
    key="block_storage.backup",
    title="Volume backups",
    service="block-storage",
    aliases=("backups",),
    list=lambda conn, q: conn.block_storage.backups(**q),
    columns=[
        Column("Name", "name"),
        Column("Status", "status"),
        Column("Size GiB", "size"),
        Column("Volume", "volume_id"),
        Column("Incremental", "is_incremental"),
        Column("Created", "created_at"),
    ],
    actions=[
        Action(
            "N",
            "Restore to new volume",
            restore_backup,
            confirm=True,
            fields=[Field("name", "New volume name")],
        ),
        Action("ctrl+d", "Delete", delete_backup, confirm=True, destructive=True),
    ],
)

IMAGE = ResourceKind(
    key="image.image",
    title="Images",
    service="image",
    aliases=("images", "img"),
    list=lambda conn, q: conn.image.images(**q),
    columns=[
        Column("Name", "name"),
        Column("Status", "status"),
        Column("Visibility", "visibility"),
        Column("Format", "disk_format"),
        Column("Size MiB", lambda i: round((attr(i, "size") or 0) / 2**20)),
        Column("Min disk GiB", "min_disk"),
        Column("Protected", "is_protected"),
        Column("Created", "created_at"),
    ],
    actions=[
        Action(
            "n",
            "Edit name/visibility",
            update_image,
            fields=[
                Field("name", "Name", required=True, default=lambda i: i.name),
                Field(
                    "visibility",
                    "Visibility",
                    "select",
                    True,
                    default=lambda i: i.visibility,
                    options=visibility_options,
                ),
            ],
        ),
        Action("ctrl+d", "Delete", delete_image, confirm=True, destructive=True),
    ],
)

KINDS = [VOLUME, SNAPSHOT, BACKUP, IMAGE]
