#!/usr/bin/env python3
"""
Build hook for novel-downloader.

Compiles the gettext ``.po`` catalogs into ``.mo`` files before packaging so
installed distributions ship real translations. Compilation uses Babel (pure
Python) instead of GNU ``msgfmt``, keeping builds cross-platform.
"""

from __future__ import annotations

from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py

ROOT = Path(__file__).resolve().parent
LOCALES_DIR = ROOT / "src" / "novel_downloader" / "locales"


def compile_catalogs() -> list[Path]:
    """
    Compile every ``LC_MESSAGES/*.po`` next to its source file.

    :return: The list of written ``.mo`` paths.
    """
    from babel.messages.mofile import write_mo
    from babel.messages.pofile import read_po

    written: list[Path] = []
    for po_file in sorted(LOCALES_DIR.rglob("LC_MESSAGES/*.po")):
        mo_file = po_file.with_suffix(".mo")
        with po_file.open("rb") as src:
            catalog = read_po(src)
        with mo_file.open("wb") as dst:
            write_mo(dst, catalog)
        written.append(mo_file)
    return written


class build_py(_build_py):  # noqa: N801 - setuptools class naming convention
    """``build_py`` that materializes translation catalogs first."""

    def run(self) -> None:
        for mo_file in compile_catalogs():
            self.announce(f"compiled translation catalog: {mo_file}", level=2)
        super().run()


setup(cmdclass={"build_py": build_py})
