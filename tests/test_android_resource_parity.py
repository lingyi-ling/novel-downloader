#!/usr/bin/env python3
"""Static parity checks for the Android string resources.

The Android front end keeps its copy in ``res/values/strings.xml`` (Chinese,
the project default) and ``res/values-en/strings.xml`` (English).  Gradle does
not compare the two, and a reference to a resource that was renamed or dropped
only surfaces at ``aapt2`` time -- or at runtime, as a blank label.  This
module is the cheap static gate:

* duplicate ``name`` attributes inside one file are rejected, because only one
  of the two values can ever win;
* every ``R.string.<name>`` used from ``src/main/java`` (Kotlin or Java) must
  be declared by the default ``values/strings.xml``.  Comments, string
  literals and generated trees are stripped first, so a name quoted in a log
  line does not create a phantom requirement;
* every ``values-*/strings.xml`` may translate only names that exist in the
  default file: a typo such as ``nav_serach`` is a translation no user will
  ever see, and no build fails for it;
* no value is blank, and a ``%`` in a formatted value must begin a real format
  specifier (``%%`` is the literal percent).  ``formatted="false"`` opts the
  value out, exactly as ``Resources.getString`` does;
* translated format specifiers match the default ones as a multiset, so
  English cannot format a number where Chinese formats a string (reordering
  positional arguments stays legal).

Hand-checked UI copy -- "the default file must not contain long English
sentences" -- is intentionally out of scope: it needs human judgement, and the
resource files already get that review.

The scan is pure static: no Gradle invocation, no network, no Android SDK.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ANDROID_MAIN = ROOT / "android" / "app" / "src" / "main"
RES_DIR = ANDROID_MAIN / "res"
SOURCE_ROOTS = (ANDROID_MAIN / "java", ANDROID_MAIN / "kotlin")
DEFAULT_STRINGS = RES_DIR / "values" / "strings.xml"
SOURCE_SUFFIXES = frozenset({".kt", ".java"})
GENERATED_PARTS = frozenset({"build", "generated"})

#: ``R.string`` names with no declaration in the default file on purpose.
#: Keep it empty unless a name is produced by generated code or assembled at
#: runtime from a resource id; a genuine typo must fail the test.
ALLOWED_MISSING_REFERENCES: frozenset[str] = frozenset()

#: Kotlin/Java resource reference.  ``(?<![\w.])`` excludes qualified forms
#: such as ``android.R.string.ok`` and property access on unrelated objects.
_REFERENCE = re.compile(r"(?<![\w.])R\.string\.([A-Za-z_][A-Za-z0-9_]*)")

#: One Android/Java format specifier:
#: ``%`` [argument index] [flags] [width] [.precision] conversion.
_FORMAT_SPECIFIER = re.compile(
    r"%(?:(\d+)\$)?([-#+ 0,(]*)(\d+)?(?:\.(\d+))?([bBhHsScCdoxXeEfgGaAn%])"
)


def _rel(path: Path) -> str:
    """Repository-relative POSIX path, for stable failure messages."""
    return path.relative_to(ROOT).as_posix()


def _string_elements(path: Path) -> list[ET.Element]:
    """Every ``<string>`` child of a file whose root is ``<resources>``."""
    root = ET.parse(path).getroot()
    assert root.tag == "resources", f"{_rel(path)}: root is <{root.tag}>"
    return [element for element in root if element.tag == "string"]


def _name(element: ET.Element) -> str:
    name = element.get("name")
    assert name, "a <string> element has no name attribute"
    return name


def _text(element: ET.Element) -> str:
    """Visible text, including character data wrapped in inline markup."""
    return "".join(element.itertext())


def _localized_files() -> list[Path]:
    """``values-*`` directories that carry their own ``strings.xml``."""
    return sorted(RES_DIR.glob("values-*/strings.xml"))


def _default_elements() -> list[ET.Element]:
    assert DEFAULT_STRINGS.is_file(), f"missing {_rel(DEFAULT_STRINGS)}"
    return _string_elements(DEFAULT_STRINGS)


# ---------------------------------------------------------------------------
# Kotlin / Java lexing
# ---------------------------------------------------------------------------


def _matching_brace(text: str, start: int) -> int:
    """Index of the ``}`` that closes a ``${`` template body opened before start."""
    depth = 1
    index = start
    size = len(text)
    while index < size:
        char = text[index]
        if text.startswith('"""', index):
            end = text.find('"""', index + 3)
            index = size if end < 0 else end + 3
        elif char == '"':
            index += 1
            while index < size and text[index] != '"':
                index += 2 if text[index] == "\\" else 1
            index += 1
        elif char == "'":
            index += 1
            while index < size and text[index] != "'":
                index += 2 if text[index] == "\\" else 1
            index += 1
        elif char == "{":
            depth += 1
            index += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
            index += 1
        else:
            index += 1
    return size


def _code_only(text: str) -> str:
    """Blank out comments and literals while preserving newline positions.

    Kotlin block comments nest, strings are double-quoted (raw strings are
    triple-quoted), and characters are single-quoted.  A ``${...}`` template
    body is code, so it is scanned recursively: a reference written inside a
    template still counts.  Each skipped region is replaced by one space plus
    the newlines it contained, which keeps reported line numbers useful and
    prevents tokens on either side from being glued together.
    """
    out: list[str] = []
    index = 0
    size = len(text)
    while index < size:
        char = text[index]
        pair = text[index : index + 2]
        if pair == "//":
            end = text.find("\n", index)
            end = size if end < 0 else end
            out.append(" " + "\n" * text.count("\n", index, end))
            index = end
        elif pair == "/*":
            start = index
            depth = 1
            index += 2
            while index < size and depth:
                if text.startswith("/*", index):
                    depth += 1
                    index += 2
                elif text.startswith("*/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            out.append(" " + "\n" * text.count("\n", start, index))
        elif text.startswith('"""', index):
            start = index
            index += 3
            while index < size and not text.startswith('"""', index):
                index += 1
            index = min(index + 3, size)
            out.append(" " + "\n" * text.count("\n", start, index))
        elif char == '"':
            newlines = 0
            index += 1
            while index < size:
                if text[index] == "\\":
                    index += 2
                elif text[index] == '"':
                    index += 1
                    break
                elif text.startswith("${", index):
                    end = _matching_brace(text, index + 2)
                    out.append(_code_only(text[index + 2 : end]))
                    index = end + 1
                else:
                    if text[index] == "\n":
                        newlines += 1
                    index += 1
            out.append(" " + "\n" * newlines)
        elif char == "'":
            start = index
            index += 1
            while index < size:
                if text[index] == "\\":
                    index += 2
                elif text[index] == "'":
                    index += 1
                    break
                else:
                    index += 1
            out.append(" " + "\n" * text.count("\n", start, index))
        else:
            out.append(char)
            index += 1
    return "".join(out)


def _source_files() -> list[Path]:
    """Kotlin/Java sources under the main source set, minus generated trees."""
    files: list[Path] = []
    for root in SOURCE_ROOTS:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.suffix not in SOURCE_SUFFIXES:
                continue
            if GENERATED_PARTS.intersection(path.parts):
                continue
            files.append(path)
    return sorted(files)


def _references() -> dict[str, list[str]]:
    """Referenced name -> ``path:line`` locations, in source order."""
    references: dict[str, list[str]] = {}
    files = _source_files()
    assert files, "no Kotlin/Java sources found under android/.../src/main"
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        code = _code_only(text)
        for match in _REFERENCE.finditer(code):
            line = code.count("\n", 0, match.start()) + 1
            references.setdefault(match.group(1), []).append(f"{_rel(path)}:{line}")
    return references


# ---------------------------------------------------------------------------
# Format strings
# ---------------------------------------------------------------------------


def _scan_format(value: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Split a value into (position, conversion) specs and malformed ``%`` uses."""
    specs: list[tuple[str, str]] = []
    problems: list[str] = []
    index = 0
    size = len(value)
    while index < size:
        if value[index] != "%":
            index += 1
            continue
        if value.startswith("%%", index):
            index += 2
            continue
        match = _FORMAT_SPECIFIER.match(value, index)
        if match is None:
            problems.append(f"lone % at offset {index} in {value!r}")
            index += 1
            continue
        position = match.group(1)
        if position is not None and int(position) < 1:
            problems.append(f"invalid argument index %{position}$ at {index}")
        specs.append((position or "", match.group(5)))
        index = match.end()
    return specs, problems


def _specs(element: ET.Element) -> list[tuple[str, str]]:
    """Format specs of one element; ``formatted="false"`` disables formatting."""
    if element.get("formatted") == "false":
        return []
    return _scan_format(_text(element))[0]


def _format_problems(element: ET.Element) -> list[str]:
    """Malformed ``%`` uses, or nothing when formatting is explicitly off."""
    if element.get("formatted") == "false":
        return []
    return _scan_format(_text(element))[1]


# ---------------------------------------------------------------------------
# Resource declarations
# ---------------------------------------------------------------------------


def test_default_string_names_are_unique() -> None:
    names = [_name(element) for element in _default_elements()]
    duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
    assert not duplicates, (
        f"{_rel(DEFAULT_STRINGS)} declares duplicate names: {duplicates}"
    )


@pytest.mark.parametrize("path", _localized_files(), ids=lambda path: path.parent.name)
def test_localized_string_names_are_unique(path: Path) -> None:
    names = [_name(element) for element in _string_elements(path)]
    duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
    assert not duplicates, f"{_rel(path)} declares duplicate names: {duplicates}"


@pytest.mark.parametrize("path", _localized_files(), ids=lambda path: path.parent.name)
def test_localized_string_names_exist_in_default(path: Path) -> None:
    """A translation key absent from the default file can never be shown."""
    declared = {_name(element) for element in _default_elements()}
    extra = sorted({_name(element) for element in _string_elements(path)} - declared)
    assert not extra, (
        f"{_rel(path)} translates names that {_rel(DEFAULT_STRINGS)} does not "
        f"declare (typos or stale keys): {extra}"
    )


def test_every_r_string_reference_has_a_default_string() -> None:
    declared = {_name(element) for element in _default_elements()}
    references = _references()
    assert len(references) >= 50, (
        "the reference scan found almost nothing; the lexer or the source "
        f"root is wrong (only {len(references)} unique names)"
    )

    missing = {
        name: locations
        for name, locations in references.items()
        if name not in declared and name not in ALLOWED_MISSING_REFERENCES
    }
    assert not missing, (
        "R.string references have no declaration in "
        f"{_rel(DEFAULT_STRINGS)}:\n"
        + "\n".join(
            f"  {name}: {locations[0]}" for name, locations in sorted(missing.items())
        )
    )


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


def test_no_blank_string_values() -> None:
    """An empty value renders as a blank label or an empty toast."""
    offenders: list[str] = []
    for path in [DEFAULT_STRINGS, *_localized_files()]:
        for element in _string_elements(path):
            if not _text(element).strip():
                offenders.append(f"{_rel(path)}: {_name(element)}")
    assert not offenders, "blank string values:\n" + "\n".join(offenders)


@pytest.mark.parametrize("path", [DEFAULT_STRINGS, *_localized_files()], ids=_rel)
def test_percent_signs_form_valid_format_strings(path: Path) -> None:
    """``%`` must be ``%%`` unless it starts a format specifier."""
    offenders = [
        f"{_rel(path)}: {_name(element)}: {problem}"
        for element in _string_elements(path)
        for problem in _format_problems(element)
    ]
    assert not offenders, "malformed format strings:\n" + "\n".join(offenders)


@pytest.mark.parametrize("path", _localized_files(), ids=lambda path: path.parent.name)
def test_translated_format_specifiers_match_default(path: Path) -> None:
    """Translation may reorder arguments but must not change their types."""
    default = {_name(element): element for element in _default_elements()}
    mismatches: list[str] = []
    for element in _string_elements(path):
        original = default.get(_name(element))
        if original is None:
            continue
        expected = Counter(_specs(original))
        actual = Counter(_specs(element))
        if expected != actual:
            mismatches.append(
                f"{_name(element)}: default {sorted(expected.elements())} "
                f"vs {_rel(path)} {sorted(actual.elements())}"
            )
    assert not mismatches, (
        f"{_rel(path)} changes the format placeholders of the default strings:\n"
        + "\n".join(mismatches)
    )
