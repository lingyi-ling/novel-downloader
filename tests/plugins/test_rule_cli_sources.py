#!/usr/bin/env python3
"""
CLI ``sources`` subcommand tests.

``sources list`` (with and without ``--check``), ``sources check``, ``sources
add`` and ``sources remove`` are exercised fully offline by capturing the
``ui`` output helpers instead of printing to a terminal.  No server, no network.
"""

from __future__ import annotations

import json
from argparse import ArgumentParser
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader.apps.cli import ui
from novel_downloader.apps.cli.commands import sources as sources_module
from novel_downloader.apps.cli.commands.sources import SourcesCmd
from novel_downloader.plugins.rule import reload_rules
from novel_downloader.plugins.utils.site_probe import ProbeResult


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
def _forbid_implicit_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if any command probes without an explicit opt-in.

    ``--check`` used to reach the network; this guard is what keeps that
    regression from coming back.  Probe-enabled tests override it with
    ``_install_fake_probe``.
    """

    async def forbidden(
        items: Sequence[tuple[str, str]],
        *,
        timeout: float = 3.0,
        concurrency: int = 8,
    ) -> list[ProbeResult]:
        raise AssertionError("a network probe ran without --probe/--by-latency")

    monkeypatch.setattr(sources_module, "probe_sites", forbidden)


def _install_fake_probe(
    monkeypatch: pytest.MonkeyPatch,
    *,
    latencies: dict[str, float | None] | None = None,
    calls: list[list[tuple[str, str]]] | None = None,
) -> None:
    """Install an in-memory probe that never touches the network."""

    async def fake_probe_sites(
        items: Sequence[tuple[str, str]],
        *,
        timeout: float = 3.0,
        concurrency: int = 8,
    ) -> list[ProbeResult]:
        if calls is not None:
            calls.append(list(items))
        out: list[ProbeResult] = []
        for index, (key, url) in enumerate(items):
            latency = (latencies or {}).get(key, float(index))
            out.append(
                ProbeResult(
                    site_key=key,
                    url=url,
                    ok=latency is not None,
                    status=200 if latency is not None else None,
                    latency_ms=latency,
                    error=None if latency is not None else "timeout",
                )
            )
        return out

    monkeypatch.setattr(sources_module, "probe_sites", fake_probe_sites)


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


def test_list_with_check_is_offline_and_reports_status(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    _builtin_only(monkeypatch)
    # The autouse guard raises if ``probe_sites`` is touched at all.
    SourcesCmd._list(check=True)

    assert len(recorder.tables) == 1
    _title, columns, rows = recorder.tables[0]
    assert "Status" in columns

    status_idx = columns.index("Status")
    assert rows
    for row in rows:
        assert row[status_idx] == "ok", row


def test_check_output_has_no_network_columns(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bare --check must look exactly like it did before probes existed.
    _builtin_only(monkeypatch)
    SourcesCmd._list(check=True)

    _title, columns, _rows = recorder.tables[0]
    assert "Latency" not in columns
    assert "HTTP" not in columns


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
# sources list --probe: opt-in network columns
# --------------------------------------------------------------------------- #
def _fake_rule(name: str, host: str) -> SimpleNamespace:
    return SimpleNamespace(
        display_name=name,
        search=None,
        hosts=[host],
        base_url=f"https://{host}",
    )


def test_probe_adds_latency_and_http_columns(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    _builtin_only(monkeypatch)
    _install_fake_probe(monkeypatch)
    SourcesCmd._list(probe=True)

    _title, columns, rows = recorder.tables[0]
    # Probing alone does not validate registrations, so there is no Status.
    assert "Status" not in columns
    latency_idx = columns.index("Latency")
    http_idx = columns.index("HTTP")
    assert latency_idx < http_idx

    row = rows[0]
    assert row[http_idx] == "200"
    assert row[latency_idx].endswith(" ms")


def test_check_and_probe_order_is_status_then_latency_then_http(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    _builtin_only(monkeypatch)
    _install_fake_probe(monkeypatch)
    SourcesCmd._list(check=True, probe=True)

    _title, columns, rows = recorder.tables[0]
    assert columns.index("Status") < columns.index("Latency") < columns.index("HTTP")
    assert rows[0][columns.index("Status")] == "ok"


def test_list_without_probe_has_no_network_columns(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    _builtin_only(monkeypatch)
    SourcesCmd._list()

    _title, columns, _rows = recorder.tables[0]
    assert "Latency" not in columns
    assert "HTTP" not in columns


def _forbid_instantiation(message: str) -> Any:
    """Build a ``_probe`` replacement that fails if registrations are checked."""

    def boom(_cls: type[Any], _key: str, _has_search: bool) -> str:
        raise AssertionError(message)

    return classmethod(boom)


def test_list_by_latency_orders_rows_and_puts_unreachable_last(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "novel_downloader.plugins.rule.iter_rules",
        lambda: [
            ("slow", _fake_rule("Slow", "slow.example"), {"origin": "user"}),
            ("fast", _fake_rule("Fast", "fast.example"), {"origin": "user"}),
            ("dead", _fake_rule("Dead", "dead.example"), {"origin": "user"}),
        ],
    )
    # --by-latency must not start instantiation checks either.
    monkeypatch.setattr(
        SourcesCmd,
        "_probe",
        _forbid_instantiation("--by-latency must not imply --check"),
    )
    _install_fake_probe(
        monkeypatch,
        latencies={"slow": 250.0, "fast": 40.0, "dead": None},
    )

    SourcesCmd._list(by_latency=True)

    _title, columns, rows = recorder.tables[0]
    assert "Status" not in columns
    assert [row[0] for row in rows] == ["fast", "slow", "dead"]
    latency_idx = columns.index("Latency")
    http_idx = columns.index("HTTP")
    assert rows[0][latency_idx] == "40 ms"
    assert rows[1][latency_idx] == "250 ms"
    assert rows[2][latency_idx] == "-"
    assert rows[2][http_idx] == "-"


def test_list_by_latency_implies_probe(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    _builtin_only(monkeypatch)
    _install_fake_probe(monkeypatch)
    SourcesCmd._list(by_latency=True)

    _title, columns, _rows = recorder.tables[0]
    assert "Latency" in columns


def test_probe_without_check_does_not_validate_registrations(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    _builtin_only(monkeypatch)
    _install_fake_probe(monkeypatch)
    monkeypatch.setattr(
        SourcesCmd,
        "_probe",
        _forbid_instantiation("--probe alone must not instantiate sources"),
    )

    SourcesCmd._list(probe=True)


def test_list_json_adds_network_fields_when_probing(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    _builtin_only(monkeypatch)
    _install_fake_probe(monkeypatch)
    SourcesCmd._list(probe=True, as_json=True)

    payload = json.loads(recorder.emitted[0])
    assert payload[0]["latency_ms"] == 0.0
    assert payload[0]["http_status"] == 200
    # No Status column was requested, so no registration check ran.
    assert payload[0]["status"] is None


def test_list_json_without_probe_omits_network_fields(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Backward compatibility: the offline payload must not grow new keys.
    _builtin_only(monkeypatch)
    SourcesCmd._list(as_json=True)

    item = json.loads(recorder.emitted[0])[0]
    assert "latency_ms" not in item
    assert "http_status" not in item


def test_check_json_has_no_network_fields(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    _builtin_only(monkeypatch)
    SourcesCmd._list(check=True, as_json=True)

    item = json.loads(recorder.emitted[0])[0]
    assert item["status"] == "ok"
    assert "latency_ms" not in item
    assert "http_status" not in item


def test_list_network_failure_does_not_change_exit_code(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    _builtin_only(monkeypatch)
    _install_fake_probe(monkeypatch, latencies={"biquge5r": None})

    # No SystemExit: network reachability is informational, and --probe alone
    # never validates instantiation either.
    SourcesCmd._list(probe=True, as_json=True)

    payload = json.loads(recorder.emitted[0])
    assert payload[0]["status"] is None
    assert payload[0]["latency_ms"] is None
    assert payload[0]["http_status"] is None


def test_check_failure_exits_nonzero_even_with_probe(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    _builtin_only(monkeypatch)
    _install_fake_probe(monkeypatch)
    monkeypatch.setattr(
        SourcesCmd,
        "_probe",
        classmethod(lambda _cls, _key, _has_search: "FAIL: boom"),
    )

    with pytest.raises(SystemExit) as excinfo:
        SourcesCmd._list(check=True, probe=True)

    assert excinfo.value.code == 1


@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({"probe_timeout": 0.0}, "probe-timeout"),
        ({"probe_timeout": -1.0}, "probe-timeout"),
        ({"probe_timeout": float("inf")}, "probe-timeout"),
        ({"probe_timeout": float("nan")}, "probe-timeout"),
        ({"probe_concurrency": 0}, "probe-concurrency"),
        ({"probe_concurrency": -3}, "probe-concurrency"),
    ],
)
def test_invalid_probe_knobs_are_usage_errors(
    recorder: _Recorder,
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, float],
    needle: str,
) -> None:
    _builtin_only(monkeypatch)
    _install_fake_probe(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        SourcesCmd._list(probe=True, **kwargs)  # type: ignore[arg-type]

    assert excinfo.value.code == 2
    assert recorder.errors
    assert needle in recorder.errors[0]


def test_invalid_probe_knobs_are_ignored_when_not_probing(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ``--check --probe-timeout 0`` stays a valid offline invocation: the
    # knob only matters when a probe actually runs.
    _builtin_only(monkeypatch)
    SourcesCmd._list(check=True, probe_timeout=0.0, probe_concurrency=0)

    assert not recorder.errors
    assert recorder.tables


def test_list_probe_flags_are_parsed() -> None:
    parser = ArgumentParser()
    SourcesCmd.add_arguments(parser)

    args = parser.parse_args(
        [
            "list",
            "--probe",
            "--by-latency",
            "--probe-timeout",
            "1.5",
            "--probe-concurrency",
            "2",
        ]
    )

    assert args.probe is True
    assert args.by_latency is True
    assert args.probe_timeout == 1.5
    assert args.probe_concurrency == 2


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
