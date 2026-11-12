#!/usr/bin/env python3
"""
Rebuild ``src/novel_downloader/locales/messages.pot`` from the source tree.

Uses Babel instead of GNU ``xgettext`` so the same command works on every
platform. The creation date of an existing template is preserved so repeated
runs are reproducible (safe for CI freshness checks).

Hardening added on top of the extraction:

* ``msgctxt`` and plural forms are carried through instead of being dropped;
* a msgid extracted both as singular and plural (or with two different plural
  forms under the same context) is reported as a hard error, because Babel
  would otherwise keep only one of them;
* the template is always written as UTF-8 without a BOM, atomically;
* ``--check`` renders the template in memory and reports whether the committed
  file is stale without touching it.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from babel.messages import Catalog
from babel.messages.extract import extract_from_dir
from babel.messages.pofile import read_po, write_po

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = ROOT / "src" / "novel_downloader"
SCRIPTS_DIR = ROOT / "scripts"
OUT_FILE = PACKAGE_DIR / "locales" / "messages.pot"

_KEYWORDS = {"t": None}
_OPTIONS = {"encoding": "utf-8"}
_OPTIONS_MAP = {"**.py": _OPTIONS, "i18n_strings.py": _OPTIONS}
_SOURCES = (
    (PACKAGE_DIR, [("**.py", "python")], "src/novel_downloader"),
    (SCRIPTS_DIR, [("i18n_strings.py", "python")], "scripts"),
)
_FALLBACK_DATE = datetime(2025, 1, 1, tzinfo=UTC)

_UTF8_BOM = b"\xef\xbb\xbf"

#: Matches the header line a template must carry to declare its creation date.
_DATE_DECLARED_RE = re.compile(r'^"POT-Creation-Date:', re.MULTILINE)

MessageId = str | tuple[str, str]


class ExtractedMessage(NamedTuple):
    """One extracted literal with everything needed to add it to a catalog."""

    message: MessageId
    location: tuple[str, int]
    flags: tuple[str, ...]
    context: str | None


def _previous_creation_date() -> datetime:
    """Reuse the creation date the template itself declares, if any.

    Babel substitutes ``datetime.now()`` when the header block carries no
    ``POT-Creation-Date`` at all (e.g. hand-made templates), raises on a
    malformed one, and may store other non-datetime values as-is. Only a date
    the file actually declares, parsed into a real ``datetime``, is reused;
    everything else falls back so repeated runs stay byte-identical.
    """
    if not OUT_FILE.is_file():
        return _FALLBACK_DATE
    try:
        declared = _DATE_DECLARED_RE.search(
            OUT_FILE.read_text("utf-8-sig", errors="replace")
        )
        raw_bytes = OUT_FILE.read_bytes()
        if raw_bytes.startswith(_UTF8_BOM):
            raw_bytes = raw_bytes[len(_UTF8_BOM) :]
        with io.BytesIO(raw_bytes) as handle:
            raw = read_po(handle).creation_date
    except Exception:
        return _FALLBACK_DATE
    if declared and isinstance(raw, datetime):
        return raw
    return _FALLBACK_DATE


def _location(prefix: str, filename: str, lineno: int) -> tuple[str, int]:
    """Build a repo-relative, forward-slashed ``(file, line)`` reference."""
    return (f"{prefix}/{filename.lstrip('/')}", lineno)


def _flags(message: MessageId) -> list[str]:
    """Flag brace-formatted strings, matching ``xgettext`` behaviour."""
    texts = message if isinstance(message, tuple) else (message,)
    if any("{" in text and "}" in text for text in texts):
        return ["python-brace-format"]
    return []


def _normalize_message(message: object) -> MessageId | None:
    """Normalize the extractor's message value (str or ``(singular, plural)``)."""
    if isinstance(message, str):
        return message or None
    if isinstance(message, (list, tuple)):
        if not message:
            return None
        if len(message) == 1:
            return str(message[0]) or None
        return (str(message[0]), str(message[1]))
    return str(message) or None


def _collect_entries() -> list[ExtractedMessage]:
    """Extract full ``ExtractedMessage`` records, ordered by location."""
    entries: list[ExtractedMessage] = []

    for dirname, method_map, prefix in _SOURCES:
        extracted = extract_from_dir(
            dirname,
            method_map=method_map,
            options_map=_OPTIONS_MAP,
            keywords=_KEYWORDS,
        )
        for filename, lineno, message, _comments, context in extracted:
            normalized = _normalize_message(message)
            if normalized is None:
                continue
            entries.append(
                ExtractedMessage(
                    message=normalized,
                    location=_location(prefix, filename, lineno),
                    flags=tuple(_flags(normalized)),
                    context=context,
                )
            )

    entries.sort(key=lambda entry: entry.location)
    return entries


def _collect() -> list[tuple[MessageId, tuple[str, int], list[str]]]:
    """Backwards-compatible ``(msgid, location, flags)`` view of extraction."""
    return [
        (entry.message, entry.location, list(entry.flags))
        for entry in _collect_entries()
    ]


def _message_key(message: MessageId, context: str | None) -> tuple[object, ...]:
    singular = message[0] if isinstance(message, tuple) else message
    return (singular, context)


def _conflicting_messages(entries: Sequence[ExtractedMessage]) -> list[str]:
    """Report duplicate msgids whose singular/plural shape disagrees."""
    seen: dict[tuple[object, ...], MessageId] = {}
    problems: list[str] = []
    for entry in entries:
        key = _message_key(entry.message, entry.context)
        previous = seen.get(key)
        if previous is None:
            seen[key] = entry.message
            continue
        same_shape = isinstance(previous, tuple) == isinstance(entry.message, tuple)
        if same_shape and isinstance(previous, tuple):
            same_shape = previous[1] == entry.message[1]
        if not same_shape:
            location = f"{entry.location[0]}:{entry.location[1]}"
            problems.append(
                f"{entry.message!r} ({location}) conflicts with the earlier "
                f"definition {previous!r} for the same msgid/context"
            )
    return problems


def _build_catalog(entries: Sequence[ExtractedMessage]) -> Catalog:
    catalog = Catalog(
        project="novel-downloader",
        msgid_bugs_address=("https://github.com/lingyi-ling/novel-downloader/issues"),
        copyright_holder="Novel Downloader contributors",
        charset="UTF-8",
    )
    creation_date = _previous_creation_date()
    catalog.creation_date = creation_date
    catalog.revision_date = creation_date

    for entry in entries:
        catalog.add(
            entry.message,
            locations=[entry.location],
            flags=entry.flags,
            context=entry.context,
        )
    return catalog


def _detect_newline(data: bytes | None) -> bytes:
    """Keep the checkout's line endings (CRLF on Windows) to avoid churn."""
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


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="report whether the template is stale without writing it",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args([] if argv is None else argv)

    entries = _collect_entries()
    problems = _conflicting_messages(entries)
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 1

    try:
        current = OUT_FILE.read_bytes() if OUT_FILE.is_file() else None
    except OSError as exc:
        print(f"error: cannot read {OUT_FILE}: {exc}", file=sys.stderr)
        return 1

    catalog = _build_catalog(entries)
    rendered = _render(catalog, _detect_newline(current))

    if args.check:
        if rendered != current:
            print(
                f"{OUT_FILE} is stale; run scripts/update_pot.py",
                file=sys.stderr,
            )
            return 1
        print(f"Template is up to date: {OUT_FILE}")
        return 0

    if rendered == current:
        print(f"Template unchanged: {OUT_FILE}")
        return 0

    _write_atomically(OUT_FILE, rendered)
    print(f"Updated template: {OUT_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
