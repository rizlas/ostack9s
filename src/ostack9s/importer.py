"""Merge application credential files downloaded from Horizon into one clouds.yaml.

Horizon names every downloaded entry ``openstack``. This module authenticates
each credential to learn its project, renames the entry after the project and
merges everything into a target file. The target is never overwritten
blindly: a timestamped backup is written first, existing entries are kept and
entries with the same name are skipped unless ``replace`` is set.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from openstack.config import OpenStackConfig
from openstack.connection import Connection


@dataclass
class Imported:
    source: Path
    entry: str
    name: str
    project: str
    status: str  # "added", "replaced", "skipped", "error: …"


def entry_name(project: str, prefix: str = "") -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", project).strip("-").lower()
    return f"{prefix}{name}" if prefix else name


def _project_of(path: Path, entry: str) -> str:
    """Authenticate the entry and return its project name."""
    config = OpenStackConfig(config_files=[str(path)], load_envvars=False)
    conn = Connection(config=config.get_one(cloud=entry), app_name="ostack9s")
    auth: Any = conn.session.auth
    return auth.get_access(conn.session).project_name


def _load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: not a clouds.yaml file")
    data.setdefault("clouds", {})
    return data


def merge(
    sources: list[Path],
    target: Path,
    prefix: str = "",
    replace: bool = False,
    dry_run: bool = False,
    resolve_project=_project_of,
) -> tuple[list[Imported], Path | None]:
    """Merge ``sources`` into ``target``; return the report and the backup path."""
    merged = _load(target) if target.exists() else {"clouds": {}}
    clouds: dict[str, Any] = merged["clouds"]
    report: list[Imported] = []
    for source in sources:
        try:
            entries = _load(source)["clouds"]
        except (OSError, ValueError, yaml.YAMLError) as exc:
            report.append(Imported(source, "", "", "", f"error: {exc}"))
            continue
        for entry, config in entries.items():
            try:
                project = resolve_project(source, entry)
            except Exception as exc:  # noqa: BLE001 - report and continue
                report.append(Imported(source, entry, "", "", f"error: {exc}"))
                continue
            name = entry_name(project, prefix)
            if name in clouds and not replace:
                status = "skipped"
            else:
                status = "replaced" if name in clouds else "added"
                clouds[name] = config
            report.append(Imported(source, entry, name, project, status))
    backup = None
    if not dry_run and any(r.status in ("added", "replaced") for r in report):
        if target.exists():
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = target.with_name(f"{target.name}.bak-{stamp}")
            shutil.copy2(target, backup)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            yaml.safe_dump(merged, fh, sort_keys=False, default_flow_style=False)
        tmp.replace(target)
    return report, backup
