"""Main Textual application, with k9s style navigation.

The header shows the context and the available keys. ``:`` opens the command
bar (with completion, Tab accepts) to jump to a resource (``:volumes``), switch
region (``:region region-a``), project (``:project name``) or cloud (``:cloud
name``). ``/`` uses the same bar to filter rows, ``m`` opens the resource menu
and ``d`` toggles the describe pane next to the table.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Literal

import yaml
from openstack.connection import Connection
from rich.markup import escape
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.suggester import SuggestFromList
from textual.timer import Timer
from textual.widgets import DataTable, Input, Static

from .. import privacy, resources
from ..cloud import CloudManager, Context, Target, parse_time, time_left
from ..gpu import count_gpus
from ..helptext import GLOBAL_HELP
from ..i18n import LANGUAGES, set_language, t
from ..overview import Summary, server_counts, short_error, summarize
from ..privacy import mask
from ..resources import Action, Field, ResourceKind
from ..resources.base import Options, status_style, to_plain
from ..search import Hit
from .modals import ConfirmScreen, FormScreen, FuzzySelect, TextScreen
from .overview_screen import OverviewScreen
from .screens import PasswordScreen, ResourceMenu, TopologyScreen
from .search_screen import SearchScreen
from .widgets import CommandInput, Crumbs, DescribePane, HeaderBar, Hint, QuotaPanel

STATUS_TITLES = {"Status", "Provisioning", "Operating"}
SERVERS = "compute.server"

# Command bar words (besides resource names): word -> command.
COMMANDS = {
    "region": "region",
    "reg": "region",
    "project": "project",
    "proj": "project",
    "cloud": "cloud",
    "ctx": "cloud",
    "overview": "overview",
    "ov": "overview",
    "topology": "topology",
    "topo": "topology",
    "search": "search",
    "find": "search",
    "menu": "menu",
    "lang": "lang",
    "privacy": "privacy",
    "help": "help",
    "q": "quit",
    "quit": "quit",
}

GLOBAL_HINTS = [
    Hint(":", "command", "bold magenta"),
    Hint("m", "menu", "bold magenta"),
    Hint("/", "filter", "bold magenta"),
    Hint("d", "describe", "bold magenta"),
    Hint("a", "actions", "bold magenta"),
    Hint("?", "help", "bold magenta"),
]

CommandMode = Literal["command", "filter"]


@dataclass
class View:
    """A view in the navigation stack (root or child resource)."""

    kind: ResourceKind
    query: dict[str, Any] = field(default_factory=dict)
    parent: Any = None
    path: list[str] = field(default_factory=list)
    scope: str = ""
    items: list[Any] = field(default_factory=list)
    error: str | None = None
    loaded: bool = False
    filter: str = ""
    sort: tuple[int, bool] | None = None
    cursor_id: str | None = None


def error_text(exc: BaseException) -> str:
    details = getattr(exc, "details", None)
    text = str(details or exc) or type(exc).__name__
    # Some services (e.g. Glance) return HTML messages.
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text))
    code = getattr(exc, "status_code", None)
    return (f"[{code}] " if code else "") + text.strip()[:500]


def normalize_key(key: str) -> str:
    """``shift+a`` (kitty keyboard protocol) is the same as ``A``."""
    if key.startswith("shift+") and len(key) == 7:
        return key[-1].upper()
    return key


def kind_words(kind: ResourceKind) -> list[str]:
    """Words that open a resource from the command bar."""
    words = [*kind.aliases, kind.key, kind.title.lower().replace(" ", "-")]
    return list(dict.fromkeys(words))


def resolve_kind(word: str) -> ResourceKind | None:
    word = word.lower()
    for kind in resources.top_level():
        if word in kind_words(kind):
            return kind
    return None


class ResourceTable(DataTable):
    """Resource table: keys not handled by the table go to the actions."""

    def on_key(self, event: Any) -> None:
        app = self.app
        if isinstance(app, OstdApp) and app.dispatch_resource_key(normalize_key(event.key)):
            event.stop()
            event.prevent_default()


class OstdApp(App[None]):
    CSS_PATH = "app.tcss"
    TITLE = "ostack9s"
    # Ctrl+P toggles privacy mode instead of opening Textual's command palette.
    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("colon", "command", "Command"),
        Binding("slash", "filter", "Filter"),
        Binding("m", "menu", "Menu"),
        Binding("d", "toggle_describe", "Describe"),
        Binding("tab", "switch_focus", "Switch focus", show=False),
        Binding("a", "action_menu", "Actions"),
        Binding("y", "describe", "YAML"),
        Binding("question_mark", "help", "Help"),
        Binding("left_square_bracket", "cycle_region(-1)", "Previous region"),
        Binding("right_square_bracket", "cycle_region(1)", "Next region"),
        Binding("ctrl+r", "reload", "Reload"),
        Binding("ctrl+o", "sort", "Sort"),
        Binding("ctrl+p", "toggle_privacy", "Privacy mode"),
        Binding("escape", "go_back", "Back"),
        Binding("f1", "overview", "Overview"),
        Binding("f2", "select_cloud", "Cloud"),
        Binding("f3", "toggle_panel", "Quota panel"),
        Binding("f4", "select_project", "Project"),
        Binding("f5", "select_region", "Region"),
    ]

    def __init__(
        self,
        manager: CloudManager,
        cloud: str | None = None,
        region: str | None = None,
        kind: str = SERVERS,
        refresh: float = 30.0,
    ) -> None:
        super().__init__()
        self.manager = manager
        self.initial_cloud = cloud
        self.initial_region = region
        self.refresh_seconds = refresh
        self.ctx: Context | None = None
        self.user = ""
        self.auth = ""
        self.token_expires: datetime | None = None
        # Clouds already checked for expiring application credentials.
        self.expiry_checked: set[str] = set()
        self.regions: list[str] = []
        self.targets: list[Target] = []
        self.summaries: dict[Context, Summary] = {}
        # Last items of each list, shown at once while fresh data is fetched.
        self.list_cache: dict[tuple[Context, str, str], list[Any]] = {}
        root = resources.get(kind)
        self.stack: list[View] = [View(root, path=[root.title])]
        self.row_items: dict[str, Any] = {}
        self.command_mode: CommandMode = "command"
        self._busy = False
        self._describe_timer: Timer | None = None

    # --- layout -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield HeaderBar(id="header")
        with Horizontal(id="cmdbar"):
            yield Static(":", id="prompt")
            yield CommandInput(id="cmd")
        with Horizontal(id="body"):
            yield QuotaPanel(id="quota")
            with Vertical(id="main"):
                with Horizontal(id="split"):
                    yield ResourceTable(cursor_type="row", zebra_stripes=True, id="table")
                    yield DescribePane(id="describe")
                yield Static("", id="message")
        yield Crumbs(id="crumbs")

    def on_mount(self) -> None:
        self._q("#cmdbar").display = False
        self._q("#message", Static).display = False
        self._q(DescribePane).display = False
        self._q(ResourceTable).focus()
        self._render_chrome()
        self.startup()
        if self.refresh_seconds > 0:
            self.set_interval(self.refresh_seconds, self._tick)
        self.set_interval(max(60.0, self.refresh_seconds * 2), self.refresh_quota)

    def on_resize(self) -> None:
        self._render_header()

    @property
    def view(self) -> View:
        return self.stack[-1]

    def _q(self, selector: Any, expect: Any = None) -> Any:
        """Query the main screen, even while a modal screen is on top of it."""
        screen = self.screen_stack[0] if self.screen_stack else self.screen
        return screen.query_one(selector, expect) if expect else screen.query_one(selector)

    # --- context ----------------------------------------------------------

    @work(group="context")
    async def startup(self) -> None:
        try:
            clouds = await asyncio.to_thread(self.manager.cloud_names)
        except Exception as exc:  # noqa: BLE001
            self._show_message(t("Cannot read clouds.yaml: {error}", error=error_text(exc)))
            return
        if not clouds:
            self._show_message(t("No cloud found in clouds.yaml"))
            return
        cloud = self.initial_cloud
        if cloud is None:
            if len(clouds) == 1:
                cloud = clouds[0]
            else:
                cloud = await self.push_screen_wait(
                    FuzzySelect(t("Choose the cloud"), [(c, c) for c in clouds])
                )
                if cloud is None:
                    self.exit()
                    return
        await self.switch_context(cloud, None, self.initial_region)

    async def _ensure_password(self, cloud: str) -> bool:
        if not await asyncio.to_thread(self.manager.needs_password, cloud):
            return True
        password = await self.push_screen_wait(PasswordScreen(cloud))
        if not password:
            return False
        self.manager.set_password(cloud, password)
        return True

    async def switch_context(self, cloud: str, project_id: str | None, region: str | None) -> None:
        self._set_status(t("connecting…"))
        if not await self._ensure_password(cloud):
            self._set_status()
            return

        def resolve() -> tuple[Context, list[str], str, str, datetime | None]:
            ctx = self.manager.context(cloud, project_id, region)
            locked = self.manager.is_project_locked(cloud)
            try:
                expires = self.manager.token_expires(ctx)
            except Exception:  # noqa: BLE001 - only shown in the header
                expires = None
            return (
                ctx,
                self.manager.regions(ctx.cloud, ctx.project_id),
                self.manager.user_name(cloud),
                t("application credential") if locked else self.manager.auth_type(cloud),
                expires,
            )

        try:
            ctx, regions, user, auth, expires = await asyncio.to_thread(resolve)
        except Exception as exc:  # noqa: BLE001
            self.notify(error_text(exc), title=t("Connection failed"), severity="error", timeout=15)
            self._set_status()
            return
        known = {x.cloud for x in self.targets}
        privacy.register_names("user", [user])
        privacy.register_names("project", [ctx.project_name])
        self.ctx = ctx
        self.regions = regions
        self.user = user
        self.auth = auth
        self.token_expires = expires
        root = self.stack[0]
        self.stack = [View(root.kind, path=[root.kind.title], sort=root.sort)]
        if ctx.cloud not in known:
            self.targets = [Target(ctx.cloud, p) for p in self.manager.projects(ctx.cloud)]
            self.load_targets()
        self._update_suggestions()
        self._render_table()
        self._render_chrome()
        self.refresh_quota()
        self.load_view()
        self.prefetch_lists()
        if ctx.cloud not in self.expiry_checked:
            self.expiry_checked.add(ctx.cloud)
            self.check_credentials(ctx.cloud)

    @work(thread=True, group="expiry")
    def check_credentials(self, cloud: str) -> None:
        """Warn about application credentials that expire soon (Horizon does not)."""
        try:
            expiring = self.manager.expiring_credentials(cloud)
        except Exception:  # noqa: BLE001 - restricted credentials cannot list them
            return
        for cred, own in expiring:
            left = time_left(parse_time(cred.expires_at))
            if own:
                text = t(
                    "The credential of cloud {cloud} expires in {left}", cloud=cloud, left=left
                )
            else:
                text = t(
                    "Application credential {name} expires in {left}", name=cred.name, left=left
                )
            self.call_from_thread(self._warn, text)

    def _warn(self, text: str) -> None:
        if self.screen_stack:  # the app may be shutting down
            self.notify(text, severity="warning", timeout=15)

    @work(thread=True, exclusive=True, group="targets")
    def load_targets(self) -> None:
        """Group the projects of sibling clouds.yaml entries (same Keystone and user)."""
        ctx = self.ctx
        if ctx is None:
            return
        try:
            targets = self.manager.targets(ctx.cloud)
        except Exception:  # noqa: BLE001 - keep the projects of the current entry
            return
        self.call_from_thread(self._apply_targets, targets)

    def _apply_targets(self, targets: list[Target]) -> None:
        if not self.screen_stack:  # app shutting down
            return
        if self.ctx is not None and self.ctx.cloud in {x.cloud for x in targets}:
            self.targets = targets
            privacy.register_names("project", [x.project.name for x in targets])
            self._update_suggestions()

    # --- data loading -------------------------------------------------------

    @staticmethod
    def _cache_key(ctx: Context, view: View) -> tuple[Context, str, str]:
        return (ctx, view.kind.key, json.dumps(view.query, sort_keys=True, default=str))

    def load_view(self, quiet: bool = False) -> None:
        if self.ctx is None:
            return
        view = self.view
        cached = self.list_cache.get(self._cache_key(self.ctx, view))
        if cached is not None and not view.loaded:
            view.items = cached
            view.loaded = True
            self._render_table()
        self._busy = True
        if not quiet:
            self._set_status(t("refreshing…") if view.loaded else t("loading…"))
        self._fetch(view, self.ctx)

    @work(thread=True, exclusive=True, group="prefetch")
    def prefetch_lists(self) -> None:
        """Load the root view in the other regions, so switching region is instant."""
        ctx = self.ctx
        if ctx is None:
            return
        view = View(self.stack[0].kind)
        others = [replace(ctx, region=r) for r in self.regions if r != ctx.region]

        def fetch(other: Context) -> None:
            try:
                items = list(view.kind.list(self.manager.connection(other), {}))
            except Exception:  # noqa: BLE001 - prefetch is best effort
                return
            self.list_cache[self._cache_key(other, view)] = items

        if others:
            with ThreadPoolExecutor(max_workers=len(others)) as pool:
                list(pool.map(fetch, others))

    @work(thread=True, exclusive=True, group="list")
    def _fetch(self, view: View, ctx: Context) -> None:
        error = None
        items: list[Any] = []
        try:
            conn = self.manager.connection(ctx)
            items = list(view.kind.list(conn, view.query))
        except Exception as exc:  # noqa: BLE001
            error = error_text(exc)
        self.call_from_thread(self._apply_items, view, ctx, items, error)

    def _apply_items(self, view: View, ctx: Context, items: list[Any], error: str | None) -> None:
        if not self.screen_stack:  # app shutting down
            return
        self._busy = False
        if view is not self.view or ctx != self.ctx:
            return
        if error is None:
            self.list_cache[self._cache_key(ctx, view)] = items
        elif view.loaded and view.items:
            # Keep showing the previous data, the error goes to a notification.
            self.notify(error, title=t("Refresh failed"), severity="warning")
            error = None
            items = view.items
        view.items = items
        view.error = error
        view.loaded = True
        if view.kind.key == SERVERS and not view.query and error is None:
            # The server list is already here: reuse it for the quota panel counts.
            summary = self.summaries.get(ctx) or Summary(ctx)
            self.summaries[ctx] = replace(
                summary, servers=server_counts(items), gpus=count_gpus(items)
            )
            self._render_panel()
        self._render_table()
        self._set_status()

    def _tick(self) -> None:
        if self.screen is self.screen_stack[0] and not self._busy and self.ctx is not None:
            self.load_view(quiet=True)

    @work(thread=True, exclusive=True, group="quota")
    def refresh_quota(self) -> None:
        ctx = self.ctx
        if ctx is None:
            return
        include_servers = self.stack[0].kind.key != SERVERS

        def update(summary: Summary) -> None:
            self.call_from_thread(self._apply_summary, summary)

        try:
            summarize(self.manager.connection(ctx), ctx, update, include_servers)
        except Exception as exc:  # noqa: BLE001
            update(Summary(ctx, errors={"all": short_error(exc)}))
        # Warm the other regions so that switching region shows data at once.
        others = [replace(ctx, region=r) for r in self.regions if r != ctx.region]
        if others:
            with ThreadPoolExecutor(max_workers=len(others)) as pool:
                for other in others:
                    pool.submit(self._prefetch, other)

    def _prefetch(self, ctx: Context) -> None:
        try:
            summary = summarize(self.manager.connection(ctx), ctx, include_servers=False)
        except Exception:  # noqa: BLE001 - prefetch is best effort
            return
        self.call_from_thread(self._apply_summary, summary)

    def _apply_summary(self, summary: Summary) -> None:
        if not self.screen_stack:  # app shutting down
            return
        old = self.summaries.get(summary.ctx)
        if summary.servers is None and old is not None and old.servers is not None:
            summary = replace(summary, servers=old.servers, gpus=old.gpus)
        self.summaries[summary.ctx] = summary
        if summary.ctx == self.ctx:
            self._render_panel()

    # --- rendering ----------------------------------------------------------

    def _render_chrome(self) -> None:
        self._render_header()
        self._render_panel()
        self._set_status()

    def _render_header(self) -> None:
        kind = self.view.kind
        hints = list(GLOBAL_HINTS)
        for child in kind.children:
            hints.append(Hint(child.key, child.label, "bold cyan"))
        for action in kind.actions:
            style = "bold red" if action.destructive else "bold dodger_blue1"
            hints.append(Hint(action.key, action.label, style))
        auth = self.auth
        left = time_left(self.token_expires)
        if left:
            auth = f"{auth} · {t('token {left}', left=left)}"
        self._q(HeaderBar).show(self.ctx, self.user, auth, hints, privacy.is_enabled())

    def _render_panel(self) -> None:
        summary = self.summaries.get(self.ctx) if self.ctx else None
        self._q(QuotaPanel).show(self.ctx, summary)

    def _set_status(self, status: str = "") -> None:
        self._q(Crumbs).show(self.view.path, status)
        self._render_title()

    def _render_title(self) -> None:
        view = self.view
        scope = mask(view.scope) or (self.ctx.region if self.ctx else "")
        shown, total = len(self.row_items), len(view.items)
        count = f"{shown}/{total}" if shown != total else str(total)
        title = f" [b]{escape(t(view.kind.title).lower())}[/b]([b magenta]{escape(scope)}[/])"
        title += f"\\[[b]{count if view.loaded else '…'}[/b]] "
        if view.filter:
            title += f"[b yellow]</{escape(view.filter)}>[/] "
        self._q("#main").border_title = title

    def _show_message(self, text: str) -> None:
        msg = self._q("#message", Static)
        msg.update(mask(text))
        msg.display = bool(text)

    def _cell(self, kind: ResourceKind, col_index: int, value: str) -> Text | str:
        column = kind.columns[col_index]
        if column.title in STATUS_TITLES or column.get == kind.status:
            return Text(value, style=status_style(value.split(" ")[0]))
        return value

    def _render_table(self) -> None:
        view = self.view
        kind = view.kind
        table = self._q(ResourceTable)
        # Remember the selected row to restore it after the refresh.
        current = self.selected_item()
        if current is not None:
            view.cursor_id = kind.item_id(current)
        table.clear(columns=True)
        for i, column in enumerate(kind.columns):
            marker = ""
            if view.sort and view.sort[0] == i:
                marker = "↓" if view.sort[1] else "↑"
            table.add_column(t(column.title).upper() + marker, key=str(i))
        rows = []
        for item in view.items:
            cells = [mask(c.value(item)) for c in kind.columns]
            if view.filter and not any(view.filter.lower() in c.lower() for c in cells):
                continue
            rows.append((item, cells))
        if view.sort:
            idx, reverse = view.sort
            rows.sort(key=lambda r: _sort_key(r[1][idx]), reverse=reverse)
        self.row_items = {}
        cursor_row = 0
        for n, (item, cells) in enumerate(rows):
            key = f"{n}:{kind.item_id(item)}"
            self.row_items[key] = item
            table.add_row(*(self._cell(kind, i, v) for i, v in enumerate(cells)), key=key)
            if view.cursor_id and kind.item_id(item) == view.cursor_id:
                cursor_row = n
        if rows:
            table.move_cursor(row=cursor_row)
        if view.error:
            self._show_message(f"[red]{escape(t('Error:'))}[/red] {escape(view.error)}")
        elif view.loaded and not view.items:
            self._show_message(f"[dim]{escape(t('No items'))}[/dim]")
        else:
            self._show_message("")
        self._render_header()
        self._render_title()
        self._update_describe()

    def selected_item(self) -> Any:
        table = self._q(ResourceTable)
        if table.row_count == 0:
            return None
        try:
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        except Exception:  # noqa: BLE001 - empty table or cursor out of range
            return None
        return self.row_items.get(str(row_key.value))

    # --- describe pane --------------------------------------------------------

    def _update_describe(self) -> None:
        if not self.screen_stack:  # app shutting down
            return
        pane = self._q(DescribePane)
        if not pane.display:
            return
        item = self.selected_item()
        pane.show(_yaml(item) if item is not None else "")
        pane.border_title = mask(self.view.kind.item_label(item)) if item is not None else ""

    @on(DataTable.RowHighlighted, "#table")
    def _row_highlighted(self) -> None:
        # Debounced: holding an arrow key must not render YAML for every row.
        if self._describe_timer is not None:
            self._describe_timer.stop()
        self._describe_timer = self.set_timer(0.12, self._update_describe)

    def action_toggle_describe(self) -> None:
        pane = self._q(DescribePane)
        pane.display = not pane.display
        if pane.display:
            self._update_describe()
        else:
            self._q(ResourceTable).focus()

    def action_switch_focus(self) -> None:
        pane = self._q(DescribePane)
        table = self._q(ResourceTable)
        if not pane.display:
            return
        if table.has_focus:
            pane.focus()
        else:
            table.focus()

    # --- command bar ------------------------------------------------------

    def _update_suggestions(self) -> None:
        words: list[str] = []
        for kind in resources.top_level():
            words += kind_words(kind)
        words += [f"region {r}" for r in self.regions]
        words += [f"project {mask(target.project.name)}" for target in self.targets]
        words += [f"cloud {c}" for c in self.manager.cloud_names()]
        words += [f"lang {lang}" for lang in LANGUAGES]
        words += ["overview", "topology", "search", "menu", "help", "quit"]
        self._q("#cmd", CommandInput).suggester = SuggestFromList(words, case_sensitive=False)

    def _open_bar(self, mode: CommandMode) -> None:
        self.command_mode = mode
        bar = self._q("#cmdbar")
        bar.display = True
        bar.set_class(mode == "filter", "filter")
        self._q("#prompt", Static).update("/" if mode == "filter" else ":")
        box = self._q("#cmd", CommandInput)
        box.value = self.view.filter if mode == "filter" else ""
        box.placeholder = (
            t("filter rows")
            if mode == "filter"
            else t("resource, region <r>, project <p>, cloud <c>, search <text>, lang <en|it>")
        )
        box.focus()

    def close_command(self, cancel: bool = False) -> None:
        if cancel and self.command_mode == "filter":
            self.view.filter = ""
            self._render_table()
        self._q("#cmdbar").display = False
        self._q(ResourceTable).focus()

    def action_command(self) -> None:
        self._open_bar("command")

    def action_filter(self) -> None:
        self._open_bar("filter")

    @on(Input.Changed, "#cmd")
    def _cmd_changed(self, event: Input.Changed) -> None:
        if self.command_mode == "filter":
            self.view.filter = event.value
            self._render_table()

    @on(Input.Submitted, "#cmd")
    def _cmd_submitted(self, event: Input.Submitted) -> None:
        mode = self.command_mode
        self.close_command()
        if mode == "command":
            self.run_command(event.value)

    def run_command(self, text: str) -> None:
        text = text.strip().lstrip(":").strip()
        if not text:
            return
        word, _, arg = text.partition(" ")
        arg = arg.strip()
        command = COMMANDS.get(word.lower())
        handlers: dict[str, Callable[[], None]] = {
            "quit": self.exit,
            "overview": self.action_overview,
            "topology": self.action_topology,
            "search": lambda: self.action_search(arg),
            "menu": self.action_menu,
            "help": self.action_help,
            "region": lambda: self._command_region(arg),
            "project": lambda: self._command_project(arg),
            "cloud": lambda: self._command_cloud(arg),
            "lang": lambda: self._command_lang(arg),
            "privacy": lambda: self._command_privacy(arg),
        }
        if command is not None:
            handlers[command]()
        elif (kind := resolve_kind(word)) is not None:
            self.show_kind(kind)
        else:
            self.notify(t("Unknown command: {command}", command=escape(text)), severity="warning")

    def show_kind(self, kind: ResourceKind) -> None:
        self.stack = [View(kind, path=[kind.title])]
        self._render_table()
        self._set_status()
        self.load_view()

    def _command_region(self, arg: str) -> None:
        if self.ctx is None:
            return
        if not arg:
            self.action_select_region()
            return
        match = next((r for r in self.regions if r.lower() == arg.lower()), None)
        if match is None:
            self.notify(t("Unknown region: {region}", region=escape(arg)), severity="warning")
        elif match != self.ctx.region:
            self._switch(self.ctx.cloud, self.ctx.project_id, match)

    def _find_target(self, arg: str) -> Target | None:
        arg = arg.lower()
        return next(
            (
                x
                for x in self.targets
                if arg
                in (x.project.name.lower(), x.project.id.lower(), mask(x.project.name).lower())
            ),
            None,
        )

    def _command_project(self, arg: str) -> None:
        if self.ctx is None:
            return
        if not arg:
            self.action_select_project()
            return
        target = self._find_target(arg)
        if target is None:
            if self._project_locked_warning():
                return
            self.notify(t("Unknown project: {project}", project=escape(arg)), severity="warning")
        elif target.project.id != self.ctx.project_id:
            self._switch(target.cloud, target.project.id, self.ctx.region)

    def _command_cloud(self, arg: str) -> None:
        if not arg:
            self.action_select_cloud()
            return
        if arg not in self.manager.cloud_names():
            self.notify(t("Unknown cloud: {cloud}", cloud=escape(arg)), severity="warning")
        elif self.ctx is None or arg != self.ctx.cloud:
            self._switch(arg, None, None)

    def _command_privacy(self, arg: str) -> None:
        value = {"on": True, "off": False}.get(arg.lower())
        self.set_privacy(not privacy.is_enabled() if value is None else value)

    def action_toggle_privacy(self) -> None:
        self.set_privacy(not privacy.is_enabled())

    def set_privacy(self, enabled: bool) -> None:
        privacy.set_enabled(enabled)
        self._update_suggestions()
        self._render_table()
        self._render_chrome()
        self.notify(t("Privacy mode on") if enabled else t("Privacy mode off"))

    def notify(self, message: str, **kwargs: Any) -> None:  # type: ignore[override]
        super().notify(mask(str(message)), **kwargs)

    def _command_lang(self, arg: str) -> None:
        if arg not in LANGUAGES:
            self.notify(
                t("Available languages: {languages}", languages=", ".join(LANGUAGES)),
                severity="warning",
            )
            return
        set_language(arg)
        self._render_table()
        self._render_chrome()
        self.notify(t("Language: {lang}", lang=arg))

    # --- resource keys ------------------------------------------------------

    def dispatch_resource_key(self, key: str) -> bool:
        kind = self.view.kind
        child = kind.child_for_key(key)
        if child is not None:
            self.open_child(child.key)
            return True
        action = kind.action_for_key(key)
        if action is not None:
            self.perform_action(action)
            return True
        return False

    @on(DataTable.RowSelected, "#table")
    def _row_selected(self) -> None:
        kind = self.view.kind
        if kind.enter:
            self.open_child(kind.enter)
        else:
            self.action_describe()

    @on(DataTable.HeaderSelected, "#table")
    def _header_selected(self, event: DataTable.HeaderSelected) -> None:
        idx = int(str(event.column_key.value))
        view = self.view
        reverse = view.sort is not None and view.sort[0] == idx and not view.sort[1]
        view.sort = (idx, reverse)
        self._render_table()

    def open_child(self, key: str) -> None:
        child = self.view.kind.child_for_key(key)
        item = self.selected_item()
        if child is None or item is None:
            return
        kind = resources.get(child.kind)
        label = self.view.kind.item_label(item)
        view = View(
            kind,
            query=child.query(item),
            parent=item,
            path=[*self.view.path, kind.title],
            scope=label,
        )
        self.stack.append(view)
        self._render_table()
        self._set_status()
        self.load_view()

    # --- actions ------------------------------------------------------------

    @work(group="action")
    async def perform_action(self, action: Action) -> None:
        ctx = self.ctx
        if ctx is None:
            return
        view = self.view
        item = self.selected_item() if action.needs_item else view.parent
        if action.needs_item and item is None:
            self.notify(t("No item selected"), severity="warning")
            return
        label = view.kind.item_label(item) if action.needs_item else t(view.kind.title)
        title = f"{t(action.label)} · {label}"
        try:
            conn = await asyncio.to_thread(self.manager.connection, ctx)
        except Exception as exc:  # noqa: BLE001
            self.notify(error_text(exc), severity="error")
            return
        values: dict[str, Any] = {}
        if action.fields:
            self._set_status(t("preparing form…"))
            options, errors = await asyncio.to_thread(_load_options, action.fields, conn, item)
            defaults = _defaults(action.fields, item)
            self._set_status()
            form = FormScreen(title, action.fields, defaults, options, errors)
            result = await self.push_screen_wait(form)
            if result is None:
                return
            values = result
        if action.confirm:
            message = (
                f"{escape(t(action.label))}: [b]{escape(label)}[/b]?\n\n"
                f"[dim]{escape(ctx.label())}[/dim]"
            )
            if not await self.push_screen_wait(ConfirmScreen(message, action.destructive)):
                return
        self._set_status(f"{t(action.label)}…")
        try:
            out = await asyncio.to_thread(action.run, conn, item, values)
        except Exception as exc:  # noqa: BLE001
            self._set_status()
            self.notify(
                escape(error_text(exc)), title=t(action.label), severity="error", timeout=15
            )
            return
        if action.output and out:
            self.push_screen(TextScreen(title, out))
        elif out:
            self.notify(escape(out), title=t(action.label))
        self.load_view(quiet=True)
        self.set_timer(5, lambda: self.load_view(quiet=True))
        self.set_timer(6, self.refresh_quota)

    # --- global commands ----------------------------------------------------

    def action_menu(self) -> None:
        def chosen(target: str | None) -> None:
            if target is None:
                return
            special: dict[str, Callable[[], None]] = {
                "topology": self.action_topology,
                "overview": self.action_overview,
                "search": self.action_search,
                "region": self.action_select_region,
                "project": self.action_select_project,
                "cloud": self.action_select_cloud,
            }
            if target in special:
                special[target]()
            else:
                self.show_kind(resources.get(target))

        self.push_screen(ResourceMenu(self.stack[0].kind.key), chosen)

    def action_topology(self) -> None:
        if self.ctx is not None:
            self.push_screen(TopologyScreen(self.manager, self.ctx))

    def action_action_menu(self) -> None:
        kind = self.view.kind
        entries: list[tuple[str, str]] = []
        for child in kind.children:
            entries.append((f"{child.key:>7}  → {t(child.label)}", f"child:{child.key}"))
        for action in kind.actions:
            entries.append((f"{action.key:>7}  {t(action.label)}", f"action:{action.key}"))
        if not entries:
            self.notify(t("No actions for this resource"))
            return

        def chosen(value: str | None) -> None:
            if value:
                self.dispatch_resource_key(value.split(":", 1)[1])

        self.push_screen(FuzzySelect(t("Actions · {kind}", kind=t(kind.title)), entries), chosen)

    def action_describe(self) -> None:
        item = self.selected_item()
        if item is None:
            return
        self.push_screen(TextScreen(self.view.kind.item_label(item), _yaml(item), "yaml"))

    def action_go_back(self) -> None:
        if self.view.filter:
            self.view.filter = ""
            self._render_table()
            return
        if len(self.stack) > 1:
            self.stack.pop()
            self._render_table()
            self._set_status()
            self.load_view()

    def action_sort(self) -> None:
        view = self.view
        ncols = len(view.kind.columns)
        if view.sort is None:
            view.sort = (0, False)
        elif not view.sort[1]:
            view.sort = (view.sort[0], True)
        elif view.sort[0] + 1 < ncols:
            view.sort = (view.sort[0] + 1, False)
        else:
            view.sort = None
        self._render_table()

    def action_reload(self) -> None:
        self.load_view()
        self.refresh_quota()

    def action_toggle_panel(self) -> None:
        panel = self._q(QuotaPanel)
        panel.display = not panel.display

    def action_overview(self) -> None:
        def chosen(ctx: Context | None) -> None:
            if ctx is not None:
                self._switch(ctx.cloud, ctx.project_id, ctx.region)

        self.push_screen(OverviewScreen(self.manager, self.ctx), chosen)

    def action_search(self, query: str = "") -> None:
        def chosen(hit: Hit | None) -> None:
            if hit is not None:
                self.run_worker(self._go_to(hit), group="context")

        self.push_screen(SearchScreen(self.manager, query), chosen)

    async def _go_to(self, hit: Hit) -> None:
        """Open the resource view of a search result, in its context."""
        if hit.ctx != self.ctx:
            await self.switch_context(hit.ctx.cloud, hit.ctx.project_id, hit.ctx.region)
            if self.ctx != hit.ctx:  # switch failed, already notified
                return
        kind = resources.get(hit.kind)
        self.stack = [View(kind, path=[kind.title], filter=hit.filter)]
        self._render_table()
        self._set_status()
        self.load_view()

    def _switch(self, cloud: str, project_id: str | None, region: str | None) -> None:
        self.run_worker(self.switch_context(cloud, project_id, region), group="context")

    def action_select_cloud(self) -> None:
        clouds = self.manager.cloud_names()
        current = self.ctx.cloud if self.ctx else None

        def chosen(cloud: str | None) -> None:
            if cloud and cloud != current:
                self._switch(cloud, None, None)

        self.push_screen(FuzzySelect(t("Cloud"), [(c, c) for c in clouds], current), chosen)

    def _project_locked_warning(self) -> bool:
        ctx = self.ctx
        if ctx is None or len(self.targets) > 1:
            return False
        if not self.manager.is_project_locked(ctx.cloud):
            return False
        self.notify(
            t(
                "The credential of this cloud (application credential) is bound to project "
                "{project}. For other projects add one clouds.yaml entry per project, or use "
                "a credential not bound to a project.",
                project=ctx.project_name,
            ),
            severity="warning",
            timeout=10,
        )
        return True

    def action_select_project(self) -> None:
        ctx = self.ctx
        if ctx is None or self._project_locked_warning():
            return

        def chosen(value: str | None) -> None:
            if not value:
                return
            cloud, _, project_id = value.partition("|")
            if project_id != ctx.project_id:
                self._switch(cloud, project_id, ctx.region)

        options = [
            (
                x.project.name + (f"  ({x.cloud})" if x.cloud != ctx.cloud else ""),
                f"{x.cloud}|{x.project.id}",
            )
            for x in self.targets
        ]
        current = f"{ctx.cloud}|{ctx.project_id}"
        self.push_screen(FuzzySelect(t("Project"), options, current), chosen)

    def action_select_region(self) -> None:
        if self.ctx is None:
            return
        ctx = self.ctx

        def chosen(region: str | None) -> None:
            if region and region != ctx.region:
                self._switch(ctx.cloud, ctx.project_id, region)

        options = [(r, r) for r in self.regions]
        self.push_screen(FuzzySelect(t("Region"), options, ctx.region), chosen)

    def action_cycle_region(self, step: int) -> None:
        if self.ctx is None or len(self.regions) < 2:
            return
        idx = self.regions.index(self.ctx.region) if self.ctx.region in self.regions else 0
        region = self.regions[(idx + step) % len(self.regions)]
        self._switch(self.ctx.cloud, self.ctx.project_id, region)

    def action_help(self) -> None:
        self.push_screen(TextScreen(t("Help"), help_text(self.view.kind)))


def _yaml(item: Any) -> str:
    return mask(yaml.safe_dump(to_plain(item), sort_keys=True, allow_unicode=True, width=120))


def _sort_key(value: str) -> tuple[int, float | str]:
    try:
        return (0, float(value))
    except ValueError:
        return (1, value.lower())


def _defaults(fields: list[Field], item: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in fields:
        default = f.default
        if callable(default):
            try:
                default = default(item)
            except Exception:  # noqa: BLE001 - default not computable (e.g. no item)
                default = None
        out[f.name] = default
    return out


def _load_options(
    fields: list[Field], conn: Connection, item: Any
) -> tuple[dict[str, Options | None], dict[str, str]]:
    """Load the options of select fields in parallel."""
    options: dict[str, Options | None] = {}
    errors: dict[str, str] = {}
    loaders: dict[str, Callable[[Connection, Any], Options]] = {
        f.name: f.options for f in fields if f.kind == "select" and f.options is not None
    }
    if not loaders:
        return options, errors
    with ThreadPoolExecutor(max_workers=len(loaders)) as pool:
        futures = {pool.submit(fn, conn, item): name for name, fn in loaders.items()}
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                options[name] = fut.result()
            except Exception as exc:  # noqa: BLE001
                options[name] = None
                errors[name] = short_error(exc)
    return options, errors


def help_text(kind: ResourceKind) -> str:
    aliases = [
        f"  {t(k.title):<28} :{', :'.join(kind_words(k)[:3])}" for k in resources.top_level()
    ]
    lines = [t(GLOBAL_HELP), t("Resources"), *aliases, "", t(kind.title)]
    for child in kind.children:
        lines.append(f"  {child.key:<24} → {t(child.label)}")
    for action in kind.actions:
        flag = f"  ({t('confirmation')})" if action.confirm else ""
        lines.append(f"  {action.key:<24} {t(action.label)}{flag}")
    return "\n".join(lines)
