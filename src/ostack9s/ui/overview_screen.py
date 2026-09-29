"""Global overview screen: every cloud × project × region."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import DataTable, Label, Static

from ..cloud import CloudManager, Context
from ..i18n import t
from ..overview import Summary, Usage, short_error, summarize
from ..privacy import mask
from .widgets import quota_rows, status_counts, usage_value

# Default security group: every project uses at least one, so it does not count
# as "activity" when deciding whether a region is empty.
IDLE_METRICS = {"security_groups"}


def is_empty(summary: Summary | None) -> bool:
    """True when the project has nothing in this region (usually: no quota there)."""
    if summary is None or summary.pending:
        return False
    used = any(u.used for k, u in summary.usage.items() if k not in IDLE_METRICS)
    return not used and not summary.servers


def _usage_key(usage: Usage | None) -> tuple[float, float]:
    """Sort quotas by how full they are, then by absolute usage."""
    if usage is None:
        return (-1.0, -1.0)
    ratio = usage.ratio
    return (ratio if ratio is not None else -0.5, usage.used)


class OverviewScreen(ModalScreen[Context | None]):
    """Enter switches to the context of the selected row."""

    BINDINGS = [
        Binding("escape,f1,q", "close", "Close"),
        Binding("ctrl+r", "reload", "Reload"),
        Binding("ctrl+o", "sort", "Sort"),
        Binding("e", "toggle_empty", "Hide empty regions"),
    ]

    def __init__(self, manager: CloudManager, current: Context | None) -> None:
        super().__init__()
        self.manager = manager
        self.current = current
        self.contexts: dict[str, Context] = {}
        self.summaries: dict[str, Summary] = {}
        self.sort: tuple[str, bool] | None = None
        self.hide_empty = False

    def compose(self) -> ComposeResult:
        yield Label(t("Overview: all projects and regions"), classes="dialog-title")
        yield DataTable(cursor_type="row", zebra_stripes=True, id="overview-table")
        yield Static(
            t(
                "[b]Enter[/b] go to context   [b]Ctrl+O[/b]/click sort   "
                "[b]e[/b] hide empty regions   [b]Ctrl+R[/b] reload   [b]Esc[/b] close"
            ),
            classes="hint",
        )

    def on_mount(self) -> None:
        self.query_one(DataTable).focus()
        self.load()

    def action_reload(self) -> None:
        self.summaries = {}
        self.load()

    @work(thread=True, exclusive=True)
    def load(self) -> None:
        try:
            contexts = self.manager.all_contexts()
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(self.notify, short_error(exc), severity="error")
            return
        self.app.call_from_thread(self._set_contexts, contexts)
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [
                pool.submit(summarize, self.manager.connection(ctx), ctx) for ctx in contexts
            ]
            for fut in as_completed(futures):
                try:
                    summary = fut.result()
                except Exception as exc:  # noqa: BLE001
                    self.app.call_from_thread(self.notify, short_error(exc), severity="error")
                    continue
                self.app.call_from_thread(self._apply, summary)

    @staticmethod
    def _row_key(ctx: Context) -> str:
        return f"{ctx.cloud}|{ctx.project_id}|{ctx.region}"

    def _set_contexts(self, contexts: list[Context]) -> None:
        self.contexts = {self._row_key(ctx): ctx for ctx in contexts}
        self._redraw()

    def _apply(self, summary: Summary) -> None:
        key = self._row_key(summary.ctx)
        if key in self.contexts:
            self.summaries[key] = summary
            self._redraw()

    # --- columns ---------------------------------------------------------------

    def _show_cloud(self) -> bool:
        """The cloud column is useful only when entry names differ from projects."""
        return any(ctx.cloud != ctx.project_name for ctx in self.contexts.values())

    def _columns(self) -> list[tuple[str, str, Callable[[str], Any]]]:
        """(key, title, sort key) of every column, in display order."""

        def ctx_attr(name: str) -> Callable[[str], Any]:
            return lambda k: getattr(self.contexts[k], name).lower()

        def metric(name: str) -> Callable[[str], Any]:
            def key(k: str) -> Any:
                summary = self.summaries.get(k)
                return _usage_key(summary.usage.get(name) if summary else None)

            return key

        def total(name: str) -> Callable[[str], Any]:
            def key(k: str) -> Any:
                summary = self.summaries.get(k)
                counter = getattr(summary, name, None) if summary else None
                return sum(counter.values()) if counter else 0

            return key

        cols: list[tuple[str, str, Callable[[str], Any]]] = [
            ("project", t("Project"), ctx_attr("project_name"))
        ]
        if self._show_cloud():
            cols.append(("cloud", t("Cloud"), ctx_attr("cloud")))
        cols.append(("region", t("Region"), ctx_attr("region")))
        cols.append(("gpus", t("GPUs in use"), total("gpus")))
        keys = {k for s in self.summaries.values() for k in s.usage}
        for key, label, unit, vtype in quota_rows(keys):
            title = f"{t(label)} {vtype}".rstrip()
            if unit:
                title += f" ({unit})"
            cols.append((key, title, metric(key)))
        cols.append(("servers", t("Servers"), total("servers")))
        cols.append(("note", t("Note"), lambda k: ""))
        return cols

    def _cells(self, key: str, columns: list[str]) -> list[Text]:
        ctx = self.contexts[key]
        summary = self.summaries.get(key)
        empty = is_empty(summary)
        base = "bold" if ctx == self.current else ("grey42" if empty else "")
        out = []
        for col in columns:
            if col == "project":
                out.append(Text(mask(ctx.project_name), style=base))
            elif col == "cloud":
                out.append(Text(ctx.cloud, style=base))
            elif col == "region":
                out.append(Text(ctx.region, style=base))
            elif summary is None:
                out.append(Text("…", style="dim"))
            elif col == "gpus":
                gpus = ", ".join(f"{m} {n}" for m, n in sorted((summary.gpus or {}).items()))
                out.append(Text(gpus or "-", style="bold magenta" if gpus else "dim"))
            elif col == "servers":
                out.append(status_counts(summary) or Text("-", style="dim"))
            elif col == "note":
                note = ", ".join(f"{k}: {v}" for k, v in summary.errors.items())
                if empty and not note:
                    note = t("no resources in this region")
                out.append(Text(mask(note), style="dim red" if summary.errors else "grey42"))
            else:
                value = usage_value(summary.usage.get(col))
                if empty:
                    value.stylize("grey42")
                out.append(value)
        return out

    # --- rendering ---------------------------------------------------------------

    def _redraw(self) -> None:
        table = self.query_one(DataTable)
        selected = None
        if table.row_count:
            try:
                selected = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            except Exception:  # noqa: BLE001 - cursor out of range
                selected = None
        if selected is None and self.current is not None:
            selected = self._row_key(self.current)
        columns = self._columns()
        keys = list(self.contexts)
        if self.hide_empty:
            keys = [k for k in keys if not is_empty(self.summaries.get(k))]
        if self.sort is not None:
            col, reverse = self.sort
            sort_key = next((c[2] for c in columns if c[0] == col), None)
            if sort_key is not None:
                keys.sort(key=sort_key, reverse=reverse)
        table.clear(columns=True)
        for col, title, _ in columns:
            marker = ""
            if self.sort and self.sort[0] == col:
                marker = " ↓" if self.sort[1] else " ↑"
            table.add_column(title + marker, key=col)
        names = [c[0] for c in columns]
        for n, key in enumerate(keys):
            table.add_row(*self._cells(key, names), key=key)
            if key == selected:
                table.move_cursor(row=n)

    @on(DataTable.HeaderSelected)
    def _header(self, event: DataTable.HeaderSelected) -> None:
        col = str(event.column_key.value)
        # First click: descending for numbers (fullest first), ascending for names.
        numeric = col not in ("project", "cloud", "region", "note")
        if self.sort and self.sort[0] == col:
            self.sort = (col, not self.sort[1])
        else:
            self.sort = (col, numeric)
        self._redraw()

    def action_sort(self) -> None:
        names = [c[0] for c in self._columns() if c[0] != "note"]
        if self.sort is None:
            self.sort = (names[0], False)
        elif self.sort[0] in names and names.index(self.sort[0]) + 1 < len(names):
            nxt = names[names.index(self.sort[0]) + 1]
            self.sort = (nxt, nxt not in ("project", "cloud", "region"))
        else:
            self.sort = None
        self._redraw()

    def action_toggle_empty(self) -> None:
        self.hide_empty = not self.hide_empty
        self._redraw()

    @on(DataTable.RowSelected)
    def _selected(self, event: DataTable.RowSelected) -> None:
        self.dismiss(self.contexts.get(str(event.row_key.value)))

    def action_close(self) -> None:
        self.dismiss(None)
