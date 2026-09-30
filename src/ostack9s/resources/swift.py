"""Object storage (Swift): containers and objects, browsed by pseudo folder.

openstacksdk lists objects without a delimiter and has no copy call, so the
listing, copy, HEAD and POST requests are made directly on the Swift endpoint.
Horizon does not show container policies, quotas, versioning, large objects or
expiry dates: they are columns or details here.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from openstack import exceptions
from openstack.connection import Connection

from ..i18n import t
from .base import Action, Child, Column, Field, Options, ResourceKind, attr

PUBLIC_ACL = ".r:*,.rlistings"
# HEAD requests made to fill the container columns: beyond this the list only
# shows names, counts and sizes.
MAX_CONTAINER_DETAILS = 100
PARALLEL = 8
META_PREFIX = "x-object-meta-"


def _path(container: str, name: str = "") -> str:
    return f"/{quote(container)}" + (f"/{quote(name)}" if name else "")


def _request(conn: Connection, method: str, path: str, **kwargs: Any) -> Any:
    """Raw request on the Swift endpoint; errors become SDK exceptions (403 included)."""
    resp = conn.object_store.request(path, method, raise_exc=False, **kwargs)
    exceptions.raise_from_response(resp)
    return resp


def _endpoint(conn: Connection) -> str:
    return str(conn.object_store.get_endpoint() or "").rstrip("/")


def _headers(resp: Any) -> dict[str, str]:
    return {k.lower(): v for k, v in resp.headers.items()}


# --- account ------------------------------------------------------------------


def account_usage(conn: Connection) -> tuple[float, float]:
    """(GiB used, GiB quota or -1) of the Swift account (Horizon shows neither)."""
    account = conn.object_store.get_account_metadata()
    used = (attr(account, "account_bytes_used") or 0) / 2**30
    quota = (attr(account, "metadata") or {}).get("quota-bytes")
    try:
        limit = int(quota) / 2**30 if quota not in (None, "") else -1
    except (TypeError, ValueError):
        limit = -1
    return used, limit


# --- containers -----------------------------------------------------------------


@dataclass
class ContainerInfo:
    policy: str = ""
    public: bool | None = None
    quota_bytes: int | None = None
    quota_count: int | None = None
    versioning: str = ""


def container_info(headers: dict[str, str]) -> ContainerInfo:
    read = headers.get("x-container-read", "")
    versioning = ""
    if headers.get("x-versions-enabled", "").lower() == "true":
        versioning = "enabled"
    elif headers.get("x-history-location"):
        versioning = "history → " + headers["x-history-location"]
    elif headers.get("x-versions-location"):
        versioning = "stack → " + headers["x-versions-location"]

    def number(key: str) -> int | None:
        try:
            return int(headers[key])
        except (KeyError, ValueError):
            return None

    return ContainerInfo(
        policy=headers.get("x-storage-policy", ""),
        public=".r:" in read,
        quota_bytes=number("x-container-meta-quota-bytes"),
        quota_count=number("x-container-meta-quota-count"),
        versioning=versioning,
    )


def list_containers(conn: Connection, q: dict[str, Any]) -> Any:
    containers: list[Any] = list(conn.object_store.containers(**q))
    if len(containers) > MAX_CONTAINER_DETAILS:
        return containers

    def fill(c: Any) -> None:
        try:
            c.info = container_info(_headers(_request(conn, "HEAD", _path(c.name))))
        except Exception:  # noqa: BLE001 - the name, count and size are still shown
            c.info = None

    if containers:
        with ThreadPoolExecutor(max_workers=min(PARALLEL, len(containers))) as pool:
            list(pool.map(fill, containers))
    return containers


def _info(c: Any, name: str) -> Any:
    info = getattr(c, "info", None)
    return getattr(info, name) if info is not None else None


def container_access(c: Any) -> str:
    public = _info(c, "public")
    return "" if public is None else ("public" if public else "private")


def container_quota(c: Any) -> str:
    parts = []
    if (size := _info(c, "quota_bytes")) is not None:
        parts.append(f"{round(size / 2**30, 1)} GiB")
    if (count := _info(c, "quota_count")) is not None:
        parts.append(t("{count} objects", count=count))
    return ", ".join(parts)


# --- objects ----------------------------------------------------------------------


@dataclass
class Entry:
    """A row of the object view: an object or a pseudo folder (``is_dir``)."""

    container: str
    name: str
    is_dir: bool = False
    bytes: int = 0
    content_type: str = ""
    last_modified: str = ""
    hash: str = ""
    large: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.name

    def to_dict(self) -> dict[str, Any]:
        out = {k: v for k, v in vars(self).items() if k != "extra"}
        out.update(self.extra)
        return out


def entry_from_listing(container: str, row: dict[str, Any]) -> Entry:
    if "subdir" in row:
        return Entry(container, row["subdir"], is_dir=True)
    large = "SLO" if row.get("slo_etag") else ""
    content_type = row.get("content_type", "")
    if content_type == "application/directory":
        # Folder marker created by Horizon or by "Create folder".
        return Entry(container, row["name"].rstrip("/") + "/", is_dir=True)
    known = {"name", "bytes", "content_type", "last_modified", "hash"}
    return Entry(
        container,
        row["name"],
        bytes=int(row.get("bytes") or 0),
        content_type=content_type,
        last_modified=row.get("last_modified", ""),
        hash=row.get("hash", ""),
        large=large,
        extra={k: v for k, v in row.items() if k not in known},
    )


def list_entries(conn: Connection, q: dict[str, Any]) -> list[Entry]:
    """One folder level: objects and sub folders under ``prefix``."""
    container, prefix = q["container"], q.get("prefix", "")
    out: dict[str, Entry] = {}
    marker = ""
    while True:
        params = {"format": "json", "delimiter": "/", "prefix": prefix, "marker": marker}
        rows = _request(conn, "GET", _path(container), params=params).json()
        if not rows:
            break
        for row in rows:
            entry = entry_from_listing(container, row)
            if entry.name != prefix:  # the marker of the folder being browsed
                out.setdefault(entry.name, entry)
        marker = rows[-1].get("name") or rows[-1].get("subdir")
    # Folders first, like a file manager.
    return sorted(out.values(), key=lambda e: (not e.is_dir, e.name))


def all_names(conn: Connection, container: str, prefix: str = "") -> list[str]:
    """Every object under a prefix (no delimiter)."""
    names: list[str] = []
    marker = ""
    while True:
        params = {"format": "json", "prefix": prefix, "marker": marker}
        rows = _request(conn, "GET", _path(container), params=params).json()
        if not rows:
            return names
        names += [r["name"] for r in rows]
        marker = rows[-1]["name"]


def delete_names(conn: Connection, container: str, names: list[str]) -> None:
    def delete(name: str) -> None:
        conn.object_store.delete_object(name, container=container)

    if names:
        with ThreadPoolExecutor(max_workers=min(PARALLEL, len(names))) as pool:
            list(pool.map(delete, names))


def entry_label(entry: Any) -> str:
    """Name relative to the folder being browsed."""
    name = attr(entry, "name") or ""
    base = name.rstrip("/").rsplit("/", 1)[-1]
    return base + "/" if attr(entry, "is_dir") else base


def entry_type(entry: Any) -> str:
    if attr(entry, "is_dir"):
        return t("folder")
    return " ".join(filter(None, (attr(entry, "large"), attr(entry, "content_type"))))


def target(item: Any) -> tuple[str, str]:
    """(container, folder prefix) for actions run from a container or an object view."""
    if isinstance(item, Entry):
        if item.is_dir:
            return item.container, item.name
        return item.container, item.name.rsplit("/", 1)[0] + "/" if "/" in item.name else ""
    return str(attr(item, "name")), ""


def _require_object(item: Any) -> Entry:
    if not isinstance(item, Entry) or item.is_dir:
        raise ValueError(t("Select an object, not a folder"))
    return item


# --- actions: containers ------------------------------------------------------------


def create_container(conn: Connection, _item: Any, v: dict[str, Any]) -> str:
    conn.object_store.create_container(name=v["name"])
    return t("Container {name} created", name=v["name"])


def delete_container(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    if v.get("objects"):
        delete_names(conn, item.name, all_names(conn, item.name))
    conn.object_store.delete_container(item)
    return t("Container {name} deleted", name=item.name)


def set_access(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    if v["access"] == "public":
        _request(conn, "POST", _path(item.name), headers={"X-Container-Read": PUBLIC_ACL})
        url = _endpoint(conn) + _path(item.name)
        return t("Container {name} is public: {url}", name=item.name, url=url)
    _request(conn, "POST", _path(item.name), headers={"X-Remove-Container-Read": "1"})
    return t("Container {name} is private", name=item.name)


def container_details(conn: Connection, item: Any, _v: dict[str, Any]) -> str:
    headers = _headers(_request(conn, "HEAD", _path(item.name)))
    lines = [f"{k}: {v}" for k, v in sorted(headers.items()) if k.startswith("x-")]
    lines.append(f"url: {_endpoint(conn)}{_path(item.name)}")
    return "\n".join(lines)


# --- actions: objects -------------------------------------------------------------


def upload(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    """Upload a file or a directory tree; large files become SLO segments."""
    container, prefix = target(item)
    source = Path(v["path"]).expanduser()
    dest = (v.get("name") or "").strip()
    if source.is_dir():
        base = prefix + (dest.rstrip("/") + "/" if dest else source.name + "/")
        files = [p for p in sorted(source.rglob("*")) if p.is_file()]
        pairs = [(p, base + p.relative_to(source).as_posix()) for p in files]
    elif source.is_file():
        pairs = [(source, prefix + (dest or source.name))]
    else:
        raise FileNotFoundError(t("{path} does not exist", path=source))

    def send(pair: tuple[Path, str]) -> None:
        conn.object_store.create_object(container, pair[1], filename=os.fspath(pair[0]))

    with ThreadPoolExecutor(max_workers=min(PARALLEL, max(1, len(pairs)))) as pool:
        list(pool.map(send, pairs))
    size = sum(p.stat().st_size for p, _ in pairs)
    return t(
        "Uploaded {count} files ({size} MiB) to {container}",
        count=len(pairs),
        size=round(size / 2**20, 1),
        container=container,
    )


def create_folder(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    container, prefix = target(item)
    name = prefix + v["name"].strip("/") + "/"
    _request(
        conn,
        "PUT",
        _path(container, name),
        data=b"",
        headers={"Content-Type": "application/directory", "Content-Length": "0"},
    )
    return t("Folder {name} created", name=name)


def download_object(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    entry = _require_object(item)
    target_path = Path(v["path"]).expanduser()
    if target_path.is_dir():
        target_path = target_path / Path(entry.name).name
    if target_path.exists():
        raise FileExistsError(t("{path} already exists", path=target_path))
    size = 0
    with open(target_path, "wb") as fh:
        for chunk in conn.object_store.stream_object(entry.name, container=entry.container):
            fh.write(chunk)
            size += len(chunk)
    return t("Saved to {path} ({size} bytes)", path=target_path, size=size)


def copy_object(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    entry = _require_object(item)
    dest_container = v.get("container") or entry.container
    dest = v["name"].strip()
    _request(
        conn,
        "PUT",
        _path(dest_container, dest),
        headers={"X-Copy-From": _path(entry.container, entry.name), "Content-Length": "0"},
    )
    return t("Copied to {container}/{name}", container=dest_container, name=dest)


def object_headers(conn: Connection, entry: Entry) -> dict[str, str]:
    return _headers(_request(conn, "HEAD", _path(entry.container, entry.name)))


def _post_object(
    conn: Connection, entry: Entry, meta: dict[str, str], extra: dict[str, str]
) -> None:
    """POST replaces every user metadata of an object: always send the full set."""
    headers = {META_PREFIX + k: val for k, val in meta.items()}
    _request(conn, "POST", _path(entry.container, entry.name), headers={**headers, **extra})


def _current_meta(headers: dict[str, str]) -> dict[str, str]:
    return {k[len(META_PREFIX) :]: v for k, v in headers.items() if k.startswith(META_PREFIX)}


def parse_meta(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip():
            out[key.strip().lower()] = value.strip()
    return out


def metadata_text(conn: Connection, item: Any) -> str:
    entry = _require_object(item)
    meta = _current_meta(object_headers(conn, entry))
    return "\n".join(f"{k}: {v}" for k, v in sorted(meta.items()))


def edit_metadata(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    entry = _require_object(item)
    headers = object_headers(conn, entry)
    extra = {}
    # The expiry date survives the POST only if sent again.
    if headers.get("x-delete-at"):
        extra["X-Delete-At"] = headers["x-delete-at"]
    meta = parse_meta(v.get("metadata") or "")
    _post_object(conn, entry, meta, extra)
    return t("Metadata updated ({count} keys)", count=len(meta))


def set_expiry(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    entry = _require_object(item)
    meta = _current_meta(object_headers(conn, entry))
    days = int(v.get("days") or 0)
    if days <= 0:
        _post_object(conn, entry, meta, {"X-Remove-Delete-At": "1"})
        return t("Expiry removed from {name}", name=entry.name)
    _post_object(conn, entry, meta, {"X-Delete-After": str(days * 86400)})
    return t("{name} will be deleted in {days} days", name=entry.name, days=days)


def temp_url(conn: Connection, item: Any, v: dict[str, Any]) -> str:
    entry = _require_object(item)
    key = conn.object_store.get_temp_url_key(entry.container)
    if not key:
        if not v.get("create_key"):
            raise ValueError(t("No temp URL key on the account or the container"))
        key = os.urandom(24).hex()
        conn.object_store.set_account_temp_url_key(key)
    endpoint = urlsplit(_endpoint(conn))
    path = endpoint.path.rstrip("/") + "/" + entry.container + "/" + entry.name
    minutes = int(v.get("minutes") or 60)
    signed = conn.object_store.generate_temp_url(path, minutes * 60, "GET", temp_url_key=key)
    signed = signed.decode() if isinstance(signed, bytes) else signed
    return f"{endpoint.scheme}://{endpoint.netloc}{quote(signed, safe='/?=&')}"


def object_details(conn: Connection, item: Any, _v: dict[str, Any]) -> str:
    entry = _require_object(item)
    headers = object_headers(conn, entry)
    lines = [f"{k}: {v}" for k, v in sorted(headers.items())]
    if headers.get("x-delete-at", "").isdigit():
        when = datetime.fromtimestamp(int(headers["x-delete-at"]), UTC)
        lines.append(t("deleted at: {date}", date=when.isoformat()))
    if headers.get("x-static-large-object", "").lower() == "true":
        lines.append(t("static large object (segments deleted with it)"))
    if headers.get("x-object-manifest"):
        lines.append(
            t("dynamic large object: segments in {path}", path=headers["x-object-manifest"])
        )
    return "\n".join(lines)


def delete_entry(conn: Connection, item: Any, _v: dict[str, Any]) -> str:
    if isinstance(item, Entry) and item.is_dir:
        names = all_names(conn, item.container, item.name)
        delete_names(conn, item.container, names)
        return t("Folder {name} deleted ({count} objects)", name=item.name, count=len(names))
    conn.object_store.delete_object(item.name, container=item.container)
    return t("Object {name} deleted", name=item.name)


def container_options(conn: Connection, item: Any) -> Options:
    names = sorted(c.name for c in conn.object_store.containers())
    current = attr(item, "container")
    return [
        (n, n) for n in ([current] if current in names else []) + [n for n in names if n != current]
    ]


# --- definitions --------------------------------------------------------------------


def _upload_action(needs_item: bool) -> Action:
    return Action(
        "u",
        "Upload",
        upload,
        needs_item=needs_item,
        fields=[
            Field("path", "Local file or directory", required=True),
            Field(
                "name",
                "Object name",
                help="Empty = the file or directory name, inside the current folder",
            ),
        ],
    )


def _open_folder(entry: Any) -> dict[str, Any] | None:
    if not attr(entry, "is_dir"):
        return None
    return {"container": entry.container, "prefix": entry.name}


CONTAINER = ResourceKind(
    key="object_store.container",
    title="Containers",
    service="object-store",
    aliases=("containers", "swift", "buckets"),
    status=None,
    id_attr="name",
    enter="o",
    list=list_containers,
    columns=[
        Column("Name", "name"),
        Column("Objects", "count"),
        Column("Size MiB", lambda c: round((attr(c, "bytes") or 0) / 2**20, 1)),
        Column("Access", container_access),
        Column("Policy", lambda c: _info(c, "policy")),
        Column("Quota", container_quota),
        Column("Versioning", lambda c: _info(c, "versioning")),
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
        _upload_action(needs_item=True),
        Action(
            "p",
            "Set access",
            set_access,
            confirm=True,
            fields=[
                Field(
                    "access",
                    "Access",
                    "select",
                    True,
                    default=lambda c: "public" if _info(c, "public") else "private",
                    options=lambda _c, _i: [("private", "private"), ("public", "public")],
                    help="public = anyone can read and list the objects, without a token",
                )
            ],
        ),
        Action("i", "Details", container_details, output=True),
        Action(
            "ctrl+d",
            "Delete",
            delete_container,
            confirm=True,
            destructive=True,
            fields=[Field("objects", "Also delete every object in it", "bool")],
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
    enter="o",
    list=list_entries,
    columns=[
        Column("Name", entry_label),
        Column("Size KiB", lambda e: "" if e.is_dir else round(e.bytes / 1024, 1)),
        Column("Type", entry_type),
        Column("Modified", "last_modified"),
    ],
    children=[Child("o", "Open folder", "object_store.object", _open_folder)],
    actions=[
        _upload_action(needs_item=False),
        Action(
            "N",
            "Create folder",
            create_folder,
            needs_item=False,
            fields=[Field("name", "Folder name", required=True)],
        ),
        Action(
            "D",
            "Download",
            download_object,
            fields=[Field("path", "Destination (file or directory)", required=True, default=".")],
        ),
        Action(
            "c",
            "Copy",
            copy_object,
            fields=[
                Field(
                    "container", "Destination container", "select", True, options=container_options
                ),
                Field("name", "Destination name", required=True, default=lambda e: e.name),
            ],
        ),
        Action(
            "e",
            "Edit metadata",
            edit_metadata,
            confirm=True,
            fields=[
                Field(
                    "metadata",
                    "Metadata",
                    "textarea",
                    load=metadata_text,
                    help="One per line: key: value. Keys left out are removed",
                )
            ],
        ),
        Action(
            "x",
            "Set expiry",
            set_expiry,
            confirm=True,
            fields=[Field("days", "Delete after days (0 = never)", "int", default=30)],
        ),
        Action(
            "t",
            "Temporary URL",
            temp_url,
            output=True,
            fields=[
                Field("minutes", "Valid for minutes", "int", default=60),
                Field(
                    "create_key",
                    "Create an account temp URL key if missing",
                    "bool",
                    help="Writes X-Account-Meta-Temp-URL-Key on the account",
                ),
            ],
        ),
        Action("i", "Details", object_details, output=True),
        Action("ctrl+d", "Delete", delete_entry, confirm=True, destructive=True),
    ],
)

KINDS = [CONTAINER, OBJECT]
