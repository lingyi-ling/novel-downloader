#!/usr/bin/env python3
"""
tests.test_i18n_placeholders
----------------------------

Static guards that keep translations structurally compatible with the source
strings they translate.

``infra.i18n.t()`` returns a plain translated string; callers then apply
``str.format(...)`` to it (or, for a few argparse strings, ``%``-style
interpolation).  A translation that drops, duplicates or invents a placeholder
therefore does not fail at import time -- it raises ``KeyError``/``IndexError``
(or silently renders the wrong value) only when that message is shown.  These
checks move that failure to CI:

* the brace placeholders (``{name}``, ``{0}``, ...) of a ``msgstr`` match the
  ``msgid`` as a multiset, so reordering is allowed but losing or adding one is
  not;
* ``%``-style placeholders (``%s``, ``%d``, ``%(name)s``, ``%1$s``) match the
  same way;
* ``""`` is the only entry allowed to have an empty ``msgstr``;
* only allow-listed entries may stay ``fuzzy`` (a fuzzy entry silently falls
  back to English at runtime);
* plural forms, when a catalog grows them, match ``msgid_plural``;
* every active ``.po`` entry still exists in the generated ``.pot`` template
  (stale entries are allowed only when marked obsolete with ``#~``; Babel
  exposes those through ``Catalog.obsolete`` instead of normal iteration).

The checks are host-independent: they read the ``.po``/``.pot`` sources with
Babel, never import the package and never resolve a runtime language.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from string import Formatter
from typing import Any

import pytest

_pofile = pytest.importorskip("babel.messages.pofile")
read_po = _pofile.read_po

ROOT = Path(__file__).resolve().parents[1]
PO_FILE = (
    ROOT
    / "src"
    / "novel_downloader"
    / "locales"
    / "zh_CN"
    / "LC_MESSAGES"
    / "messages.po"
)
POT_FILE = ROOT / "src" / "novel_downloader" / "locales" / "messages.pot"

_PO_REL = PO_FILE.relative_to(ROOT).as_posix()
_POT_REL = POT_FILE.relative_to(ROOT).as_posix()

#: Entries whose ``fuzzy`` flag is tolerated.  Keep this empty unless a
#: half-finished translation is deliberately parked; a fuzzy entry is ignored
#: by gettext and shows English at runtime.
FUZZY_ALLOWLIST: frozenset[object] = frozenset()

#: ``%``-style placeholder.  Covers ``%s``/``%d``, mapping keys
#: (``%(prog)s``) and numbered positional forms (``%1$s``).  ``%%`` is an
#: escape, not a placeholder, so ``%`` is deliberately not in the conversion
#: class; the space flag is omitted so ordinary prose such as "50% free" is
#: not mistaken for a format directive.
_PERCENT_RE = re.compile(
    r"%(?:(?P<position>\d+)\$)?(?:\((?P<name>[^)]+)\))?"
    r"[-#0+]?\d*(?:\.\d+)?[diouxXeEfFgGcrsa]"
)


def _bulleted(items: list[str]) -> str:
    """Render findings as an indented list for a readable assertion message."""
    return "\n".join(f"  - {item}" for item in items)


def _brace_fields(text: str) -> list[str]:
    """Field names of every ``str.format`` replacement field in ``text``.

    Nested fields in a format spec (``{value:{width}}``) are collected too,
    because ``str.format`` resolves them as well.
    """
    fields: list[str] = []
    for _literal, field_name, format_spec, _conversion in Formatter().parse(text):
        if field_name is None:
            continue
        fields.append(field_name)
        if "{" in format_spec:
            fields.extend(_brace_fields(format_spec))
    return fields


def _brace_error(text: str) -> str | None:
    """Return the ``str.format`` brace error in ``text``, if any."""
    try:
        list(Formatter().parse(text))
    except ValueError as exc:
        return str(exc)
    return None


def _placeholders(text: str) -> Counter[str]:
    """Multiset of placeholders in ``text``, canonicalised for comparison.

    Brace fields collapse to their field name (so ``{path!r}`` and ``{path}``
    use the same placeholder), keeping conversion/format specs out of the
    identity check.
    """
    tokens: Counter[str] = Counter()
    # A malformed string is reported by ``_brace_error``; do not let it raise
    # (and abort the whole scan) during placeholder extraction.
    if _brace_error(text) is None:
        tokens.update(f"{{{field}}}" for field in _brace_fields(text))
    for match in _PERCENT_RE.finditer(text):
        tokens[match.group(0)] += 1
    return tokens


def _placeholder_violations(msgid: str, msgstr: str) -> list[str]:
    """Report placeholders lost from, or added to, ``msgstr``."""
    expected = _placeholders(msgid)
    actual = _placeholders(msgstr)
    problems: list[str] = []

    for token, count in sorted((expected - actual).items()):
        problems.append(
            f"missing {token!r} x{count} (present in msgid, absent from msgstr)"
        )
    for token, count in sorted((actual - expected).items()):
        problems.append(
            f"unexpected {token!r} x{count} (present in msgstr, absent from msgid)"
        )
    return problems


def _format_violations(msgid: str, msgstr: str) -> list[str]:
    """Every brace/placeholder problem for one ``msgid``/``msgstr`` pair."""
    problems: list[str] = []

    msgid_error = _brace_error(msgid)
    if msgid_error:
        problems.append(f"msgid has malformed braces: {msgid_error}")
    msgstr_error = _brace_error(msgstr)
    if msgstr_error:
        problems.append(f"msgstr has malformed braces: {msgstr_error}")

    problems.extend(_placeholder_violations(msgid, msgstr))
    return problems


def _msgstr_forms(message: object) -> list[str]:
    """Return a message's translations as a list (plural entries use a list)."""
    string = getattr(message, "string", "")
    if isinstance(string, (list, tuple)):
        return [form or "" for form in string]
    return [string or ""]


