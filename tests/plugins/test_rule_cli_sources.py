#!/usr/bin/env python3
"""
CLI ``sources`` subcommand tests.

``sources list`` (with and without ``--check``), ``sources check``, ``sources
add`` and ``sources remove`` are exercised fully offline by capturing the
``ui`` output helpers instead of printing to a terminal.  No server, no network.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.apps.cli import ui
from novel_downloader.apps.cli.commands.sources import SourcesCmd
from novel_downloader.plugins.rule import reload_rules


class _Recorder:
    """Records the messages passed to the ``ui`` helpers."""

    def __init__(self) -> None:
        self.success: list[str] = []
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.emitted: list[str] = []
        self.tables: list[tuple[str, list[str], list[list[str]]]] = []

    # bound methods handed to monkeypatch.setattr
    def on_success(self, message: str) -> None:
        self.success.append(message)

    def on_error(self, message: str) -> None:
        self.errors.append(message)

    def on_warn(self, message: str) -> None:
        self.warnings.append(message)

    def on_emit(self, text: str) -> None:
        self.emitted.append(text)

    def on_table(self, title: str, columns: Sequence[str], rows: Any) -> None:
        self.tables.append((title, list(columns), [list(r) for r in rows]))


@pytest.fixture()
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(ui, "success", rec.on_success)
    monkeypatch.setattr(ui, "error", rec.on_error)
    monkeypatch.setattr(ui, "warn", rec.on_warn)
    monkeypatch.setattr(ui, "emit", rec.on_emit)
    monkeypatch.setattr(ui, "render_table", rec.on_table)
    return rec


@pytest.fixture(autouse=True)
def _isolated_rule_state() -> Iterator[None]:
    reload_rules()
    yield
    reload_rules()


def _valid_rule(site_key: str) -> dict[str, Any]:
    return {
        "site_key": site_key,
        "base_url": "https://demo.example",
        "hosts": ["demo.example"],
        "chapter": {"url": "{base_url}/{book_id}/{chapter_id}.html"},
    }


def _write(path: Path, payload: dict[str, Any]) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# sources check
# --------------------------------------------------------------------------- #
def test_check_accepts_a_valid_rule(tmp_path: Path, recorder: _Recorder) -> None:
    path = _write(tmp_path / "ok.json", _valid_rule("oksite"))

    SourcesCmd._check(str(path))

    assert not recorder.errors
    assert len(recorder.success) == 1
    assert "oksite" in recorder.success[0]


def test_check_rejects_a_missing_file(tmp_path: Path, recorder: _Recorder) -> None:
    with pytest.raises(SystemExit) as excinfo:
        SourcesCmd._check(str(tmp_path / "nope.json"))

    assert excinfo.value.code == 2
    assert recorder.errors and not recorder.success
    assert "not found" in recorder.errors[0].lower()


def test_check_rejects_invalid_json(tmp_path: Path, recorder: _Recorder) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{ not json", encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        SourcesCmd._check(str(path))

    assert excinfo.value.code == 1
    assert recorder.errors
    assert "invalid json" in recorder.errors[0].lower()


def test_check_rejects_duplicate_keys(tmp_path: Path, recorder: _Recorder) -> None:
    # ``json.loads`` silently keeps the last duplicate; ``sources check`` must
    # reject the file (aligned with the rule loader) instead of validating the
    # last-wins value.
    path = tmp_path / "dup.json"
    path.write_text(
        '{"site_key": "dupsite", "site_key": "othersite",'
        ' "chapter": {"url": "{base_url}/{book_id}/{chapter_id}.html"}}',
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as excinfo:
        SourcesCmd._check(str(path))

    assert excinfo.value.code == 1
    assert recorder.errors
    assert not recorder.success
    assert "duplicate key" in recorder.errors[0]
    assert "site_key" in recorder.errors[0]


def test_check_rejects_duplicate_keys_in_a_nested_section(
    tmp_path: Path, recorder: _Recorder
) -> None:
    # Duplicate keys are rejected at every nesting level, not only at the root.
    path = tmp_path / "dup-nested.json"
    path.write_text(
        '{"site_key": "dupsite",'
        ' "chapter": {"url": "u1", "url": "u2",'
        ' "content": {"selector": "div#content"}}}',
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as excinfo:
        SourcesCmd._check(str(path))

    assert excinfo.value.code == 1
    assert recorder.errors
    assert not recorder.success
    assert "duplicate key" in recorder.errors[0]
    assert "'url'" in recorder.errors[0]


def test_check_accepts_underscore_comment_keys(
    tmp_path: Path, recorder: _Recorder
) -> None:
    # Keys prefixed with ``_`` are the comment convention and never validated.
    payload = _valid_rule("commentsite")
    payload["_comment"] = "kept as an annotation, ignored by validation"
    payload["chapter"] = {
        "_note": "section-level comment",
        "url": "{base_url}/{book_id}/{chapter_id}.html",
    }
    path = _write(tmp_path / "commented.json", payload)

    SourcesCmd._check(str(path))

    assert not recorder.errors
    assert len(recorder.success) == 1
    assert "commentsite" in recorder.success[0]


def test_check_rejects_a_structurally_invalid_rule(
    tmp_path: Path, recorder: _Recorder
) -> None:
    # Valid JSON, but no site_key / no book / chapter section.
    path = _write(tmp_path / "bad.json", {"base_url": "https://demo.example"})

    with pytest.raises(SystemExit) as excinfo:
        SourcesCmd._check(str(path))

    assert excinfo.value.code == 1
    assert recorder.errors
    assert not recorder.success


def test_check_surfaces_an_invalid_regex_as_a_rule_error(
    tmp_path: Path, recorder: _Recorder
) -> None:
    payload = _valid_rule("regexsite")
    payload["resolve"] = {"book": "([unclosed"}
    path = _write(tmp_path / "badregex.json", payload)

    with pytest.raises(SystemExit) as excinfo:
        SourcesCmd._check(str(path))

    assert excinfo.value.code == 1
    assert recorder.errors
    assert "invalid regex" in recorder.errors[0].lower()


# --------------------------------------------------------------------------- #
# sources list
# --------------------------------------------------------------------------- #
def _builtin_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make rule discovery deterministic: builtin rules only."""
    monkeypatch.setattr("novel_downloader.plugins.rule._user_dirs", lambda: [])
    reload_rules()


