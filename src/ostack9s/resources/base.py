"""Declarative model of the resources shown in the TUI.

Each resource type (``ResourceKind``) describes how to list it, which columns to
show, which actions are available and which child resources can be opened. The
UI is generic and only works on these descriptions.

Labels (titles, action and field labels, help texts) are written in English and
translated by the UI at render time through :func:`ostack9s.i18n.t`.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from openstack.connection import Connection

FieldKind = Literal["text", "int", "bool", "select", "textarea"]
Options = list[tuple[str, str]]


def attr(obj: Any, path: str, default: Any = None) -> Any:
    """Read a nested attribute (``"flavor.original_name"``) from resources or dicts."""
    cur = obj
    for part in path.split("."):
        if cur is None:
            return default
        if isinstance(cur, dict):
            cur = cur.get(part)
            continue
        val = getattr(cur, part, None)
        if val is None:
            # SDK resources also accept the original API keys.
            try:
                val = cur[part]
            except (TypeError, KeyError, AttributeError):
                val = None
        cur = val
    return default if cur is None else cur


def fmt(value: Any) -> str:
    """Convert a value into a compact string for a table cell."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (list, tuple, set)):
        return ", ".join(fmt(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, default=str, separators=(",", ":"))
    return str(value)


def to_plain(obj: Any) -> Any:
    """Convert an SDK resource into plain Python structures (for YAML)."""
    if hasattr(obj, "to_dict"):
        obj = obj.to_dict()
    return json.loads(json.dumps(obj, default=str))


@dataclass
class Column:
    title: str
    get: str | Callable[[Any], Any]
    width: int | None = None

    def value(self, item: Any) -> str:
        raw = self.get(item) if callable(self.get) else attr(item, self.get)
        return fmt(raw)


@dataclass
class Field:
    """Input form field of an action."""

    name: str
    label: str
    kind: FieldKind = "text"
    required: bool = False
    # Initial value, or a function (item) -> value. For actions that do not need
    # a selected item, the item is the parent resource of the view (or None).
    default: Any = None
    help: str = ""
    # Option loader for ``select`` fields: (conn, item) -> [(label, value)]
    options: Callable[[Connection, Any], Options] | None = None
    # Initial value read from the cloud: (conn, item) -> value (overrides ``default``).
    load: Callable[[Connection, Any], Any] | None = None


@dataclass
class Action:
    """Operation on a resource (or on the view, when ``needs_item`` is False).

    ``run`` receives (connection, selected item, form values) and may return a
    string: shown in a viewer when ``output`` is True, as a notification
    otherwise. When ``needs_item`` is False the item passed is the parent
    resource of the current view (or None).
    """

    key: str
    label: str
    run: Callable[[Connection, Any, dict[str, Any]], str | None]
    fields: list[Field] = field(default_factory=list)
    confirm: bool = False
    destructive: bool = False
    needs_item: bool = True
    output: bool = False


@dataclass
class Child:
    """Navigation to a dependent resource (e.g. network -> subnets).

    ``query`` builds, from the parent resource, the filters passed to the child list.
    It returns None when the child does not apply to that row (Enter then shows
    the YAML details).
    """

    key: str
    label: str
    kind: str
    query: Callable[[Any], dict[str, Any] | None]


# (connection, filters) -> resources. Filters are empty for top level views.
ListFn = Callable[[Connection, dict[str, Any]], Iterable[Any]]


@dataclass
class ResourceKind:
    key: str
    title: str
    service: str
    list: ListFn
    columns: list[Column]
    actions: list[Action] = field(default_factory=list)
    children: list[Child] = field(default_factory=list)
    aliases: tuple[str, ...] = ()
    status: str | None = "status"
    # When True the view only makes sense when opened from a parent resource.
    requires_parent: bool = False
    # Child opened with Enter (when missing, Enter shows the YAML details).
    enter: str | None = None
    id_attr: str = "id"

    def item_id(self, item: Any) -> str:
        return str(attr(item, self.id_attr, "") or attr(item, "name", ""))

    def item_label(self, item: Any) -> str:
        return str(attr(item, "name", "") or self.item_id(item))

    def action_for_key(self, key: str) -> Action | None:
        return next((a for a in self.actions if a.key == key), None)

    def child_for_key(self, key: str) -> Child | None:
        return next((c for c in self.children if c.key == key), None)


def status_style(status: str) -> str:
    """Rich style for a resource status."""
    s = status.upper()
    if s in {
        "ERROR",
        "ERROR_DELETING",
        "ERROR_EXTENDING",
        "FAILED",
        "DOWN",
        "OFFLINE",
        "VIOLATED",
        "HIGH",
    }:
        return "bold red"
    if s in {"SHUTOFF", "STOPPED", "SHELVED", "SHELVED_OFFLOADED", "PAUSED", "SUSPENDED"}:
        return "dim"
    if s in {"ACTIVE", "AVAILABLE", "IN-USE", "ONLINE", "UP", "ENABLED", "OK"}:
        return "green"
    if s.endswith("ING") or s.startswith("PENDING") or s in {"BUILD", "RESIZE"}:
        return "yellow"
    if s in {"VERIFY_RESIZE", "SHARED_HOST", "SPREAD", "MEDIUM"}:
        return "bold yellow"
    return ""


def by_name(items: Iterable[Any], label: Callable[[Any], str] | None = None) -> Options:
    """Convert resources into (label, id) options sorted by name."""
    out = []
    for it in items:
        name = label(it) if label else (attr(it, "name") or attr(it, "id"))
        out.append((str(name), str(attr(it, "id"))))
    return sorted(out, key=lambda o: o[0].lower())


def none_if_empty(value: Any) -> Any:
    return None if value in ("", None) else value
