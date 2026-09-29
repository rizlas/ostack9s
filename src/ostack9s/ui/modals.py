"""Modal screens: fuzzy selection, confirmation, forms, text viewer."""

from __future__ import annotations

from typing import Any

from rich.markup import escape
from rich.syntax import Syntax
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, OptionList, Select, Static, TextArea
from textual.widgets.option_list import Option

from ..i18n import t
from ..privacy import mask
from ..resources import Field
from ..resources.base import Options


def fuzzy_match(query: str, text: str) -> bool:
    """True when the characters of ``query`` appear in order in ``text``."""
    it = iter(text.lower())
    return all(ch in it for ch in query.lower() if not ch.isspace())


class FuzzySelect(ModalScreen[str | None]):
    """Filterable list: Enter picks, Esc cancels."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, options: Options, current: str | None = None) -> None:
        super().__init__()
        self.title_text = title
        self.options = options
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog fuzzy"):
            yield Label(self.title_text, classes="dialog-title")
            yield Input(placeholder=t("search…"), id="fuzzy-input")
            yield OptionList(id="fuzzy-list")

    def on_mount(self) -> None:
        self._fill("")
        self.query_one(Input).focus()

    def _fill(self, query: str) -> None:
        olist = self.query_one(OptionList)
        olist.clear_options()
        matches = [(mask(lbl), val) for lbl, val in self.options if fuzzy_match(query, mask(lbl))]
        for lbl, val in matches:
            mark = "● " if val == self.current else "  "
            olist.add_option(Option(mark + escape(lbl), id=val))
        if matches:
            idx = next((i for i, (_, v) in enumerate(matches) if v == self.current), 0)
            olist.highlighted = idx if not query else 0

    @on(Input.Changed, "#fuzzy-input")
    def _changed(self, event: Input.Changed) -> None:
        self._fill(event.value)

    @on(Input.Submitted, "#fuzzy-input")
    def _submitted(self) -> None:
        olist = self.query_one(OptionList)
        if olist.highlighted is not None:
            self.dismiss(olist.get_option_at_index(olist.highlighted).id)

    @on(OptionList.OptionSelected)
    def _selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def on_key(self, event: Any) -> None:
        # Arrows move the list even while the search field has focus.
        moves = {
            "up": "action_cursor_up",
            "down": "action_cursor_down",
            "pageup": "action_page_up",
            "pagedown": "action_page_down",
        }
        if event.key in moves:
            getattr(self.query_one(OptionList), moves[event.key])()
            event.stop()

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "confirm", "Yes"),
        Binding("n,escape", "cancel", "No"),
    ]

    def __init__(self, message: str, destructive: bool = False) -> None:
        super().__init__()
        self.message = message
        self.destructive = destructive

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog confirm" + (" destructive" if self.destructive else "")):
            yield Static(self.message, classes="dialog-title")
            yield Static(t("[b]y[/b] confirm   [b]n[/b]/[b]Esc[/b] cancel"), classes="hint")
            with Horizontal(classes="buttons"):
                yield Button(
                    t("Confirm"), variant="error" if self.destructive else "primary", id="ok"
                )
                yield Button(t("Cancel"), id="cancel")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    @on(Button.Pressed, "#ok")
    def action_confirm(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(False)


class FormScreen(ModalScreen[dict[str, Any] | None]):
    """Form built from the ``Field`` list of an action. Ctrl+S submits, Esc cancels.

    ``options`` holds the already loaded options of select fields; when loading
    failed (e.g. 403) the field becomes a free text input.
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("ctrl+s", "submit", "Submit", priority=True),
    ]

    def __init__(
        self,
        title: str,
        fields: list[Field],
        defaults: dict[str, Any],
        options: dict[str, Options | None],
        errors: dict[str, str] | None = None,
    ) -> None:
        super().__init__()
        self.title_text = title
        self.fields = fields
        self.defaults = defaults
        self.options = options
        self.load_errors = errors or {}

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog form"):
            yield Label(self.title_text, classes="dialog-title")
            with VerticalScroll(classes="form-body"):
                for f in self.fields:
                    yield from self._field_widgets(f)
            yield Static("", id="form-error", classes="error")
            yield Static(
                t("[b]Ctrl+S[/b] submit   [b]Esc[/b] cancel   [b]Tab[/b] next field"),
                classes="hint",
            )
            with Horizontal(classes="buttons"):
                yield Button(t("Submit"), variant="primary", id="ok")
                yield Button(t("Cancel"), id="cancel")

    def _field_widgets(self, f: Field) -> ComposeResult:
        label = t(f.label) + (" *" if f.required else "")
        default = self.defaults.get(f.name)
        wid = f"field-{f.name}"
        if f.kind == "bool":
            yield Checkbox(label, value=bool(default), id=wid)
        else:
            yield Label(label, classes="field-label")
            opts = self.options.get(f.name)
            if f.kind == "select" and opts is not None:
                values = {v for _, v in opts}
                yield Select(
                    [(mask(lbl), v) for lbl, v in opts],
                    value=default if default in values else Select.NULL,
                    allow_blank=not f.required or default not in values,
                    prompt=t("choose…"),
                    id=wid,
                )
            elif f.kind == "textarea":
                yield TextArea(str(default or ""), id=wid, classes="field-textarea")
            else:
                placeholder = t("(ID or name)") if f.kind == "select" else ""
                yield Input(
                    "" if default is None else str(default),
                    placeholder=placeholder,
                    type="integer" if f.kind == "int" else "text",
                    id=wid,
                )
        notes = [t(f.help)] if f.help else []
        if f.name in self.load_errors:
            notes.append(
                t("options unavailable ({error}): enter the ID", error=self.load_errors[f.name])
            )
        if notes:
            yield Static(escape(" · ".join(notes)), classes="field-help")

    def on_mount(self) -> None:
        first = self.query(
            ".form-body > Input, .form-body > Select, .form-body > Checkbox, .form-body > TextArea"
        )
        if first:
            first.first().focus()

    def _collect(self) -> dict[str, Any] | str:
        values: dict[str, Any] = {}
        for f in self.fields:
            widget = self.query_one(f"#field-{f.name}")
            if isinstance(widget, Checkbox):
                values[f.name] = widget.value
                continue
            if isinstance(widget, Select):
                raw = widget.value
                value: Any = None if raw in (Select.NULL, Select.BLANK) else raw
            elif isinstance(widget, TextArea):
                value = widget.text
            elif isinstance(widget, Input):
                value = widget.value.strip()
            else:
                continue
            if f.kind == "int" and value not in (None, ""):
                try:
                    value = int(value)
                except ValueError:
                    return t("{field}: invalid number", field=t(f.label))
            if f.required and value in (None, ""):
                return t("{field}: required field", field=t(f.label))
            values[f.name] = value
        return values

    @on(Button.Pressed, "#ok")
    def action_submit(self) -> None:
        result = self._collect()
        if isinstance(result, str):
            self.query_one("#form-error", Static).update(escape(result))
            return
        self.dismiss(result)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class TextScreen(ModalScreen[None]):
    """Text or YAML viewer. ``c`` copies to the clipboard (OSC 52)."""

    BINDINGS = [
        Binding("escape,q", "close", "Close"),
        Binding("c", "copy", "Copy"),
    ]

    def __init__(self, title: str, text: str, language: str | None = None) -> None:
        super().__init__()
        self.title_text = title
        self.text = text
        self.language = language

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog viewer"):
            yield Label(mask(self.title_text), classes="dialog-title")
            body: Any
            shown = mask(self.text)
            if self.language:
                body = Syntax(shown, self.language, theme="ansi_dark", word_wrap=True)
            else:
                body = shown
            with VerticalScroll(id="viewer-scroll"):
                yield Static(body, markup=False, id="viewer-body")
            yield Static(t("[b]c[/b] copy   [b]Esc[/b] close"), classes="hint")

    def on_mount(self) -> None:
        self.query_one("#viewer-scroll").focus()

    def action_copy(self) -> None:
        self.app.copy_to_clipboard(self.text)
        self.notify(t("Copied to clipboard"))

    def action_close(self) -> None:
        self.dismiss(None)
