#!/usr/bin/env python3
"""
tests.apps.test_cli_completion
-------------------------------

Completion scripts must track the *real* argparse tree and stay valid shell
input.  These tests guard the drift that plain word lists invite:

* every shell the CLI offers (Bash, Zsh, Fish, PowerShell) renders, and the
  newly added options are actually offered at the right path;
* the generated tree matches the options argparse registered, node by node;
* duplicate or exotic option strings never produce a broken script;
* a path outside the tree falls through to "no candidates" instead of leaving
  a stale word list behind;
* scripts are LF-only (Windows text-mode stdout would otherwise emit CRLF);
* each script passes its shell's own syntax checker when that shell exists,
  with structural static assertions as the fallback.

No shell is required: the native checks are skipped and the static ones run.
"""

from __future__ import annotations

import shutil
import subprocess
from argparse import ArgumentParser, _SubParsersAction
from pathlib import Path

import pytest

from novel_downloader.apps.cli.commands.base import localize_help
from novel_downloader.apps.cli.commands.clean import CleanCmd
from novel_downloader.apps.cli.commands.completion import (
    SHELLS,
    CompletionNode,
    build_completion_tree,
    detect_shell,
    render_completion,
)
from novel_downloader.apps.cli.commands.export import ExportCmd
from novel_downloader.apps.cli.commands.music import MusicCmd
from novel_downloader.apps.cli.commands.storage import StorageCmd

#: The command families whose options this module is responsible for.
COMMAND_CLASSES = (CleanCmd, ExportCmd, StorageCmd, MusicCmd)

#: Paths exercised by the drift test (leading space, as the scripts key them).
DRIFT_PATHS = (
    "",
    " clean",
    " clean logs",
    " clean cache",
    " clean book",
    " export",
    " storage",
    " storage backup",
    " music health",
    " music platforms",
)

#: Options added or changed by the recent CLI rounds; each must be offered.
NEW_OPTIONS = (
    (" clean logs", "--dry-run"),
    (" clean cache", "--dry-run"),
    (" clean book", "--dry-run"),
    (" export", "--out"),
    (" export", "--stage"),
    (" storage", "--dry-run"),
    (" storage", "--clean"),
    (" storage backup", "--out"),
    (" music health", "--platform"),
    (" music health", "--keyword"),
    (" music health", "--json"),
    (" music platforms", "--json"),
)


def _tree() -> CompletionNode:
    return build_completion_tree(list(COMMAND_CLASSES))


def _node(path: str, tree: CompletionNode | None = None) -> CompletionNode:
    node = _tree() if tree is None else tree
    for part in path.split():
        node = node.subcommands[part]
    return node


# ---------------------------------------------------------------------------
# argparse drift
# ---------------------------------------------------------------------------


def _real_parser_for(path: str) -> ArgumentParser:
    """Build the parser exactly as ``completion`` and ``cli_main`` do, then walk."""
    parser = ArgumentParser(prog="novel-cli", add_help=False)
    localize_help(parser)
    parser.add_argument("-v", "--version", action="version", version="novel-cli")
    parser.add_argument("--lang")

    subparsers = parser.add_subparsers(dest="command", title="Commands")
    for cls in COMMAND_CLASSES:
        cls.register(subparsers)

    current = parser
    for part in path.split():
        nested = next(
            action
            for action in current._actions
            if isinstance(action, _SubParsersAction)
        )
        current = nested.choices[part]
    return current


def _registered_options(parser: ArgumentParser) -> set[str]:
    return {
        option
        for action in parser._actions
        if not isinstance(action, _SubParsersAction)
        for option in action.option_strings
    }


@pytest.mark.parametrize("path", DRIFT_PATHS)
def test_completion_tree_matches_registered_argparse_options(path: str) -> None:
    """The completion tree must expose exactly the options argparse knows."""
    assert set(_node(path).options) == _registered_options(_real_parser_for(path))


@pytest.mark.parametrize(("path", "option"), NEW_OPTIONS)
def test_new_options_are_offered(path: str, option: str) -> None:
    assert option in _node(path).options


def test_shells_cover_every_documented_shell() -> None:
    assert set(SHELLS) == {"bash", "zsh", "fish", "powershell"}


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shell", SHELLS)
def test_scripts_offer_the_new_options(shell: str) -> None:
    script = render_completion(_tree(), shell)

    for option in ("--dry-run", "--out", "--stage", "--platform", "--keyword"):
        assert option in script, f"{shell} script lost {option}"


@pytest.mark.parametrize("shell", SHELLS)
def test_rendered_scripts_use_lf_only(shell: str) -> None:
    script = render_completion(_tree(), shell)

    assert script
    assert "\r" not in script


def test_duplicate_options_are_rendered_once() -> None:
    """A node carrying the same option several times must not repeat it."""
    node = CompletionNode(options=("--dup", "--dup", "--dup"))

    for shell in SHELLS:
        script = render_completion(node, shell)
        assert script.count("--dup") == 1, f"{shell} repeated a duplicate option"