def _is_plural(message: object) -> bool:
    return bool(getattr(message, "pluralizable", False))


def _short(text: str, limit: int = 160) -> str:
    """Collapse whitespace and trim long message text for assertion output."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else f"{flat[: limit - 3]}..."


@pytest.fixture(scope="module")
def po_catalog() -> Any:
    with PO_FILE.open("rb") as stream:
        return read_po(stream)


@pytest.fixture(scope="module")
def pot_catalog() -> Any:
    with POT_FILE.open("rb") as stream:
        return read_po(stream)


# ---------------------------------------------------------------------------
# Pure helper checks (prove the guards actually detect regressions)
# ---------------------------------------------------------------------------


def test_placeholder_check_detects_lost_placeholder() -> None:
    problems = _format_violations("Hello {name}", "你好")
    assert any("missing '{name}'" in problem for problem in problems), problems


def test_placeholder_check_detects_extra_placeholder() -> None:
    problems = _format_violations("你好", "你好 {name}")
    assert any("unexpected '{name}'" in problem for problem in problems), problems


def test_placeholder_check_allows_reordering() -> None:
    assert not _format_violations("{0} owns {1}", "{1} 属于 {0}")
    assert not _format_violations("{a} and {b}", "{b} 和 {a}")


def test_placeholder_check_supports_percent_forms() -> None:
    assert not _format_violations("%s and %d", "%d 和 %s")
    assert not _format_violations("%(prog)s: %s", "%(prog)s：%s")
    assert not _format_violations("%1$s / %2$s", "%2$s / %1$s")
    problems = _format_violations("%s", "%d")
    assert any("missing '%s'" in problem for problem in problems), problems


def test_placeholder_check_detects_unbalanced_braces() -> None:
    problems = _format_violations("Hello {name}", "你好 {name")
    assert any("malformed braces" in problem for problem in problems), problems


def test_placeholder_check_allows_escaped_literal_braces() -> None:
    assert not _format_violations("Use {{braces}}", "使用 {{braces}}")


# ---------------------------------------------------------------------------
# Catalog-wide guards
# ---------------------------------------------------------------------------


def test_every_translation_preserves_placeholders(po_catalog: Any) -> None:
    problems: list[str] = []
    for message in po_catalog:
        if not message.id or _is_plural(message):
            continue
        msgid = str(message.id)
        for form in _msgstr_forms(message):
            for problem in _placeholder_violations(msgid, form):
                problems.append(f"{msgid!r} -> {_short(form)!r}: {problem}")

    assert not problems, (
        f"{len(problems)} placeholder mismatch(es) in {_PO_REL}; a translation "
        "must keep every placeholder of its msgid (reordering is fine):\n"
        f"{_bulleted(problems)}"
    )


def test_plural_translations_match_msgid_plural(po_catalog: Any) -> None:
    problems: list[str] = []
    for message in po_catalog:
        if not message.id or not _is_plural(message):
            continue
        msgid_plural = str(message.id[1])
        for index, form in enumerate(_msgstr_forms(message)):
            if not form:
                continue
            for problem in _placeholder_violations(msgid_plural, form):
                problems.append(
                    f"{msgid_plural!r} msgstr[{index}] {_short(form)!r}: {problem}"
                )

    assert not problems, (
        f"{len(problems)} plural placeholder mismatch(es) in {_PO_REL}; every "
        "msgstr form must keep the placeholders of msgid_plural:\n"
        f"{_bulleted(problems)}"
    )


def test_every_msgstr_has_balanced_braces(po_catalog: Any) -> None:
    problems: list[str] = []
    for message in po_catalog:
        if not message.id:
            continue
        msgid = str(message.id)
        msgid_error = _brace_error(msgid)
        if msgid_error:
            problems.append(f"msgid {msgid!r}: {msgid_error}")
        for form in _msgstr_forms(message):
            if not form:
                continue
            error = _brace_error(form)
            if error:
                problems.append(f"msgstr {_short(form)!r}: {error}")

    assert not problems, (
        f"{len(problems)} unbalanced brace(s) in {_PO_REL}; ``str.format`` "
        "raises ValueError on these at runtime (write ``{{``/``}}`` for a "
        f"literal brace):\n{_bulleted(problems)}"
    )


def test_no_empty_msgstr(po_catalog: Any) -> None:
    problems: list[str] = []
    for message in po_catalog:
        if not message.id:
            continue  # the header is the only entry allowed to be empty
        forms = _msgstr_forms(message)
        if not forms or any(not form for form in forms):
            problems.append(f"{message.id!r} has an empty msgstr")

    assert not problems, (
        f"{len(problems)} untranslated entr(ies) in {_PO_REL}; gettext falls "
        f"back to English for an empty msgstr:\n{_bulleted(problems)}"
    )


def test_catalog_has_no_fuzzy_entries(po_catalog: Any) -> None:
    fuzzy = sorted(
        repr(message.id)
        for message in po_catalog
        if message.id and message.fuzzy and message.id not in FUZZY_ALLOWLIST
    )
    assert not fuzzy, (
        f"{len(fuzzy)} fuzzy entr(ies) in {_PO_REL}; gettext ignores a fuzzy "
        f"msgstr and shows English instead:\n{_bulleted(fuzzy)}"
    )


def test_po_has_no_active_entries_missing_from_pot(
    po_catalog: Any, pot_catalog: Any
) -> None:
    pot_ids = {message.id for message in pot_catalog if message.id}
    assert pot_ids, f"{_POT_REL} contains no message ids; was it generated?"

    stale = sorted(
        repr(message.id)
        for message in po_catalog
        if message.id and message.id not in pot_ids
    )
    assert not stale, (
        f"{len(stale)} active entr(ies) in {_PO_REL} do not exist in "
        f"{_POT_REL}. Remove them, or mark them obsolete with ``#~`` so the "
        f"catalog no longer ships them:\n{_bulleted(stale)}"
    )
