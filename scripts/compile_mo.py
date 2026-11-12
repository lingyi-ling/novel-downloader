#!/usr/bin/env python3
"""
Compile ``.po`` catalogs into ``.mo`` files.

Uses Babel instead of GNU ``msgfmt`` so it works on every platform. The
resulting ``.mo`` files are build artifacts (git-ignored); they are regenerated
by ``setup.py`` during packaging.

Fuzzy entries are never compiled: Babel's ``write_mo`` is asked explicitly for
``use_fuzzy=False`` so an unreviewed translation can only fall back to the
source string. Catalogs are read as UTF-8 (a stray BOM is tolerated and
ignored) and the ``.mo`` is written atomically, so a failed compile cannot
leave a half-written file behind.
"""

from __future__ import annotations

import argparse
import io
import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from babel.messages import Catalog
from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po

ROOT = Path(__file__).resolve().parent.parent
LOCALES_DIR = ROOT / "src" / "novel_downloader" / "locales"

_UTF8_BOM = b"\xef\xbb\xbf"


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


def _read_catalog(po_file: Path) -> Catalog:
    raw = po_file.read_bytes()
    if raw.startswith(_UTF8_BOM):
        raw = raw[len(_UTF8_BOM) :]
    with io.BytesIO(raw) as handle:
        return read_po(handle)


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="compile in memory and report whether any .mo is stale",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args([] if argv is None else argv)

    po_files = sorted(LOCALES_DIR.rglob("LC_MESSAGES/*.po"))
    if not po_files:
        print("No .po files found.")
        return 0

    exit_code = 0
    for po_file in po_files:
        mo_file = po_file.with_suffix(".mo")
        try:
            catalog = _read_catalog(po_file)
        except Exception as exc:
            print(f"error: cannot read {po_file}: {exc}", file=sys.stderr)
            exit_code = 1
            continue

        fuzzy = sum(1 for message in catalog if message.id and message.fuzzy)
        buffer = io.BytesIO()
        write_mo(buffer, catalog, use_fuzzy=False)
        compiled = buffer.getvalue()

        if args.check:
            current = mo_file.read_bytes() if mo_file.is_file() else None
            if compiled != current:
                print(
                    f"{mo_file} is stale; run scripts/compile_mo.py",
                    file=sys.stderr,
                )
                exit_code = 1
            continue

        _write_atomically(mo_file, compiled)
        suffix = f" (skipped {fuzzy} fuzzy entr{'y' if fuzzy == 1 else 'ies'})"
        print(f"Compiled {po_file} -> {mo_file}{suffix}")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
