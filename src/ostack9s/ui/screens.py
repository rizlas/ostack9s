"""Resource menu, network topology and password screens."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from .. import resources, topology
from ..cloud import CloudManager, Context
from ..i18n import t
from ..privacy import mask

# Quick resource menu: (group, [(hotkey, target)]). A target is a resource key or
# one of the special entries below.
SPECIAL = {
    "topology": "Network topology",
    "overview": "Overview (all projects)",
    "search": "Search all projects",
    "region": "Switch region",
    "project": "Switch project",
    "cloud": "Switch cloud",
}
MENU: list[tuple[str, list[tuple[str, str]]]] = [
    (
        "Compute",
        [
            ("s", "compute.server"),
            ("f", "compute.flavor"),
            ("k", "compute.keypair"),
            ("g", "compute.server_group"),
        ],
    ),
    (
        "Storage",
        [
            ("v", "block_storage.volume"),
            ("n", "block_storage.snapshot"),
            ("b", "block_storage.backup"),
            ("i", "image.image"),
            ("o", "object_store.container"),
        ],
    ),
    (
        "Network",
        [
            ("w", "network.network"),
            ("u", "network.subnet"),
            ("r", "network.router"),
            ("p", "network.port"),
            ("a", "network.floating_ip"),
            ("x", "network.security_group"),
            ("h", "network.rbac_policy"),
            ("t", "topology"),
        ],
    ),
    (
        "Load balancing",
        [
            ("l", "load_balancer.loadbalancer"),
            ("L", "load_balancer.listener"),
            ("P", "load_balancer.pool"),
        ],
    ),
    (
        "Identity and secrets",
        [("e", "key_manager.secret"), ("c", "identity.application_credential")],
    ),
    (
        "Checks",
        [("U", "checks.unused"), ("X", "checks.security")],
    ),
    (
        "Context",
        [("O", "overview"), ("S", "search"), ("R", "region"), ("J", "project"), ("C", "cloud")],
    ),
]


def menu_label(target: str) -> str:
    if target in SPECIAL:
        return t(SPECIAL[target])
    return t(resources.get(target).title)


class ResourceMenu(ModalScreen[str | None]):
    """Grouped list of resources: press the hotkey, or move and press Enter."""

    BINDINGS = [Binding("escape,m", "cancel", "Close")]

    def __init__(self, current: str | None = None) -> None:
        super().__init__()
        self.current = current
        self.hotkeys = {key: target for _, entries in MENU for key, target in entries}

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog menu"):
            yield Label(t("Go to…"), classes="dialog-title")
            options: list[Option | None] = []
            for group, entries in MENU:
                if options:
                    options.append(None)
                options.append(Option(Text(t(group), style="bold orange1"), disabled=True))
                for key, target in entries:
                    text = Text()
                    text.append(f"  <{key}> ", style="bold dodger_blue1")
                    text.append(menu_label(target), style="bold" if target == self.current else "")
                    options.append(Option(text, id=target))
            yield OptionList(*options, id="menu-list")
            yield Static(
                t("[b]letter[/b] go   [b]↑↓ Enter[/b] choose   [b]Esc[/b] close"), classes="hint"
            )

    def on_mount(self) -> None:
        olist = self.query_one(OptionList)
        olist.focus()
        for idx in range(olist.option_count):
            if olist.get_option_at_index(idx).id == (self.current or "compute.server"):
                olist.highlighted = idx
                break

    def on_key(self, event: Any) -> None:
        key = event.key
        if key.startswith("shift+") and len(key) == 7:
            key = key[-1].upper()
        if key in self.hotkeys:
            event.stop()
            self.dismiss(self.hotkeys[key])

    @on(OptionList.OptionSelected)
    def _selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)


def _free_path(base: Path) -> Path:
    """First non-existing path: never overwrite an existing file."""
    if not base.exists():
        return base
    for n in range(1, 1000):
        candidate = base.with_name(f"{base.stem}-{n}{base.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(base)


class TopologyScreen(ModalScreen[None]):
    """Network topology tree. ``c`` copies Mermaid, ``s`` saves .mmd and .dot."""

    BINDINGS = [
        Binding("escape,q", "close", "Close"),
        Binding("ctrl+r,r", "reload", "Reload"),
        Binding("c", "copy", "Copy Mermaid"),
        Binding("s", "save", "Save"),
    ]

    def __init__(self, manager: CloudManager, ctx: Context) -> None:
        super().__init__()
        self.manager = manager
        self.ctx = ctx
        self.topo: topology.Topology | None = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog viewer"):
            yield Label(
                t("Network topology · {context}", context=mask(self.ctx.label())),
                classes="dialog-title",
            )
            with VerticalScroll(id="topology-scroll"):
                yield Static(t("loading…"), id="topology-body")
            yield Static(
                t(
                    "[b]c[/b] copy Mermaid   [b]s[/b] save .mmd/.dot   [b]r[/b] reload   [b]Esc[/b] close"  # noqa: E501
                ),
                classes="hint",
            )

    def on_mount(self) -> None:
        self.query_one("#topology-scroll").focus()
        self.load()

    @work(thread=True, exclusive=True)
    def load(self) -> None:
        try:
            topo = topology.build(self.manager.connection(self.ctx), self.ctx.label())
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(
                self.query_one("#topology-body", Static).update,
                Text(t("Error: {error}", error=exc), style="red"),
            )
            return
        self.app.call_from_thread(self._show, topo)

    def _show(self, topo: topology.Topology) -> None:
        self.topo = topo
        self.query_one("#topology-body", Static).update(topology.render(topo, mask))

    def action_reload(self) -> None:
        self.query_one("#topology-body", Static).update(t("loading…"))
        self.load()

    def action_copy(self) -> None:
        if self.topo is not None:
            self.app.copy_to_clipboard(mask(topology.to_mermaid(self.topo)))
            self.notify(t("Mermaid diagram copied to clipboard"))

    def action_save(self) -> None:
        if self.topo is None:
            return
        stem = f"topology-{self.ctx.project_name}-{self.ctx.region}"
        saved = []
        for suffix, render in ((".mmd", topology.to_mermaid), (".dot", topology.to_dot)):
            path = _free_path(Path.cwd() / f"{stem}{suffix}")
            path.write_text(mask(render(self.topo)))
            saved.append(path.name)
        self.notify(t("Saved {files}", files=", ".join(saved)))

    def action_close(self) -> None:
        self.dismiss(None)


class PasswordScreen(ModalScreen[str | None]):
    """Password prompt for password based ``clouds.yaml`` entries (kept in memory)."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, cloud: str, user: str = "") -> None:
        super().__init__()
        self.cloud = cloud
        self.user = user

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog confirm"):
            title = t("Password for cloud {cloud}", cloud=self.cloud)
            if self.user:
                title += f" ({self.user})"
            yield Label(title, classes="dialog-title")
            yield Input(password=True, id="password")
            yield Static(t("Kept in memory only, never written to disk."), classes="hint")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @on(Input.Submitted)
    def _submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value or None)

    def action_cancel(self) -> None:
        self.dismiss(None)
