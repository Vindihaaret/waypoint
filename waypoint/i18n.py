"""Oversettelser. Teksten i koden er norsk; _() gir et annet språk når det er valgt.

Språket følger systemspråket (norsk for nb/nn/no, ellers engelsk, tysk, spansk eller fransk), eller
velges i Innstillinger. Tabellene for de andre språkene har den engelske teksten som nøkkel,
som i Vifte: locales/en.py (norsk → engelsk) og locales/{de,es,fr}.py (engelsk → språket).
"""

from __future__ import annotations

import os

LANGUAGES = [("auto", None), ("nb", "Norsk"), ("en", "English"), ("de", "Deutsch"), ("es", "Español"),
             ("fr", "Français")]
CODES = {code for code, _label in LANGUAGES if code != "auto"}
_tables: dict[str, dict] = {}
_lang: str | None = None


def system_language() -> str:
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        v = os.environ.get(var)
        if v and v not in ("C", "POSIX") and not v.startswith("C."):
            code = v.split(":")[0][:2].lower()
            if code in ("nb", "nn", "no"):
                return "nb"
            return code if code in CODES else "en"
    return "en"


def current() -> str:
    global _lang
    if _lang is None:
        from . import settings
        choice = os.environ.get("WAYPOINT_LANG") or settings.get("language")
        _lang = choice if choice in CODES else system_language()
    return _lang


def reset():
    global _lang
    _lang = None


def _table(code: str) -> dict:
    if code not in _tables:
        try:
            mod = __import__(f"waypoint.locales.{code}", fromlist=["T"])
            _tables[code] = mod.T
        except ImportError:
            _tables[code] = {}
    return _tables[code]


def _(text: str) -> str:
    lang = current()
    if lang == "nb":
        return text
    en = _table("en").get(text, text)
    if lang == "en":
        return en
    return _table(lang).get(en, en)


def N_(text: str) -> str:
    """Merker tekst for oversettelse uten å oversette den (brukes i tabeller på modulnivå)."""
    return text


def language_name(code: str) -> str:
    if code == "auto":
        return _("Automatisk (systemspråk)")
    return dict(LANGUAGES)[code]
