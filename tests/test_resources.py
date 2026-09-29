from types import SimpleNamespace

import pytest

from ostack9s import resources
from ostack9s.resources.base import Column, attr, fmt, status_style
from ostack9s.resources.compute import server_addresses, server_flavor
from ostack9s.ui.app import OstdApp
from ostack9s.ui.modals import fuzzy_match

# Keys handled globally by the app: no resource action may use them.
GLOBAL_KEYS = {b.key for b in OstdApp.BINDINGS} | {"enter", "escape", "tab"}


@pytest.mark.parametrize("kind", resources.REGISTRY.values(), ids=lambda k: k.key)
def test_kind_is_consistent(kind):
    keys = [a.key for a in kind.actions] + [c.key for c in kind.children]
    assert len(keys) == len(set(keys)), f"duplicate keys in {kind.key}"
    assert not set(keys) & GLOBAL_KEYS, f"global keys used in {kind.key}"
    for child in kind.children:
        assert child.kind in resources.REGISTRY
    if kind.enter:
        assert kind.child_for_key(kind.enter) is not None
    for action in kind.actions:
        names = [f.name for f in action.fields]
        assert len(names) == len(set(names))
        for f in action.fields:
            if f.kind == "select":
                assert f.options is not None or f.help, f"{kind.key}/{action.label}/{f.name}"


def test_attr_nested_and_dict():
    obj = SimpleNamespace(flavor={"original_name": "m1.small"}, name="vm")
    assert attr(obj, "flavor.original_name") == "m1.small"
    assert attr(obj, "missing.x", "d") == "d"
    assert attr({"a": {"b": 1}}, "a.b") == 1


def test_fmt():
    assert fmt(None) == ""
    assert fmt(True) == "yes"
    assert fmt(["a", "b"]) == "a, b"
    assert fmt({"k": 1}) == '{"k":1}'


def test_column_callable():
    assert Column("x", lambda o: o * 2).value(3) == "6"


def test_server_helpers():
    server = SimpleNamespace(
        addresses={
            "net": [
                {"addr": "10.0.0.5", "OS-EXT-IPS:type": "fixed"},
                {"addr": "90.1.1.1", "OS-EXT-IPS:type": "floating"},
            ]
        },
        flavor={"original_name": "m1"},
    )
    assert server_addresses(server) == "net=10.0.0.5,90.1.1.1*"
    assert server_flavor(server) == "m1"


def test_status_style():
    assert status_style("ERROR") == "bold red"
    assert status_style("ACTIVE") == "green"
    assert status_style("building") == "yellow"
    assert status_style("unknown") == ""


def test_fuzzy_match():
    assert fuzzy_match("sgr", "Security group rules")
    assert fuzzy_match("", "anything")
    assert not fuzzy_match("xyz", "servers")


def test_error_text_strips_html():
    from ostack9s.ui.app import error_text

    exc = RuntimeError("Not authorized.<br /><br />\n done")
    assert error_text(exc) == "Not authorized. done"


def test_italian_catalog_is_complete():
    from i18n_keys import all_keys

    from ostack9s.locales import it

    missing = sorted(k for k in all_keys() if k not in it.CATALOG)
    assert not missing, "missing Italian translations:\n" + "\n".join(map(repr, missing))


def test_italian_placeholders_match():
    import string

    from ostack9s.locales import it

    def fields(text):
        return {f for _, f, _, _ in string.Formatter().parse(text) if f}

    wrong = [k for k, v in it.CATALOG.items() if fields(k) != fields(v)]
    assert not wrong, wrong


def test_language_switch():
    from ostack9s.i18n import set_language, t

    try:
        set_language("it")
        assert t("Delete") == "Elimina"
        assert t("Server {name} is being deleted", name="vm") == "Server vm in eliminazione"
        assert t("not in catalog") == "not in catalog"
    finally:
        set_language("en")
    assert t("Delete") == "Delete"