def test_exotic_option_strings_are_escaped_per_shell() -> None:
    """Unknown option spellings must be quoted, not injected into the script."""
    node = CompletionNode(options=("--weird'quote", '--weird"quote', "--weird$var"))

    bash_script = render_completion(node, "bash")
    zsh_script = render_completion(node, "zsh")
    fish_script = render_completion(node, "fish")
    powershell_script = render_completion(node, "powershell")

    for script in (bash_script, zsh_script):
        assert "'\\''" in script  # close, escaped quote, reopen
    assert "\\'" in fish_script  # Fish escapes the quote with a backslash
    assert "''" in powershell_script  # PowerShell doubles the quote
    for script in (bash_script, zsh_script, fish_script, powershell_script):
        assert "--weird" in script
        assert "\r" not in script


# ---------------------------------------------------------------------------
# static structure (used when the shell binary is unavailable)
# ---------------------------------------------------------------------------


def _assert_bash_structure(script: str) -> None:
    assert script.startswith("# bash completion for novel-cli")
    assert script.rstrip().endswith("complete -F _novel_cli_completion novel-cli")
    assert script.count("{") == script.count("}")
    assert script.count('    case "$path" in') == script.count("    esac")
    # The catch-all must be a real glob, not the quoted literal ``"*"``.
    assert '        "*")' not in script
    assert "        *)" in script
    for line in script.splitlines():
        if line.strip().startswith("words="):
            assert line.count("'") % 2 == 0, line


def _assert_zsh_structure(script: str) -> None:
    assert script.startswith("#compdef novel-cli")
    assert "compdef _novel_cli_completion novel-cli" in script
    assert '        "*")' not in script
    assert "        *)" in script
    for line in script.splitlines():
        if "choices=(" in line:
            assert line.rstrip().endswith(")")
        if line.strip().startswith("choices=("):
            assert line.count("'") % 2 == 0, line


def _assert_fish_structure(script: str) -> None:
    assert script.startswith("# fish completion for novel-cli")
    assert "function __novel_cli_completion" in script
    assert "complete -c novel-cli -f -a '(__novel_cli_completion)'" in script

    lines = [line.strip() for line in script.splitlines()]
    assert sum(line.startswith("function ") for line in lines) == 1
    # One ``end`` closes the if/else-if chain, one closes the function.
    assert lines.count("end") == 2
    assert any(line.startswith("if test ") for line in lines)
    assert any(line.startswith("else if test ") for line in lines)
    assert "case '*'" not in script


def _assert_powershell_structure(script: str) -> None:
    assert "Register-ArgumentCompleter -Native -CommandName 'novel-cli'" in script
    assert "switch -Exact" in script
    assert "default { @() }" in script
    assert "'*'" not in script
    assert script.count("{") == script.count("}")


_STATIC_CHECKS = {
    "bash": _assert_bash_structure,
    "zsh": _assert_zsh_structure,
    "fish": _assert_fish_structure,
    "powershell": _assert_powershell_structure,
}


@pytest.mark.parametrize("shell", SHELLS)
def test_static_structure_is_valid(shell: str) -> None:
    """Structural assertions that hold without any shell binary installed."""
    _STATIC_CHECKS[shell](render_completion(_tree(), shell))


# ---------------------------------------------------------------------------
# native syntax checks (skipped when the shell is absent)
# ---------------------------------------------------------------------------


def _powershell_executable() -> str | None:
    for name in ("pwsh", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _check_powershell(script: str, tmp_path: Path) -> None:
    executable = _powershell_executable()
    if executable is None:
        _assert_powershell_structure(script)
        return

    script_path = tmp_path / "novel-cli.ps1"
    script_path.write_bytes(script.encode("utf-8"))
    result = subprocess.run(
        [
            executable,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "$errors = $null;"
                f"$null = [System.Management.Automation.Language.Parser]::ParseFile("
                f"'{script_path}', [ref]$null, [ref]$errors);"
                "if ($errors.Count -gt 0) {"
                " $errors | ForEach-Object { Write-Error $_.Message }; exit 1 }"
            ),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("shell", SHELLS)
def test_script_passes_native_syntax_check(shell: str, tmp_path: Path) -> None:
    script = render_completion(_tree(), shell)

    if shell == "powershell":
        _check_powershell(script, tmp_path)
        return

    executable = shutil.which(shell)
    if executable is None:
        # No binary: fall back to the static assertions for this shell.
        _STATIC_CHECKS[shell](script)
        return

    script_path = tmp_path / f"novel-cli.{shell}"
    script_path.write_bytes(script.encode("utf-8"))
    result = subprocess.run(
        [executable, "-n", str(script_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# shell detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "platform", "expected"),
    [
        ({"SHELL": "/usr/bin/fish"}, "linux", "fish"),
        ({"SHELL": "/usr/bin/zsh"}, "win32", "zsh"),
        ({}, "win32", "powershell"),
    ],
)
def test_detect_shell_recognizes_new_shells(
    env: dict[str, str], platform: str, expected: str
) -> None:
    assert detect_shell(env, platform=platform) == expected
