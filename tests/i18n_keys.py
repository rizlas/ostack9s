"""Collect every translatable English string (used by the catalog test)."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "ostack9s"


def literal_t_calls() -> set[str]:
    """First argument of every ``t("...")`` call with a literal string."""
    keys: set[str] = set()
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "t"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                keys.add(node.args[0].value)
    return keys


def declared_labels() -> set[str]:
    """Labels declared in data structures and translated at render time."""
    from ostack9s import resources
    from ostack9s.ui import app, screens, widgets

    keys: set[str] = set()
    for kind in resources.REGISTRY.values():
        keys.add(kind.title)
        keys.update(c.title for c in kind.columns)
        keys.update(c.label for c in kind.children)
        for action in kind.actions:
            keys.add(action.label)
            for f in action.fields:
                keys.add(f.label)
                if f.help:
                    keys.add(f.help)
    keys.update(h.label for h in app.GLOBAL_HINTS)
    keys.update(label for _, label, _ in widgets.QUOTA_ROWS)
    keys.update(group for group, _ in screens.MENU)
    keys.update(screens.SPECIAL.values())
    keys.add(app.GLOBAL_HELP)  # re-exported from ostack9s.helptext
    # Labels passed through variables to t().
    keys.update({"Network", "Port", "Rule", "Security group", "Subnet", "Router"})
    keys.update({"Load balancer", "Listener", "Pool"})
    keys.update({"Resize confirmed", "Resize reverted"})
    return keys


def all_keys() -> set[str]:
    return literal_t_calls() | declared_labels()


if __name__ == "__main__":
    for key in sorted(all_keys()):
        print(repr(key))
