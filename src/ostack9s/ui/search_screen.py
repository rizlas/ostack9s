"""Search a name, ID or IP address in every cloud × project × region."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import DataTable, Input, Label, Static

from .. import resources
from ..cloud import CloudManager
from ..i18n import t
from ..overview import short_error
from ..privacy import mask
from ..search import MIN_QUERY, Hit, search_context


class SearchScreen(ModalScreen[Hit | None]):
    """Enter on a result switches to its context and opens its resource view."""

    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, manager: CloudManager, query: str = "") -> None:
        super().__init__()
        self.manager = manager
        self.query_text = query
        self.hits: list[Hit] = []
        self.done = 0
        self.total = 0
        self.failed = 0

    def compose(self) -> ComposeResult:
        yield Label(t("Search in every project and region"), classes="dialog-title")
        yield Input(self.query_text, placeholder=t("name, ID or IP address"), id="search-input")
        yield DataTable(cursor_type="row", zebra_stripes=True, id="search-table")
        yield Static("", id="search-status", classes="hint")
        yield Static(
            t("[b]Enter[/b] search / go to the resource   [b]Tab[/b] results   [b]Esc[/b] close"),
            classes="hint",
        )

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        for key, title in (
            ("project", t("Project")),
            ("region", t("Region")),
            ("type", t("Type")),
            ("name", t("Name")),
            ("match", t("Match")),
            ("id", "ID"),
        ):
            table.add_column(title, key=key)
        if len(self.query_text) >= MIN_QUERY:
            self.start(self.query_text)
        else:
            self.query_one(Input).focus()

    @on(Input.Submitted, "#search-input")
    def _submitted(self, event: Input.Submitted) -> None:
        query = event.value.strip()
        if len(query) < MIN_QUERY:
            self.notify(t("Type at least {count} characters", count=MIN_QUERY), severity="warning")
            return
        self.start(query)

    def start(self, query: str) -> None:
        self.query_text = query
        self.hits = []
        self.done = self.total = self.failed = 0
        self.query_one(DataTable).clear()
        self.query_one(DataTable).focus()
        self._status()
        self.run_search(query)

    @work(thread=True, exclusive=True)
    def run_search(self, query: str) -> None:
        try:
            contexts = self.manager.all_contexts()
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(self.notify, short_error(exc), severity="error")
            return
        self.app.call_from_thread(self._set_total, len(contexts))
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [
                pool.submit(lambda c=ctx: search_context(self.manager.connection(c), c, query))
                for ctx in contexts
            ]
            for fut in as_completed(futures):
                try:
                    hits, failed = fut.result()
                except Exception:  # noqa: BLE001 - unreachable context
                    hits, failed = [], ["all"]
                self.app.call_from_thread(self._apply, query, hits, bool(failed))

    def _set_total(self, total: int) -> None:
        self.total = total
        self._status()

    def _apply(self, query: str, hits: list[Hit], failed: bool) -> None:
        if not self.is_attached or query != self.query_text:
            return
        self.done += 1
        self.failed += failed
        table = self.query_one(DataTable)
        # Exact matches first, then in arrival order.
        for hit in sorted(hits, key=lambda h: not h.exact):
            self.hits.append(hit)
            kind = resources.get(hit.kind)
            style = "bold" if hit.exact else ""
            table.add_row(
                Text(mask(hit.ctx.project_name), style=style),
                Text(hit.ctx.region, style=style),
                Text(t(kind.title), style=style),
                Text(mask(hit.name), style=style),
                Text(mask(hit.match), style="bold green" if hit.exact else "green"),
                Text(hit.id, style="dim"),
                key=str(len(self.hits) - 1),
            )
        self._status()

    def _status(self) -> None:
        if not self.query_text:
            text = ""
        else:
            text = t(
                "{count} results · {done}/{total} contexts",
                count=len(self.hits),
                done=self.done,
                total=self.total or "…",
            )
            if self.failed:
                text += "  · " + t(
                    "some resources not readable in {count} contexts", count=self.failed
                )
        self.query_one("#search-status", Static).update(text)

    @on(DataTable.RowSelected, "#search-table")
    def _selected(self, event: DataTable.RowSelected) -> None:
        self.dismiss(self.hits[int(str(event.row_key.value))])

    def action_close(self) -> None:
        self.dismiss(None)
