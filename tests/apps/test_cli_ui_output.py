#!/usr/bin/env python3
"""Output-layer hardening for :mod:`novel_downloader.apps.cli.ui`.

Pins the parts of the CLI presentation layer that only matter once the
process leaves a friendly UTF-8 terminal:

* colour policy -- ``NO_COLOR``, ``FORCE_COLOR`` and the non-TTY default --
  including the case where the stream stops being a terminal *after* the
  console was built (an in-process redirect, a wrapper, pytest capture):
  a redirected stream must never receive escape sequences;
* legacy Windows code pages (cp936/cp1252): user data with CJK or emoji must
  degrade to backslash escapes instead of raising ``UnicodeEncodeError`` out
  of a command that otherwise succeeded;
* ``--json`` stdout purity while diagnostics, tables and progress use stderr;
* table layout measured in display cells, so double-width CJK keeps the
  borders aligned at wide and narrow widths.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import sys
import unicodedata

import pytest

from novel_downloader.apps.cli import ui

#: Any ANSI CSI sequence; colour and text attributes both use it.
_CSI = re.compile(r"\x1b\[")


class _TTYStringIO(io.StringIO):
    """A text buffer that claims to be a terminal."""

    def isatty(self) -> bool:
        return True


class _LegacyTextStream(io.TextIOBase):
    """Strict legacy-code-page stream with no byte buffer.

    Mirrors a ``sys.stdout``/``sys.stderr`` replaced by an embedding host (or
    a console opened with ``errors="strict"``): writing a character outside
    the code page raises exactly like a cp1252/cp936 terminal, and there is
    no ``.buffer`` to fall back to.
    """

    def __init__(self, encoding: str) -> None:
        self._encoding = encoding
        self._parts: list[str] = []

    @property
    def encoding(self) -> str:
        return self._encoding

    def write(self, text: str) -> int:
        text.encode(self._encoding)  # raises for characters outside the page
        self._parts.append(text)
        return len(text)

    def flush(self) -> None:
        pass

    def getvalue(self) -> str:
        return "".join(self._parts)


def _display_width(text: str) -> int:
    """Terminal cell width, counting East Asian wide/fullwidth as two."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


# --------------------------------------------------------------------------- #
# colour policy
# --------------------------------------------------------------------------- #
def test_no_color_disables_ansi_on_a_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    stream = _TTYStringIO()
    console = ui._build_console(file=stream)
    monkeypatch.setattr(ui, "_CONSOLE", console)

    assert console.is_terminal is True

    ui.success("done")
    console.print("bold too", style="bold")

    output = stream.getvalue()
    assert "done" in output
    assert _CSI.search(output) is None, "NO_COLOR must suppress all ANSI"


def test_force_color_zero_keeps_redirected_output_plain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "0")
    stream = io.StringIO()
    console = ui._build_console(file=stream)
    monkeypatch.setattr(ui, "_CONSOLE", console)

    assert console.is_terminal is False, "FORCE_COLOR=0 must not fake a TTY"

    ui.success("done")

    assert "done" in stream.getvalue()
    assert _CSI.search(stream.getvalue()) is None


def test_force_color_one_forces_ansi_for_redirected_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    stream = io.StringIO()
    console = ui._build_console(file=stream)

    assert console.is_terminal is True
    console.print("x", style="red")
    assert "\x1b[31m" in stream.getvalue()


def test_non_tty_default_has_no_ansi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    stream = io.StringIO()
    console = ui._build_console(file=stream)

    assert console.is_terminal is False
    console.print("x", style="red")
    assert _CSI.search(stream.getvalue()) is None


def test_mid_run_redirect_stops_ansi_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A console built on a TTY must not keep colouring a redirected stream."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    tty = _TTYStringIO()
    monkeypatch.setattr(sys, "stderr", tty)
    console = ui._build_console()  # resolves sys.stderr on every write

    console.print("live", style="red")
    assert _CSI.search(tty.getvalue()), "a real terminal keeps its colour"

    redirected = io.StringIO()
    monkeypatch.setattr(sys, "stderr", redirected)
    console.print("piped", style="red")

    assert console.is_terminal is False
    assert "piped" in redirected.getvalue()
    assert _CSI.search(redirected.getvalue()) is None