def test_list_renders_available_rules(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    _builtin_only(monkeypatch)
    SourcesCmd._list(check=False)

    assert len(recorder.tables) == 1
    title, columns, rows = recorder.tables[0]
    assert "Status" not in columns
    assert any(row[0] == "biquge5r" for row in rows)


def test_list_with_check_probes_each_source(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    _builtin_only(monkeypatch)
    SourcesCmd._list(check=True)

    assert len(recorder.tables) == 1
    _title, columns, rows = recorder.tables[0]
    assert "Status" in columns

    status_idx = columns.index("Status")
    assert rows
    for row in rows:
        assert row[status_idx] == "ok", row


def test_list_reports_when_no_rules_are_available(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force the rule loader to return nothing.
    monkeypatch.setattr("novel_downloader.plugins.rule.iter_rules", lambda: [])
    SourcesCmd._list(check=False)

    assert recorder.warnings
    assert not recorder.tables


def test_list_json_emits_a_parseable_payload(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    _builtin_only(monkeypatch)
    SourcesCmd._list(check=True, as_json=True)

    assert len(recorder.emitted) == 1
    payload = json.loads(recorder.emitted[0])
    assert [item["site_key"] for item in payload] == ["biquge5r"]
    assert payload[0]["search"] is True
    assert payload[0]["status"] == "ok"


def test_list_json_is_empty_list_without_rules(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("novel_downloader.plugins.rule.iter_rules", lambda: [])
    SourcesCmd._list(check=False, as_json=True)

    assert recorder.emitted == ["[]"]


# --------------------------------------------------------------------------- #
# sources add / remove
# --------------------------------------------------------------------------- #
def test_add_installs_and_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
) -> None:
    install_dir = tmp_path / "installed"
    monkeypatch.setenv("NOVEL_RULES_DIR", str(install_dir))
    source = _write(tmp_path / "src.json", _valid_rule("addsite"))

    SourcesCmd._add(str(source), force=False)

    assert recorder.success
    assert (install_dir / "src.json").is_file()


def test_add_reports_a_bad_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
) -> None:
    monkeypatch.setenv("NOVEL_RULES_DIR", str(tmp_path / "installed"))
    source = _write(tmp_path / "bad.json", {"base_url": "https://demo.example"})

    with pytest.raises(SystemExit) as excinfo:
        SourcesCmd._add(str(source), force=False)

    assert excinfo.value.code == 1
    assert recorder.errors
    assert "could not install" in recorder.errors[0].lower()


def test_add_reports_a_missing_file(tmp_path: Path, recorder: _Recorder) -> None:
    with pytest.raises(SystemExit) as excinfo:
        SourcesCmd._add(str(tmp_path / "ghost.json"), force=False)

    assert excinfo.value.code == 2
    assert recorder.errors
    assert "not found" in recorder.errors[0].lower()


def test_remove_deletes_a_user_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorder: _Recorder
) -> None:
    monkeypatch.setenv("NOVEL_RULES_DIR", str(tmp_path))
    _write(tmp_path / "gone.json", _valid_rule("gonesite"))
    reload_rules()

    SourcesCmd._remove("gonesite")

    assert recorder.success
    assert not (tmp_path / "gone.json").exists()


def test_remove_reports_a_builtin_or_unknown_key(recorder: _Recorder) -> None:
    with pytest.raises(SystemExit) as excinfo:
        SourcesCmd._remove("biquge5r")
    assert excinfo.value.code == 1
    assert recorder.errors
    assert "built-in" in recorder.errors[0].lower()

    recorder.errors.clear()
    with pytest.raises(SystemExit) as excinfo:
        SourcesCmd._remove("no-such-source")
    assert excinfo.value.code == 1
    assert recorder.errors
    assert "unknown" in recorder.errors[0].lower()
