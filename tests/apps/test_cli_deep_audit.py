#!/usr/bin/env python3
"""
tests.apps.test_cli_deep_audit
------------------------------

Deep-audit regression tests for the CLI layer.

These exercise the *real* rich prompt/table/progress pipeline (``input`` is
scripted instead of the helpers being mocked away) for the failure classes
found in the 2026-10 audit:

* rich markup escaping: user data (site keys, book ids, stage names, ...)
  containing ``[...]`` used to be swallowed silently or raise ``MarkupError``;
* prompts must render on stderr so stdout stays machine readable;
* the advertised multi-book selection (``1,3,5``) must actually be accepted;
* the Ctrl+C contract (``Aborted.`` on stderr, exit code 1) must also hold in
  interactive mode and at prompts;
* exit-code / ``--json`` non-interaction contract for ``download``/``export``.

Every test is offline and deterministic; nothing is read from the real user
profile and no network access happens.
"""

from __future__ import annotations

import asyncio
import builtins
import io
import sys
from argparse import ArgumentParser
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from rich.console import Console

from novel_downloader.apps.cli import main as cli_main_module
from novel_downloader.apps.cli import prompts, ui
from novel_downloader.apps.cli.commands.completion import CompletionCmd
from novel_downloader.apps.cli.commands.download import DownloadCmd
from novel_downloader.apps.cli.commands.export import ExportCmd

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def scripted_input(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Script ``builtins.input``: fed lines are returned in order, then EOF.

    An exception instance in the queue is raised instead of returned, so a
    ``KeyboardInterrupt`` or ``EOFError`` can be delivered deterministically.
    """

    def install(*items: Any) -> None:
        queue: list[Any] = list(items)

        def fake_input(_prompt: str = "") -> str:
            if not queue:
                raise EOFError
            item = queue.pop(0)
            if isinstance(item, BaseException):
                raise item
            return str(item)

        monkeypatch.setattr(builtins, "input", fake_input)

    return install


@pytest.fixture()
def fake_stderr_console(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    """Point ``ui._CONSOLE`` at a terminal-like buffer so rich really renders."""
    buffer = io.StringIO()
    monkeypatch.setattr(
        ui, "_CONSOLE", Console(file=buffer, force_terminal=True, width=120)
    )
    return buffer


def _parser(*commands: type) -> ArgumentParser:
    """Build a minimal top-level parser with the given command classes."""
    parser = ArgumentParser(prog="novel-cli", add_help=False)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in commands:
        command.register(subparsers)
    return parser


def _make_books(tmp_path: Path, site: str, n: int) -> None:
    for i in range(n):
        book = tmp_path / site / f"book{i}"
        book.mkdir(parents=True)


# ---------------------------------------------------------------------------
# rich markup escaping (crash / silent corruption)
# ---------------------------------------------------------------------------


def test_info_message_with_markup_is_rendered_literally(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ui.info("Using site [b520]")
    assert "Using site [b520]" in capsys.readouterr().err


def test_status_message_with_markup_is_rendered_literally(
    fake_stderr_console: io.StringIO,
) -> None:
    with ui.status("Searching for '[三体]'..."):
        pass
    assert "Searching for '[三体]'..." in fake_stderr_console.getvalue()


def test_confirm_message_with_unknown_tag_is_rendered_literally(
    scripted_input: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    scripted_input("y")

    assert ui.confirm("Remove site [b520] cache?") is True
    assert "[b520]" in capsys.readouterr().err


def test_confirm_message_with_closing_tag_does_not_crash(
    scripted_input: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    # A site key like ``x[/b]`` turns the question into mismatched markup,
    # which used to raise MarkupError out of the CLI.
    scripted_input("y")

    assert ui.confirm("Remove all data for site 'x[/b]'?") is True
    assert "x[/b]" in capsys.readouterr().err


def test_prompt_message_with_markup_is_rendered_literally(
    scripted_input: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    scripted_input("book[1]")

    assert ui.prompt("Enter [book] name") == "book[1]"
    assert "[book]" in capsys.readouterr().err


def test_prompt_choice_message_with_closing_tag_does_not_crash(
    scripted_input: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    scripted_input("1")

    assert ui.prompt_choice("Pick [x[/b]] choice", ["1"]) == "1"
    assert "x[/b]" in capsys.readouterr().err


def test_render_table_title_with_markup_does_not_crash(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # select_books() formats the site key into the table title; a site key
    # containing markup used to crash the table render.
    ui.render_table("Available Books for x[/b] · Page 1/1", ["Book ID"], [["123"]])
    assert "123" in capsys.readouterr().err


def test_render_table_column_with_markup_does_not_crash(
    capsys: pytest.CaptureFixture[str],
) -> None:
    ui.render_table("T", ["Weird [/b] col"], [["x"]])
    assert "Weird [/b] col" in capsys.readouterr().err


def test_progress_ui_prefix_with_markup_does_not_crash(
    fake_stderr_console: io.StringIO,
) -> None:
    # CLIProcessUI builds the progress prefix from the configured stage name.
    progress = ui.ProgressUI(prefix="Stage [/b]")
    progress.start()
    try:
        asyncio.run(progress.update(1, 2))
    finally:
        progress.stop()

    assert "Stage [/b]" in fake_stderr_console.getvalue()


# ---------------------------------------------------------------------------
# prompt stream: prompts belong on stderr, stdout stays machine readable
# ---------------------------------------------------------------------------


def test_confirm_prompt_renders_on_stderr_not_stdout(
    scripted_input: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    scripted_input("y")

    assert ui.confirm("Proceed with download?") is True
    captured = capsys.readouterr()
    assert "Proceed with download?" in captured.err
    assert captured.out == ""


def test_prompt_renders_on_stderr_not_stdout(
    scripted_input: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    scripted_input("hello")

    assert ui.prompt("Enter the book URL") == "hello"
    captured = capsys.readouterr()
    assert "Enter the book URL" in captured.err
    assert captured.out == ""


def test_prompt_choice_renders_on_stderr_not_stdout(
    scripted_input: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    scripted_input("1")

    assert ui.prompt_choice("Select an action", ["1"]) == "1"
    captured = capsys.readouterr()
    assert "Select an action" in captured.err
    assert captured.out == ""


# ---------------------------------------------------------------------------
# interactive selection through the real (unmocked) prompt pipeline
# ---------------------------------------------------------------------------


def test_select_books_multi_index_through_real_prompt(
    tmp_path: Path, scripted_input: Any
) -> None:
    _make_books(tmp_path, "site", 5)
    # The prompt advertises "Enter numbers (e.g. 1,3,5)" -- this must work.
    scripted_input("1,3")

    assert prompts.select_books(tmp_path, "site") == ["book0", "book2"]


def test_select_books_eof_cancels_through_real_prompt(
    tmp_path: Path, scripted_input: Any
) -> None:
    _make_books(tmp_path, "site", 3)
    scripted_input()  # immediately EOF (piped/closed stdin)

    assert prompts.select_books(tmp_path, "site") == []


def test_select_books_uppercase_input_is_accepted(
    tmp_path: Path, scripted_input: Any
) -> None:
    _make_books(tmp_path, "site", 3)
    scripted_input(" 1 , 3 ")

    assert prompts.select_books(tmp_path, "site") == ["book0", "book2"]


def test_select_site_uppercase_nav_is_accepted(
    tmp_path: Path, scripted_input: Any
) -> None:
    for i in range(12):
        (tmp_path / f"site{i:02d}").mkdir()
    # "N" must page forward like "n" (prompt_choice lowercases its result).
    scripted_input("N", "11")

    assert prompts.select_site(tmp_path, per_page=10) == "site10"


def test_select_main_action_eof_returns_cancel(scripted_input: Any) -> None:
    scripted_input()  # immediately EOF

    assert prompts.select_main_action() == ""


# ---------------------------------------------------------------------------
# Ctrl+C contract: "Aborted." on stderr, exit code 1
# ---------------------------------------------------------------------------


def test_prompt_ctrl_c_propagates(scripted_input: Any) -> None:
    scripted_input(KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        ui.prompt("Enter value")


def test_confirm_ctrl_c_propagates(scripted_input: Any) -> None:
    scripted_input(KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        ui.confirm("Proceed?")


def test_prompt_choice_ctrl_c_propagates(scripted_input: Any) -> None:
    scripted_input(KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        ui.prompt_choice("Select an action", ["1"])


def test_prompt_eof_returns_default(scripted_input: Any) -> None:
    scripted_input()

    assert ui.prompt("Enter value") == ""
    assert ui.prompt("Enter value", default="fallback") == "fallback"


def test_prompt_choice_eof_returns_cancel(scripted_input: Any) -> None:
    scripted_input()

    assert ui.prompt_choice("Select an action", ["1"]) == ""


def test_cli_interactive_mode_ctrl_c_aborts_with_exit_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _interrupted() -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(
        "novel_downloader.apps.cli.interactive.start_interactive", _interrupted
    )
    monkeypatch.setattr(sys, "argv", ["novel-cli"])

    # The interrupt must be converted into the documented "Aborted." exit,
    # not leak a raw KeyboardInterrupt (and a traceback) out of the CLI.
    try:
        cli_main_module.cli_main()
    except SystemExit as exc:
        assert exc.code == 1
    except KeyboardInterrupt:  # pragma: no cover - only on the old behavior
        pytest.fail("interactive mode leaked KeyboardInterrupt instead of exiting 1")
    else:  # pragma: no cover - cli_main always exits here
        pytest.fail("expected SystemExit(1) after Ctrl+C in interactive mode")

    assert "Aborted." in capsys.readouterr().err


def test_cli_command_ctrl_c_aborts_with_exit_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _interrupted(cls: type, args: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(CompletionCmd, "run", classmethod(_interrupted))
    monkeypatch.setattr(sys, "argv", ["novel-cli", "completion"])

    with pytest.raises(SystemExit) as excinfo:
        cli_main_module.cli_main()

    assert excinfo.value.code == 1
    assert "Aborted." in capsys.readouterr().err


# ---------------------------------------------------------------------------
# exit-code / --json contract
# ---------------------------------------------------------------------------


def test_download_site_mode_without_book_ids_is_usage_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from novel_downloader.apps.cli.commands import download as download_module

    captured: dict[str, list[str]] = {"error": []}
    monkeypatch.setattr(
        download_module, "load_or_init_config", lambda *_a, **_k: {"general": {}}
    )
    monkeypatch.setattr(download_module.ui, "error", captured["error"].append)

    args = _parser(DownloadCmd).parse_args(["download", "--site", "b520"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    # Documented contract: "未提供 URL/书籍 ID" is a usage error (exit 2).
    assert excinfo.value.code == 2
    assert any("No book IDs provided" in message for message in captured["error"])


def _export_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from novel_downloader.apps.cli.commands import export as export_module

    monkeypatch.setattr(
        export_module,
        "load_or_init_config",
        lambda *_a, **_k: {"general": {"debug": {"log_dir": str(tmp_path / "logs")}}},
    )


def test_export_json_without_site_never_prompts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _export_env(monkeypatch, tmp_path)
    # A TTY is available, but --json must never enter interactive selection.
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))

    def _must_not_prompt(*_a: Any, **_k: Any) -> None:
        raise AssertionError("interactive selection must not run with --json")

    monkeypatch.setattr(prompts, "select_site", _must_not_prompt)
    monkeypatch.setattr(prompts, "select_books", _must_not_prompt)
    captured: list[str] = []
    from novel_downloader.apps.cli.commands import export as export_module

    monkeypatch.setattr(export_module.ui, "error", captured.append)

    args = _parser(ExportCmd).parse_args(["export", "--json"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    assert any("pass the required arguments explicitly" in m for m in captured)


def test_export_json_with_site_but_no_books_never_prompts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _export_env(monkeypatch, tmp_path)
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: True))

    def _must_not_prompt(*_a: Any, **_k: Any) -> None:
        raise AssertionError("interactive selection must not run with --json")

    monkeypatch.setattr(prompts, "select_books", _must_not_prompt)
    captured: list[str] = []
    from novel_downloader.apps.cli.commands import export as export_module

    monkeypatch.setattr(export_module.ui, "error", captured.append)

    args = _parser(ExportCmd).parse_args(["export", "--site", "b520", "--json"])
    with pytest.raises(SystemExit) as excinfo:
        args.func(args)

    assert excinfo.value.code == 2
    assert any("pass the required arguments explicitly" in m for m in captured)
