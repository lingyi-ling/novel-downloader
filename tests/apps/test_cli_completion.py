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

import re
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
from novel_downloader.apps.cli.commands.doctor import DoctorCmd
from novel_downloader.apps.cli.commands.export import ExportCmd
from novel_downloader.apps.cli.commands.music import MusicCmd
from novel_downloader.apps.cli.commands.search import SearchCmd
from novel_downloader.apps.cli.commands.storage import StorageCmd

#: The command families whose options this module is responsible for.
COMMAND_CLASSES = (CleanCmd, DoctorCmd, ExportCmd, SearchCmd, StorageCmd, MusicCmd)

#: Paths exercised by the drift test (leading space, as the scripts key them).
DRIFT_PATHS = (
    "",
    " clean",
    " clean logs",
    " clean cache",
    " clean book",
    " doctor",
    " export",
    " search",
    " storage",
    " storage backup",
    " storage verify",
    " music download",
    " music health",
    " music platforms",
)

#: Options added or changed by the recent CLI rounds; each must be offered.
NEW_OPTIONS = (
    (" clean logs", "--dry-run"),
    (" clean cache", "--dry-run"),
    (" clean book", "--dry-run"),
    (" doctor", "--fix"),
    (" doctor", "--dry-run"),
    (" doctor", "--fix-age-days"),
    (" doctor", "--max-age-days"),
    (" export", "--out"),
    (" export", "--stage"),
    (" search", "--min-words"),
    (" search", "--max-words"),
    (" search", "--updated-after"),
    (" storage", "--dry-run"),
    (" storage", "--clean"),
    (" storage backup", "--out"),
    (" storage backup", "--keep"),
    (" storage verify", "--json"),
    (" music health", "--platform"),
    (" music health", "--keyword"),
    (" music health", "--json"),
    (" music platforms", "--json"),
)

#: New options grouped by the path that must offer them in every shell.
NEW_SURFACE = {
    " doctor": ("--fix", "--dry-run", "--fix-age-days", "--max-age-days"),
    " search": ("--min-words", "--max-words", "--updated-after"),
    " storage backup": ("--out", "--json", "--keep", "--dry-run"),
    " storage verify": ("--json",),
}

#: Subcommand words that must stay completable at their parent path.
NEW_SUBCOMMANDS = (
    (" storage", "verify"),
    (" storage", "backup"),
    (" music", "download"),
)

