"""GPU accounting from flavor extra specs.

Clouds expose GPUs either through PCI passthrough aliases
(``pci_passthrough:alias: gpu_a100:2``) or through placement resource classes
(``resources:VGPU: 1``, ``resources:CUSTOM_GPU_A100: 1``). Nova has no GPU quota
readable by non-admin users, so ostack9s shows usage counted from the flavors
of the project's servers.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from typing import Any

from .resources.base import attr

# PCI aliases are also used for NICs (SR-IOV): only count names that look like GPUs.
GPU_NAME = re.compile(r"gpu|nvidia|amd|rtx|\b[ahlvtp]\d{2,3}\b|\bmi\d{2,3}\b", re.IGNORECASE)


def gpu_label(name: str) -> str:
    """``gpu_a100`` -> ``A100``, ``CUSTOM_GPU_A30`` -> ``A30``, ``VGPU`` -> ``VGPU``."""
    label = re.sub(r"^(custom_)?(pgpu_|gpu[_-]?)?", "", name, flags=re.IGNORECASE)
    return (label or name).upper()


def gpus_from_specs(specs: dict[str, Any] | None) -> Counter[str]:
    out: Counter[str] = Counter()
    if not specs:
        return out
    alias = specs.get("pci_passthrough:alias")
    if alias:
        for part in str(alias).split(","):
            name, _, count = part.strip().partition(":")
            if name and GPU_NAME.search(name):
                try:
                    out[gpu_label(name)] += int(count or 1)
                except ValueError:
                    continue
    for key, value in specs.items():
        if key.startswith("resources:") and "GPU" in key.upper():
            try:
                amount = int(value)
            except (TypeError, ValueError):
                continue
            if amount > 0:
                out[gpu_label(key.split(":", 1)[1])] += amount
    return out


def flavor_gpus(flavor: Any) -> Counter[str]:
    return gpus_from_specs(attr(flavor, "extra_specs"))


def server_gpus(server: Any) -> Counter[str]:
    """GPUs of a server, from the flavor embedded in the server (microversion 2.47+)."""
    return flavor_gpus(attr(server, "flavor"))


def count_gpus(servers: Iterable[Any]) -> Counter[str]:
    total: Counter[str] = Counter()
    for server in servers:
        total.update(server_gpus(server))
    return total


def gpu_text(gpus: Counter[str]) -> str:
    return ", ".join(f"{model}×{n}" for model, n in sorted(gpus.items()))
