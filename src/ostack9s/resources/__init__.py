"""Registro dei tipi di risorsa disponibili."""

from __future__ import annotations

from . import checks, compute, network, other, storage
from .base import Action, Child, Column, Field, ResourceKind

REGISTRY: dict[str, ResourceKind] = {
    kind.key: kind for mod in (compute, storage, network, other, checks) for kind in mod.KINDS
}


def get(key: str) -> ResourceKind:
    return REGISTRY[key]


def top_level() -> list[ResourceKind]:
    """Tipi selezionabili direttamente (esclusi quelli che richiedono un padre)."""
    return [k for k in REGISTRY.values() if not k.requires_parent]


__all__ = ["REGISTRY", "Action", "Child", "Column", "Field", "ResourceKind", "get", "top_level"]