#: Argument metavars that must never leak into the completion word lists.
VALUE_METAVARS = frozenset(
    {"N", "M", "DIR", "KEY", "KEYWORD", "SITE", "SECS", "YYYY-MM-DD"}
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

    for option in (
        "--dry-run",
        "--out",
        "--stage",
        "--keep",
        "--platform",
        "--keyword",
        "--min-words",
        "--max-words",
        "--updated-after",
        "--fix",
        "--fix-age-days",
    ):
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
# script entry parsing (assert a flag is offered at the *right* path)
#
# A plain substring check cannot tell ``--keep`` offered by ``storage backup``
# from one offered at the root. These readers recover the ``path -> words``
# mapping from each generated script, using the same quoting rules the
# generators apply.
# ---------------------------------------------------------------------------

_POSIX_QUOTED = re.compile(r"'(?:[^']|'\\'')*'")
_FISH_QUOTED = re.compile(r"'(?:\\.|[^'])*'")
_POWERSHELL_QUOTED = re.compile(r"'(?:''|[^'])*'")
_CASE_LABEL = re.compile(r'^"(.*)"\)$')
_FISH_BRANCH = re.compile(r'^(?:if|else if) test "\$path" = (\'(?:\\.|[^\'])*\')$')
_POWERSHELL_BRANCH = re.compile(r"^('(?:''|[^'])*') \{ @\((.*)\) \}$")
_FISH_PRINTF = "printf '%s\\n'"


def _unquote_posix(token: str) -> str:
    assert token.startswith("'") and token.endswith("'"), token
    return token[1:-1].replace("'\\''", "'")


def _unquote_fish(token: str) -> str:
    assert token.startswith("'") and token.endswith("'"), token
    return re.sub(r"\\(.)", r"\1", token[1:-1])


def _unquote_powershell(token: str) -> str:
    assert token.startswith("'") and token.endswith("'"), token
    return token[1:-1].replace("''", "'")


def _split_posix(text: str) -> list[str]:
    """Split Bash/Zsh words.

    Bash stores the whole word list as one quoted string (``words='a b'``)
    while Zsh quotes each word; joining then splitting handles both.
    """
    quoted = [_unquote_posix(match.group(0)) for match in _POSIX_QUOTED.finditer(text)]
    return " ".join(quoted).split()


def _split_fish(text: str) -> list[str]:
    return [_unquote_fish(match.group(0)) for match in _FISH_QUOTED.finditer(text)]


def _split_powershell(text: str) -> list[str]:
    return [
        _unquote_powershell(match.group(0))
        for match in _POWERSHELL_QUOTED.finditer(text)
    ]


def _entries_bash(script: str) -> dict[str, list[str]]:
    entries: dict[str, list[str]] = {}
    path: str | None = None
    for line in script.splitlines():
        stripped = line.strip()
        label = _CASE_LABEL.fullmatch(stripped)
        if label:
            path = label.group(1)
        elif stripped == "*)":
            path = "*"
        elif path is not None and stripped.startswith("words="):
            entries[path] = _split_posix(stripped[len("words=") :])
            path = None
    return entries


def _entries_zsh(script: str) -> dict[str, list[str]]:
    entries: dict[str, list[str]] = {}
    path: str | None = None
    for line in script.splitlines():
        stripped = line.strip()
        label = _CASE_LABEL.fullmatch(stripped)
        if label:
            path = label.group(1)
        elif stripped == "*)":
            path = "*"
        elif path is not None and stripped.startswith("choices=("):
            entries[path] = _split_posix(stripped[len("choices=(") : -1])
            path = None
    return entries


def _entries_fish(script: str) -> dict[str, list[str]]:
    entries: dict[str, list[str]] = {}
    pending: str | None = None
    for line in script.splitlines():
        stripped = line.strip()
        branch = _FISH_BRANCH.fullmatch(stripped)
        if branch:
            pending = _unquote_fish(branch.group(1))
        elif pending is not None and stripped.startswith(_FISH_PRINTF):
            entries[pending] = _split_fish(stripped[len(_FISH_PRINTF) :])
            pending = None
    return entries


def _entries_powershell(script: str) -> dict[str, list[str]]:
    entries: dict[str, list[str]] = {}
    for line in script.splitlines():
        stripped = line.strip()
        if stripped == "default { @() }":
            entries["*"] = []
            continue
        branch = _POWERSHELL_BRANCH.fullmatch(stripped)
        if branch:
            path = _unquote_powershell(branch.group(1))
            entries[path] = _split_powershell(branch.group(2))
    return entries


_ENTRY_READERS = {
    "bash": _entries_bash,
    "zsh": _entries_zsh,
    "fish": _entries_fish,
    "powershell": _entries_powershell,
}


def _tree_words(node: CompletionNode, prefix: str = "") -> dict[str, list[str]]:
    """Mirror ``_walk``: every path mapped to the words offered there."""
    words = {prefix: node.words()}
    for name, child in node.subcommands.items():
        words.update(_tree_words(child, f"{prefix} {name}"))
    return words


@pytest.mark.parametrize("shell", SHELLS)
def test_every_shell_entry_matches_the_tree(shell: str) -> None:
    """Each branch must offer exactly the words of the node it keys."""
    entries = _ENTRY_READERS[shell](render_completion(_tree(), shell))
    expected = _tree_words(_tree())

    rendered = {path: words for path, words in entries.items() if path != "*"}
    assert set(rendered) == set(expected)
    for path, words in expected.items():
        assert set(rendered[path]) == set(words), f"{shell} branch {path!r} drifted"


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize(("path", "words"), tuple(NEW_SURFACE.items()))
def test_new_surface_is_offered_at_its_path(
    shell: str, path: str, words: tuple[str, ...]
) -> None:
    """New flags must sit on their own subcommand, not just appear anywhere."""
    entries = _ENTRY_READERS[shell](render_completion(_tree(), shell))

    assert path in entries, f"{shell} has no branch for {path!r}"
    missing = set(words) - set(entries[path])
    assert not missing, f"{shell} branch {path!r} lost {sorted(missing)}"


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize(("path", "subcommand"), NEW_SUBCOMMANDS)
def test_new_subcommands_are_completable(
    shell: str, path: str, subcommand: str
) -> None:
    entries = _ENTRY_READERS[shell](render_completion(_tree(), shell))

    assert subcommand in entries[path], f"{shell} cannot complete {path} {subcommand}"


@pytest.mark.parametrize("shell", SHELLS)
def test_option_values_are_not_completed(shell: str) -> None:
    """Value positions keep the historical boundary: no candidates at all."""
    entries = _ENTRY_READERS[shell](render_completion(_tree(), shell))

    offered = {word for words in entries.values() for word in words}
    assert not offered & VALUE_METAVARS

    value_paths = (
        " storage backup --out",
        " storage backup --keep",
        " storage restore --from",
        " search --min-words",
        " search --max-words",
        " search --updated-after",
        " doctor --fix-age-days",
    )
    for path in value_paths:
        assert path not in entries, f"{shell} invented a value branch for {path!r}"

    if shell == "fish":
        # Unknown paths match no branch, so Fish offers nothing.
        assert "*" not in entries
    else:
        assert entries["*"] == []


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
