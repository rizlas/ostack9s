"""Minimal translation layer.

Source strings are English. ``t()`` looks them up in the catalog of the active
language at call time, so the language can be switched while the app runs.
Placeholders use ``str.format`` syntax: ``t("Deleted {name}", name=x)``.
"""

from __future__ import annotations

import os

from .locales import it

CATALOGS: dict[str, dict[str, str]] = {"en": {}, "it": it.CATALOG}
LANGUAGES = tuple(CATALOGS)

_current = "en"


def detect_language() -> str:
    """Language from ``OSTACK9S_LANG``, falling back to English."""
    lang = os.environ.get("OSTACK9S_LANG", "").lower()[:2]
    return lang if lang in CATALOGS else "en"


def set_language(lang: str) -> None:
    global _current
    if lang not in CATALOGS:
        raise ValueError(f"unsupported language: {lang}")
    _current = lang


def get_language() -> str:
    return _current


def t(text: str, **kwargs: object) -> str:
    translated = CATALOGS[_current].get(text, text)
    return translated.format(**kwargs) if kwargs else translated
