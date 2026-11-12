#!/usr/bin/env python3
"""
Create/refresh the per-language ``.po`` files from ``messages.pot``.

Uses Babel instead of GNU ``msgmerge``/``msginit`` so it works on every
platform. The update is deliberately conservative:

* an existing non-empty ``msgstr`` is never rewritten (manual translations are
  the source of truth; the merge only ever fills in brand-new entries);
* Babel's fuzzy matching is disabled, so a renamed msgid cannot silently inherit
  an unrelated translation and leave a ``fuzzy`` entry behind;
* every translation is checked for matching ``{name}`` / ``%(name)s`` / ``%s``
  placeholders, HTML tags, newlines, tabs and surrounding whitespace. A broken
  translation is kept for review but marked ``fuzzy`` so it is never compiled;
* duplicate message definitions and non-UTF-8 catalogs are hard errors;
* ``--check`` reports what would change without writing anything, which keeps
  CI's "catalogs are fresh" gate honest;
* output is always UTF-8 without a BOM and is written atomically, so two
  consecutive runs produce byte-identical files.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
import tempfile
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from string import Formatter

from babel.messages import Catalog
from babel.messages.catalog import Message
from babel.messages.pofile import PoFileError, PoFileParser, write_po

ROOT = Path(__file__).resolve().parent.parent
LOCALES_DIR = ROOT / "src" / "novel_downloader" / "locales"
POT_FILE = LOCALES_DIR / "messages.pot"

LANGS = ("zh_CN",)

_UTF8_BOM = b"\xef\xbb\xbf"
_DECLARED_CHARSET_RE = re.compile(
    r'^"Content-Type:\s*text/plain;\s*charset=([^"\\]+)',
    re.MULTILINE,
)

#: HTML tags that may legitimately appear in a translatable string. Only these
#: are compared; angle-bracketed placeholders such as ``<output directory>``
#: are prose, not markup, and get translated.
_HTML_TAGS = frozenset(
    {
        "a",
        "b",
        "br",
        "code",
        "div",
        "em",
        "h1",
        "h2",
        "h3",
        "h4",
        "i",
        "kbd",
        "li",
        "mark",
        "ol",
        "p",
        "pre",
        "q",
        "s",
        "small",
        "span",
        "strong",
        "sub",
        "sup",
        "table",
        "td",
        "th",
        "tr",
        "tt",
        "u",
        "ul",
        "var",
    }
)
_HTML_TAG_RE = re.compile(r"</?([A-Za-z][A-Za-z0-9]*)\b[^<>]*?/?>")

#: Python/printf placeholders. A space flag or a preceding digit is not
#: accepted so prose such as ``100% complete`` or ``20%off`` is not mistaken
#: for a conversion.
_PRINTF_RE = re.compile(
    r"(?<!\d)%(?:\(([^)]*)\))?([#0\-+]*\d*(?:\.\d+)?[diouxXeEfFgGcrsa%])"
)

_LEADING_WS_RE = re.compile(r"^[ \t]*")
_TRAILING_WS_RE = re.compile(r"[ \t]*$")

_Key = tuple[str | tuple[str, ...], str | None]


@dataclass(frozen=True)
class Finding:
    """One diagnostic for a catalog entry."""

    kind: str
    message: str
    detail: str
    hard: bool = False


@dataclass
class LocaleReport:
    """Result of processing one language."""

    language: str
    path: Path
    findings: list[Finding] = field(default_factory=list)
    changed: bool = False
    written: bool = False
    rendered: bytes = b""


class CatalogFormatError(Exception):
    """A catalog cannot be read or would be unsafe to rewrite."""


class _DuplicateTrackingParser(PoFileParser):
    """Parser that records repeated ``(msgid, msgctxt)`` definitions.

    Babel silently merges duplicates, which hides a ``msgfmt``-style error and
    can drop one of the two translations. The catalog is still parsed so the
    rest of the checks can run, but the caller refuses to write it.
    """

    def __init__(self, catalog: Catalog) -> None:
        super().__init__(catalog, abort_invalid=True)
        self.duplicates: list[tuple[str, str | None, int]] = []
        self._seen: set[_Key] = set()

    def _add_message(self) -> None:
        if self.messages and not self.obsolete:
            msgid = _normalize_id(
                tuple(item.denormalize() for item in self.messages)
                if len(self.messages) > 1
                else self.messages[0].denormalize()
            )
            context = self.context.denormalize() if self.context else None
            if msgid:
                key: _Key = (msgid, context)
                if key in self._seen:
                    display = msgid[0] if isinstance(msgid, tuple) else msgid
                    self.duplicates.append((display, context, self.offset + 1))
                else:
                    self._seen.add(key)
        super()._add_message()


def _normalize_id(value: object) -> str | tuple[str, ...]:
    """Normalize a Babel message id to a hashable ``str`` or tuple."""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    return str(value)


def _normalize_string_value(value: object) -> str | tuple[str, ...]:
    """Normalize ``Message.string`` (``None``/list values included)."""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    return ""


def _string_forms(value: str | tuple[str, ...]) -> tuple[str, ...]:
    return value if isinstance(value, tuple) else (value,)


def _has_text(value: str | tuple[str, ...]) -> bool:
    return any(form.strip() for form in _string_forms(value))


def _message_key(message: Message) -> _Key:
    return (_normalize_id(message.id), message.context)


def _message_identifier(message: Message) -> str:
    ident = _normalize_id(message.id)
    text = ident[0] if isinstance(ident, tuple) else ident
    return f"{message.context}: {text}" if message.context else text


def _brace_fields(text: str) -> list[str]:
    """Return the field names used by ``str.format``, including nested specs.

    Raises ``ValueError`` when the braces are malformed.
    """
    fields: list[str] = []

    def walk(value: str) -> None:
        for _literal, field_name, spec, _conversion in Formatter().parse(value):
            if field_name is None:
                continue
            name = field_name.split(".", 1)[0].split("[", 1)[0]
            fields.append(name or "*")
            if spec:
                walk(spec)

    walk(text)
    return fields


def _printf_fields(text: str) -> list[tuple[str, str]]:
    """Return ``(mapping_key, conversion)`` pairs for ``%``-style placeholders."""
    fields: list[tuple[str, str]] = []
    for match in _PRINTF_RE.finditer(text):
        conversion = match.group(2)
        if conversion == "%":
            continue
        fields.append((match.group(1) or "", conversion[-1]))
    return fields


def _placeholders(text: str) -> Counter[tuple[str, str]]:
    """Multiset of format placeholders (order does not matter, count does)."""
    found: Counter[tuple[str, str]] = Counter()
    for name in _brace_fields(text):
        found[("brace", name)] += 1
    for mapping_key, conversion in _printf_fields(text):
        found[("printf", f"{mapping_key}:{conversion}")] += 1
    return found


def _render_placeholders(counter: Counter[tuple[str, str]]) -> str:
    if not counter:
        return "none"
    return ", ".join(
        f"{kind}:{name}" + (f" x{count}" if count > 1 else "")
        for (kind, name), count in sorted(counter.items())
    )


def _html_tags(text: str) -> Counter[str]:
    tags: Counter[str] = Counter()
    for match in _HTML_TAG_RE.finditer(text):
        name = match.group(1).lower()
        if name in _HTML_TAGS:
            tags[name] += 1
    return tags


def _edges(text: str) -> tuple[str, str]:
    leading = _LEADING_WS_RE.search(text)
    trailing = _TRAILING_WS_RE.search(text)
    return (
        leading.group(0) if leading else "",
        trailing.group(0) if trailing else "",
    )


def _compare_text(source: str, translation: str, label: str) -> list[Finding]:
    """Compare one source/translation pair for format and markup drift."""
    try:
        expected = _placeholders(source)
    except ValueError as exc:
        return [
            Finding(
                "format",
                label,
                f"source has malformed braces: {exc}",
                hard=True,
            )
        ]

    findings: list[Finding] = []
    try:
        actual = _placeholders(translation)
    except ValueError as exc:
        return findings + [
            Finding("format", label, f"translation has malformed braces: {exc}")
        ]

    if expected != actual:
        findings.append(
            Finding(
                "placeholder",
                label,
                "placeholders differ: source has "
                f"{_render_placeholders(expected)}, translation has "
                f"{_render_placeholders(actual)}",
            )
        )

    if source.count("\n") != translation.count("\n"):
        findings.append(
            Finding(
                "newline",
                label,
                f"newline count differs: source {source.count(chr(10))}, "
                f"translation {translation.count(chr(10))}",
            )
        )

    if source.count("\t") != translation.count("\t"):
        findings.append(
            Finding(
                "whitespace",
                label,
                f"tab count differs: source {source.count(chr(9))}, "
                f"translation {translation.count(chr(9))}",
            )
        )

    if _html_tags(source) != _html_tags(translation):
        findings.append(
            Finding(
                "html",
                label,
                "HTML tags differ: source "
                f"{dict(_html_tags(source))}, translation "
                f"{dict(_html_tags(translation))}",
            )
        )

    if _edges(source) != _edges(translation):
        findings.append(
            Finding(
                "whitespace",
                label,
                "leading/trailing whitespace differs: source "
                f"{_edges(source)!r}, translation {_edges(translation)!r}",
            )
        )

    if source.count("%%") != translation.count("%%"):
        findings.append(
            Finding(
                "placeholder",
                label,
                "literal '%%' count differs: source "
                f"{source.count('%%')}, translation "
                f"{translation.count('%%')}",
            )
        )

    return findings


def _matching_source(candidates: Sequence[str], translation: str) -> str:
    """Pick the source form whose placeholders match the translation.

    A one-plural-form language (``nplurals=1``, e.g. ``zh_CN``) translates the
    singular and plural with a single ``msgstr[0]``, so that form may use the
    plural placeholder even at index 0. Comparing against the alternate form
    avoids a false positive while still flagging placeholders that appear in
    neither source form.
    """
    try:
        actual = _placeholders(translation)
    except ValueError:
        return candidates[0]
    for candidate in candidates:
        try:
            if _placeholders(candidate) == actual:
                return candidate
        except ValueError:
            continue
    return candidates[0]


def _validate_message(message: Message) -> tuple[list[Finding], bool]:
    """Validate one message, returning findings and whether to mark it fuzzy."""
    if not message.id:
        return [], False

    identifier = _message_identifier(message)
    translated = _normalize_string_value(message.string)
    if not _has_text(translated):
        return [Finding("untranslated", identifier, "msgstr is empty")], False

    if message.fuzzy:
        return [
            Finding(
                "fuzzy",
                identifier,
                "msgstr is marked fuzzy and is skipped when compiling",
            )
        ], False

    ids = _normalize_id(message.id)
    id_forms = _string_forms(ids)
    translated_forms = _string_forms(translated)

    findings: list[Finding] = []
    for index, translation in enumerate(translated_forms):
        if not translation:
            findings.append(Finding("plural", identifier, f"msgstr[{index}] is empty"))
            continue
        primary = id_forms[min(index, len(id_forms) - 1)]
        candidates = [primary, *[form for form in id_forms if form != primary]]
        source = _matching_source(candidates, translation)
        label = "msgstr" if len(translated_forms) == 1 else f"msgstr[{index}]"
        findings.extend(_compare_text(source, translation, f"{identifier} [{label}]"))

    return findings, bool(findings)


def _read_catalog(path: Path, language: str | None) -> tuple[Catalog, list[Finding]]:
    """Read a UTF-8 ``.po``/``.pot`` file and collect structural findings."""
    raw = path.read_bytes()
    had_bom = raw.startswith(_UTF8_BOM)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CatalogFormatError(
            f"{path}: not valid UTF-8 ({exc.reason} at byte {exc.start})"
        ) from exc

    declared = _DECLARED_CHARSET_RE.search(text)
    if declared:
        charset = declared.group(1).strip().lower().replace("_", "-")
        if charset not in {"utf-8", "utf8"}:
            raise CatalogFormatError(
                f"{path}: declares charset {charset!r}; only UTF-8 catalogs "
                "are supported"
            )

    findings: list[Finding] = []
    if had_bom:
        findings.append(
            Finding(
                "encoding",
                str(path),
                "UTF-8 BOM found; the catalog is rewritten without it",
            )
        )

    catalog = Catalog(locale=language)
    parser = _DuplicateTrackingParser(catalog)
    parser.parse(io.StringIO(text))
    for msgid, context, lineno in parser.duplicates:
        location = f"{context}: {msgid}" if context else msgid
        findings.append(
            Finding(
                "duplicate",
                location,
                f"duplicate message definition near line {lineno}",
                hard=True,
            )
        )
    return catalog, findings


def _snapshot(catalog: Catalog) -> dict[_Key, str | tuple[str, ...]]:
    return {
        _message_key(message): _normalize_string_value(message.string)
        for message in catalog
        if message.id
    }


def _same_shape(old: str | tuple[str, ...], new: str | tuple[str, ...]) -> bool:
    if isinstance(old, str) or isinstance(new, str):
        return isinstance(old, str) and isinstance(new, str)
    return len(old) == len(new)


def _preserve_translations(
    before: dict[_Key, str | tuple[str, ...]], catalog: Catalog
) -> list[Finding]:
    """Guarantee that no pre-existing non-empty translation was rewritten."""
    after = {_message_key(message): message for message in catalog if message.id}
    findings: list[Finding] = []
    for key, old_value in before.items():
        if not _has_text(old_value):
            continue
        message = after.get(key)
        if message is None:
            # The msgid disappeared from the template; it is obsolete, not
            # overwritten. The translation is intentionally dropped with it.
            continue
        new_value = _normalize_string_value(message.string)
        if new_value == old_value:
            continue
        identifier = key[0] if isinstance(key[0], str) else key[0][0]
        findings.append(
            Finding(
                "overwritten",
                f"{key[1]}: {identifier}" if key[1] else identifier,
                "the merge attempted to change an existing translation; "
                "the original text was restored",
                hard=True,
            )
        )
        if _same_shape(old_value, new_value):
            message.string = old_value
    return findings


def _detect_newline(data: bytes | None) -> bytes:
    """Preserve the catalog's existing line endings (git checks out CRLF on
    Windows, LF elsewhere) so a no-op update stays byte-identical."""
    if data and b"\r\n" in data:
        return b"\r\n"
    return b"\n"


def _render(catalog: Catalog, newline: bytes = b"\n") -> bytes:
    catalog.charset = "utf-8"
    buffer = io.BytesIO()
    write_po(buffer, catalog, ignore_obsolete=True)
    data = buffer.getvalue()
    if newline != b"\n":
        data = data.replace(b"\r\n", b"\n").replace(b"\n", newline)
    return data


def _write_atomically(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def _process_locale(
    language: str,
    po_path: Path,
    template: Catalog,
    *,
    check: bool,
) -> LocaleReport:
    report = LocaleReport(language, po_path)
    revision_date = None

    if po_path.is_file():
        try:
            catalog, findings = _read_catalog(po_path, language)
        except (CatalogFormatError, PoFileError) as exc:
            report.findings.append(
                Finding("catalog", str(po_path), str(exc), hard=True)
            )
            return report
        report.findings.extend(findings)
        if any(finding.hard for finding in report.findings):
            # Never rewrite a catalog that is structurally broken: the merge
            # would silently discard one of the duplicate definitions.
            return report
        revision_date = catalog.revision_date
    else:
        report.findings.append(
            Finding("missing", str(po_path), "catalog does not exist yet")
        )
        catalog = Catalog(locale=language)

    before = _snapshot(catalog)
    catalog.update(template, update_creation_date=False, no_fuzzy_matching=True)
    catalog.creation_date = template.creation_date
    catalog.revision_date = revision_date or template.creation_date
    catalog.charset = "utf-8"

    report.findings.extend(_preserve_translations(before, catalog))

    fuzzy_keys: set[_Key] = set()
    for message in catalog:
        if not message.id:
            continue
        findings, mark_fuzzy = _validate_message(message)
        report.findings.extend(findings)
        if mark_fuzzy:
            fuzzy_keys.add(_message_key(message))
    for message in catalog:
        if message.id and _message_key(message) in fuzzy_keys:
            message.flags.add("fuzzy")

    current = po_path.read_bytes() if po_path.is_file() else None
    report.rendered = _render(catalog, _detect_newline(current))
    report.changed = report.rendered != current
    if not check and report.changed:
        _write_atomically(po_path, report.rendered)
        report.written = True
    return report


def _print_findings(source: str, findings: Sequence[Finding]) -> None:
    for finding in findings:
        severity = "error" if finding.hard else "warning"
        print(
            f"{source}: {severity}: [{finding.kind}] "
            f"{finding.message}: {finding.detail}",
            file=sys.stderr,
        )


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report what would change without writing any catalog",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args([] if argv is None else argv)

    if not POT_FILE.is_file():
        print("No messages.pot found. Run update_pot.py first.")
        return 1

    try:
        template, template_findings = _read_catalog(POT_FILE, None)
    except (CatalogFormatError, PoFileError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    _print_findings("messages.pot", template_findings)
    exit_code = 1 if any(finding.hard for finding in template_findings) else 0
    if exit_code:
        return exit_code

    for language in LANGS:
        po_file = LOCALES_DIR / language / "LC_MESSAGES" / "messages.po"
        report = _process_locale(language, po_file, template, check=args.check)
        _print_findings(language, report.findings)

        if any(finding.hard for finding in report.findings):
            exit_code = 1

        if args.check:
            if report.findings or report.changed:
                exit_code = 1
            if report.changed:
                print(
                    f"{language}: {po_file} would change; run scripts/update_po.py",
                    file=sys.stderr,
                )
            elif not report.findings:
                print(f"{language}: {po_file} is up to date")
        else:
            if report.written:
                print(f"Updated {po_file}")
            elif report.changed:
                print(f"{language}: {po_file} could not be written")
            elif report.findings:
                print(
                    f"{language}: {po_file} needs attention "
                    f"({len(report.findings)} finding(s))"
                )
            else:
                print(f"{language}: {po_file} is up to date")

    if args.check and exit_code == 0:
        print("All locales are up to date.")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
