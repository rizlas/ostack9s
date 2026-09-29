"""Widgets of the main screen, k9s style."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Input, Static

from .. import __version__
from ..cloud import Context
from ..i18n import t
from ..overview import Summary, Usage
from ..privacy import mask

LOGO = r"""
        _           _    ___
 ___ __| |_ __ _ __| |__/ _ \___
/ _ (_-<  _/ _` / _| / /\_, (_-<
\___/__/\__\__,_\__|_\_\ /_//__/
"""

HINT_ROWS = 6
HINT_COL_WIDTH = 26


@dataclass
class Hint:
    key: str
    label: str
    style: str = "bold dodger_blue1"


def hint_text(hint: Hint) -> Text:
    text = Text()
    text.append(f"<{hint.key}>", style=hint.style)
    text.append(f" {t(hint.label).lower()}", style="grey70")
    return text


class HeaderBar(Static):
    """Header: context on the left, key hints in the middle, logo on the right."""

    def show(
        self,
        ctx: Context | None,
        user: str,
        auth: str,
        hints: list[Hint],
        private: bool = False,
    ) -> None:
        grid = Table.grid(padding=(0, 2))
        info = Table.grid(padding=(0, 1))
        info.add_column(style="bold orange1", no_wrap=True)
        info.add_column(style="bold white", no_wrap=True)
        rows = [
            (t("Cloud:"), ctx.cloud if ctx else "-"),
            (t("Project:"), mask(ctx.project_name) if ctx else "-"),
            (t("Region:"), ctx.region if ctx else "-"),
            (t("User:"), mask(user) or "-"),
            (t("Auth:"), auth or "-"),
            ("ostack9s:", __version__),
        ]
        for label, value in rows:
            info.add_row(label, value)
        grid.add_column(no_wrap=True)

        width = self.size.width or 160
        free = max(0, width - 34 - 34)
        ncols = max(1, free // HINT_COL_WIDTH)
        capacity = ncols * HINT_ROWS
        shown = hints
        if len(hints) > capacity:
            more = Hint("a", t("{count} more…", count=len(hints) - capacity + 1))
            shown = [*hints[: capacity - 1], more]
        columns = [shown[i : i + HINT_ROWS] for i in range(0, len(shown), HINT_ROWS)]
        cells: list[Table | Text] = [info]
        for col in columns:
            grid.add_column(no_wrap=True, width=HINT_COL_WIDTH)
            cells.append(Text("\n").join(hint_text(h) for h in col))
        grid.add_column(no_wrap=True, justify="right")
        logo = Text(LOGO.strip("\n"), style="bold orange1")
        if private:
            logo.append("\n" + t("PRIVACY MODE").center(19), style="bold white on red")
        cells.append(logo)
        grid.add_row(*cells)
        self.update(grid)


class CommandInput(Input):
    """Command bar input: Tab accepts the suggestion, Esc closes."""

    BINDINGS = [
        Binding("tab", "cursor_right", "Complete", show=False, priority=True),
        Binding("escape", "close", "Close", show=False),
    ]

    def action_close(self) -> None:
        close = getattr(self.app, "close_command", None)
        if close is not None:
            close(cancel=True)


# --- quota panel of the current region ---------------------------------------

QUOTA_ROWS = [
    ("instances", "Instances", ""),
    ("cores", "vCPU", ""),
    ("ram", "RAM", "GiB"),
    ("volumes", "Volumes", ""),
    ("gigabytes", "Disk", "GiB"),
    ("floating_ips", "Floating IPs", ""),
    ("networks", "Networks", ""),
    ("security_groups", "Sec. groups", ""),
]
BAR_WIDTH = 10


def quota_rows(keys: Iterable[str]) -> list[tuple[str, str, str, str]]:
    """QUOTA_ROWS plus one row per limited volume type found in ``keys``.

    Rows are (key, label, unit, volume type); volume type rows follow the
    total they belong to, e.g. "gigabytes:Ceph-SSD" after "gigabytes".
    """
    typed: dict[str, list[str]] = {}
    for key in keys:
        metric, _, vtype = key.partition(":")
        if vtype:
            typed.setdefault(metric, []).append(vtype)
    rows = []
    for key, label, unit in QUOTA_ROWS:
        rows.append((key, label, unit, ""))
        rows.extend((f"{key}:{v}", label, unit, v) for v in sorted(typed.get(key, [])))
    return rows


def usage_style(usage: Usage) -> str:
    ratio = usage.ratio
    if ratio is None:
        return "grey70"
    if ratio >= 0.9:
        return "bold red"
    if ratio >= 0.75:
        return "yellow"
    return "green"


def usage_bar(usage: Usage) -> Text:
    ratio = usage.ratio
    if ratio is None:
        return Text("∞".ljust(BAR_WIDTH), style="grey50")
    filled = round(min(ratio, 1.0) * BAR_WIDTH)
    text = Text("█" * filled, style=usage_style(usage))
    text.append("░" * (BAR_WIDTH - filled), style="grey30")
    return text


def usage_value(usage: Usage | None, unit: str = "") -> Text:
    if usage is None:
        return Text("-", style="grey50")
    suffix = f" {unit}" if unit else ""
    return Text(usage.text().replace("/", " / ") + suffix, style=usage_style(usage))


def status_counts(summary: Summary) -> Text | None:
    if not summary.servers:
        return None
    text = Text()
    for i, (status, count) in enumerate(sorted(summary.servers.items())):
        if i:
            text.append(" · ", style="grey50")
        style = "bold red" if status == "ERROR" else "green" if status == "ACTIVE" else "grey70"
        text.append(f"{status} {count}", style=style)
    return text


class QuotaPanel(Static):
    """Usage and limits of the project in the current region.

    Rows are filled in as soon as each service answers.
    """

    def show(self, ctx: Context | None, summary: Summary | None) -> None:
        if ctx is None:
            self.update("")
            return
        self.border_title = t("quota · {region}", region=ctx.region)
        pending = summary.pending if summary else {"compute", "volume", "network", "servers"}
        table = Table.grid(padding=(0, 1))
        table.add_column(style="grey70", no_wrap=True)
        table.add_column(no_wrap=True)
        table.add_column(justify="right", no_wrap=True)
        for key, label, unit, vtype in quota_rows(summary.usage if summary else ()):
            name = f"  {vtype}" if vtype else t(label)
            usage = summary.usage.get(key) if summary else None
            if usage is None:
                waiting = any(s in pending for s in ("compute", "volume", "network"))
                value = Text("…" if waiting else "-", style="grey50")
                table.add_row(name, Text(" " * BAR_WIDTH), value)
                continue
            table.add_row(name, usage_bar(usage), usage_value(usage, unit))
        body = Text()
        if summary and summary.gpus:
            body.append(f"\n{t('GPUs in use')}", style="grey70")
            for model, count in sorted(summary.gpus.items()):
                body.append(f"\n  {model:<10}", style="grey70")
                body.append(f"{count:>5}", style="bold magenta")
            body.append(f"\n  {t('no GPU quota exposed by the cloud')}", style="grey50")
        counts = status_counts(summary) if summary else None
        if counts is not None:
            body.append(f"\n{t('Servers:')} ", style="grey70")
            body.append_text(counts)
        elif "servers" in pending:
            body.append(f"\n{t('Servers:')} …", style="grey50")
        if summary:
            for service, err in summary.errors.items():
                if service != "servers":
                    body.append(f"\n{service}: {err}", style="red")
            if not pending:
                body.append(
                    "\n\n" + t("updated in {seconds}s", seconds=f"{summary.elapsed:.1f}"),
                    style="grey50",
                )
        grid = Table.grid()
        grid.add_row(table)
        grid.add_row(body)
        self.update(grid)


class DescribePane(VerticalScroll):
    """Side pane with the YAML of the highlighted row (like ostui's describe)."""

    def compose(self):  # type: ignore[override]
        yield Static("", id="describe-body")

    def show(self, text: str) -> None:
        body = Syntax(text, "yaml", theme="ansi_dark", word_wrap=True) if text else ""
        self.query_one("#describe-body", Static).update(body)


class Crumbs(Static):
    """Breadcrumbs at the bottom, like k9s, plus a status message."""

    def show(self, path: list[str], status: str = "") -> None:
        text = Text()
        for i, crumb in enumerate(path):
            last = i == len(path) - 1
            style = "bold black on orange1" if last else "bold black on dodger_blue1"
            text.append(f" <{t(crumb).lower()}> ", style=style)
            text.append(" ")
        if status:
            text.append(f"  {status}", style="grey62 italic")
        self.update(text)