# --------------------------------------------------------------------------- #
# legacy code pages
# --------------------------------------------------------------------------- #
def test_success_and_warn_survive_a_cp1252_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _LegacyTextStream("cp1252")
    monkeypatch.setattr(ui, "_CONSOLE", ui._build_console(file=stream))

    ui.success("已完成 🎵")
    ui.warn("careful 🎵")

    output = stream.getvalue()
    assert "🎵" not in output, "unencodable characters must not reach the stream"
    assert "\\U0001f3b5" in output, "they degrade to readable escapes instead"
    assert "careful" in output


def test_render_table_survives_unencodable_cells(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _LegacyTextStream("cp1252")
    monkeypatch.setattr(ui, "_CONSOLE", ui._build_console(file=stream))

    ui.render_table("作品", ["书名", "作者"], [["斗破苍穹 🎵", "天蚕土豆"]])

    output = stream.getvalue()
    assert "🎵" not in output
    assert "\\U0001f3b5" in output
    assert "\\u6597" in output, "CJK cells are escaped, not dropped"


def test_progress_text_mode_survives_unencodable_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _LegacyTextStream("cp936")
    monkeypatch.setattr(ui, "_CONSOLE", ui._build_console(file=stream))
    monkeypatch.setattr(sys, "stdout", io.StringIO())  # non-TTY -> text mode

    progress = ui.ProgressUI(prefix="下载 🎵", unit="章")
    progress.start()
    try:
        asyncio.run(progress.update(1, 2))
    finally:
        progress.stop()

    output = stream.getvalue()
    assert progress.mode == ui.PROGRESS_TEXT
    assert "下载" in output, "the code page can represent the CJK part"
    assert "🎵" not in output
    assert "\\U0001f3b5" in output


def test_emit_falls_back_without_a_byte_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _LegacyTextStream("cp1252")
    monkeypatch.setattr(sys, "stdout", stream)

    ui.emit('{"name": "🔥"}')

    output = stream.getvalue()
    assert output.endswith("\n")
    assert "🔥" not in output
    assert "\\U0001f525" in output, "a command must not die on its own output"


def test_emit_survives_a_lone_surrogate_on_a_utf8_buffer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)

    ui.emit("bad \ud800 title")
    stream.flush()

    written = stream.buffer.getvalue()
    assert b"\\ud800" in written, "invalid Unicode must be escaped, not fatal"
    assert b"bad " in written and b" title\n" in written


# --------------------------------------------------------------------------- #
# stdout purity / table geometry
# --------------------------------------------------------------------------- #
def test_json_stdout_is_never_colored_or_polluted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = io.StringIO()
    stderr = _TTYStringIO()
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(ui, "_CONSOLE", ui._build_console(file=stderr))

    payload = {"title": "三体 🎵"}
    ui.emit(json.dumps(payload, ensure_ascii=False))
    ui.info("diagnostic")
    ui.success("ok")
    ui.render_table("T", ["c"], [["v"]])

    assert stdout.getvalue() == json.dumps(payload, ensure_ascii=False) + "\n"
    assert _CSI.search(stdout.getvalue()) is None
    assert "diagnostic" in stderr.getvalue(), "diagnostics stay on stderr"


@pytest.mark.parametrize("width", [24, 40, 80])
def test_table_cjk_borders_stay_aligned(
    monkeypatch: pytest.MonkeyPatch, width: int
) -> None:
    stream = io.StringIO()
    monkeypatch.setattr(ui, "_CONSOLE", ui._build_console(file=stream, width=width))

    ui.render_table(
        "作品",
        ["书名", "作者"],
        [
            ["斗破苍穹", "天蚕土豆"],
            ["A very long english title", "zzz"],
        ],
    )

    lines = [line for line in stream.getvalue().splitlines() if line]
    assert lines
    widths = {_display_width(line) for line in lines}
    assert widths == {width}, f"misaligned table lines: {sorted(widths)}"
