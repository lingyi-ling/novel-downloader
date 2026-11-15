#!/usr/bin/env python3
"""
tests.apps.test_cli_json_contract_matrix
----------------------------------------

Cross-command contract matrix for the CLI's machine-readable output.

Every ``--json`` command must keep stdout usable by a script while stderr
stays free of terminal control sequences.  This module locks five things for
each covered command, through the real entry point (``python -m
novel_downloader.apps.cli.main``) whenever the command is offline, and through
``cli_main()`` only where a mock has to be injected in-process:

* exit codes: ``0`` on success, ``2`` for a usage error, ``1`` for a runtime
  failure;
* stdout purity: one JSON document and nothing else -- no log line, no
  progress redraw, no human-readable diagnostics (those belong on stderr);
* JSON shape: the document parses and its top level is the type the command
  documents (``object`` for reports, ``array`` for lists);
* failure documents: a command that documents a machine-readable failure
  report still emits it on exit ``1``;
* stderr cleanliness: no ANSI CSI sequence and no carriage-return redraw when
  the streams are not TTYs (as they are under a pipe or ``capsys``).

The commands are driven from the table below.  Most cases seed the real
``settings.toml`` and a real SQLite chapter database, so the whole path
(config load -> storage -> JSON writer) runs offline.  ``search`` uses a local
mock HTTP site and an installed rule source, exactly like
``tests/apps/test_cli_e2e_matrix.py``; ``music``, ``update`` and the failure
paths that need a deterministic trigger replace one engine/manifest/probe with
an in-memory fake because a monkeypatch cannot cross a process boundary.

Output-format note: ``sources list --json`` is the one covered command whose
documented success contract is *multi-line* JSON (see
``docs/guide/cli-examples.md``, output encoding contract).  It is still
asserted to be exactly one JSON document with nothing else on stdout; forcing
it onto a single line would change that documented contract, so the matrix
records the deviation instead of silently rewriting the format.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader import __version__
from novel_downloader.apps.cli.commands import doctor as doctor_module
from novel_downloader.apps.cli.commands import music, sources, storage
from novel_downloader.apps.cli.commands import update as update_module
from novel_downloader.apps.common.updater import ReleaseAsset, UpdateCheck, UpdateInfo
from tests.apps.test_cli_e2e import _seed_config, run_cli, run_cli_inproc
from tests.apps.test_cli_e2e_matrix import (
    _install_mock_rule,
    _MockSiteHandler,
    _one_line_json,
    _seed_chapter_db,
)

#: Matches colour/attribute CSI sequences, the only escape type Rich emits.
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

#: Exit code for a usage error (missing/conflicting arguments).
USAGE_ERROR = 2
#: Exit code for a runtime failure (network, site, config, file, ...).
RUNTIME_FAILURE = 1

#: stdout formats the matrix knows about.
SINGLE_LINE = "single-line"
#: A documented ``indent=2`` multi-line document (``sources list``).
MULTI_LINE_DOCUMENT = "multi-line"


@dataclass(frozen=True)
class _Context:
    """Per-test working directory, isolated app home and scratch directory."""

    work: Path
    home: Path
    scratch: Path
    site: str


#: A case-specific setup hook; may write files and/or patch the CLI in-process.
Prepare = Callable[[pytest.MonkeyPatch, _Context], None]
#: A case-specific assertion on the decoded JSON document.
PayloadCheck = Callable[[Any, _Context], None]


@dataclass(frozen=True)
class _Case:
    """One command invocation and the contract it must satisfy."""

    argv: tuple[str, ...]
    code: int
    #: Expected top-level JSON type; ``None`` means "stdout must stay empty".
    top_level: type | None = None
    #: ``SINGLE_LINE`` or ``MULTI_LINE_DOCUMENT``; ignored when no document.
    stdout_format: str = SINGLE_LINE
    prepare: Prepare | None = None
    inproc: bool = False
    check: PayloadCheck | None = None


# ---------------------------------------------------------------------------
# fixtures and harness
# ---------------------------------------------------------------------------


@pytest.fixture
def search_site() -> Iterator[str]:
    """Run the shared mock search site on a free loopback port."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MockSiteHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def ctx(tmp_path: Path, search_site: str) -> _Context:
    """Create the per-test workspace, app home and scratch tree."""
    work = tmp_path / "work"
    home = tmp_path / "home"
    scratch = tmp_path / "scratch"
    for directory in (work, home, scratch):
        directory.mkdir()
    return _Context(work=work, home=home, scratch=scratch, site=search_site)


def _argv(case: _Case, ctx: _Context) -> list[str]:
    """Expand the ``{work}``/``{home}``/``{scratch}`` placeholders in argv."""
    values = {
        "work": str(ctx.work),
        "home": str(ctx.home),
        "scratch": str(ctx.scratch),
    }
    return [part.format(**values) for part in case.argv]


def _assert_clean_streams(stdout: str, stderr: str) -> None:
    """Assert neither stream carries ANSI control codes or a live progress redraw."""
    for name, text in (("stdout", stdout), ("stderr", stderr)):
        assert _ANSI_RE.search(text) is None, (
            f"{name} leaked an ANSI control sequence: {text!r}"
        )
        assert "\r" not in text, f"{name} redrew a live progress region: {text!r}"


def _invoke(
    case: _Case,
    ctx: _Context,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str, str]:
    """Run one case and return ``(exit code, stdout, stderr)``."""
    if case.prepare is not None:
        case.prepare(monkeypatch, ctx)
    if case.inproc:
        code = run_cli_inproc(
            monkeypatch, _argv(case, ctx), cwd=ctx.work, home=ctx.home
        )
        captured = capsys.readouterr()
        return code, captured.out, captured.err
    result = run_cli(*_argv(case, ctx), cwd=ctx.work, home=ctx.home)
    return result.returncode, result.stdout, result.stderr


def _assert_case(
    case: _Case,
    ctx: _Context,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> Any:
    """Run ``case`` and assert every stream, exit-code and shape contract."""
    code, stdout, stderr = _invoke(case, ctx, monkeypatch, capsys)
    assert code == case.code, (
        f"exit code {code} != {case.code}; stdout={stdout!r} stderr={stderr!r}"
    )
    _assert_clean_streams(stdout, stderr)

    if case.top_level is None:
        assert stdout == "", (
            f"a failed/usage invocation must not emit a document: {stdout!r}"
        )
        return None

    if case.stdout_format == SINGLE_LINE:
        payload = _one_line_json(stdout)
    else:
        # The documented multi-line form: json.loads() already rejects any
        # extra text around the document, and the trailing newline is required.
        assert stdout.endswith("\n"), f"document is not newline-terminated: {stdout!r}"
        payload = json.loads(stdout)
    assert isinstance(payload, case.top_level), (
        f"top-level JSON type is {type(payload).__name__}, "
        f"expected {case.top_level.__name__}"
    )
    if case.check is not None:
        case.check(payload, ctx)
    return payload


# ---------------------------------------------------------------------------
# setup hooks
# ---------------------------------------------------------------------------


def _seed_workspace(monkeypatch: pytest.MonkeyPatch, ctx: _Context) -> None:
    """Install the bundled sample configuration in the working directory."""
    _seed_config(ctx.work)


def _seed_search_rule(monkeypatch: pytest.MonkeyPatch, ctx: _Context) -> None:
    """Seed the config and install the mock rule the search site serves."""
    _seed_config(ctx.work)
    _install_mock_rule(ctx.work, ctx.site)


def _seed_healthy_library(monkeypatch: pytest.MonkeyPatch, ctx: _Context) -> None:
    """Seed the config and one real ``chapter.raw.sqlite`` database."""
    _seed_config(ctx.work)
    _seed_chapter_db(ctx.work)


def _seed_corrupt_library(monkeypatch: pytest.MonkeyPatch, ctx: _Context) -> None:
    """Seed a healthy database and one file SQLite cannot open."""
    _seed_config(ctx.work)
    _seed_chapter_db(ctx.work)
    broken = ctx.work / "raw_data" / "acme" / "broken"
    broken.mkdir(parents=True)
    (broken / "chapter.raw.sqlite").write_bytes(b"this is not a SQLite database")


def _prepare_export_source(monkeypatch: pytest.MonkeyPatch, ctx: _Context) -> None:
    """Seed the config that the export command copies."""
    _seed_config(ctx.work)


def _prepare_import_source(monkeypatch: pytest.MonkeyPatch, ctx: _Context) -> None:
    """Write a valid TOML document to import in the scratch directory."""
    _seed_config(ctx.scratch)


def _prepare_doctor_failure(monkeypatch: pytest.MonkeyPatch, ctx: _Context) -> None:
    """Replace the check runner with one fatal failure."""
    failure = doctor_module.Check("Simulated failure", False, "boom", key="simulated")
    monkeypatch.setattr(
        doctor_module, "collect_checks", lambda *args, **kwargs: [failure]
    )


def _prepare_search_failure(monkeypatch: pytest.MonkeyPatch, ctx: _Context) -> None:
    """Make every site fail: the empty array is still the promised document."""
    _seed_config(ctx.work)

    async def _search(**kwargs: Any) -> list[dict[str, Any]]:
        on_event = kwargs.get("on_event")
        if on_event is not None:
            on_event(
                SimpleNamespace(
                    kind="failed", site="mock", result_count=None, message="down"
                )
            )
        return []

    monkeypatch.setattr("novel_downloader.plugins.search.search", _search)


def _prepare_backup_verification_failure(
    monkeypatch: pytest.MonkeyPatch, ctx: _Context
) -> None:
    """Make the post-write integrity check reject every backup."""
    _seed_config(ctx.work)
    _seed_chapter_db(ctx.work)
    monkeypatch.setattr(
        storage, "_verify_database", lambda _path: "simulated corruption"
    )


def _prepare_sources_check_failure(
    monkeypatch: pytest.MonkeyPatch, ctx: _Context
) -> None:
    """Replace the registry and make ``--check`` reject the one rule."""
    rule = SimpleNamespace(
        display_name="Demo Source", search=object(), hosts=("demo.example",)
    )
    monkeypatch.setattr(
        "novel_downloader.plugins.rule.iter_rules",
        lambda: [("demo", rule, {"origin": "builtin"})],
    )
    monkeypatch.setattr(
        sources.SourcesCmd,
        "_probe",
        classmethod(lambda _cls, _key, _has_search: "FAIL: boom"),
    )


class _FakeMusicEngine:
    """Minimal music engine: one platform, one deterministic health result."""

    def __init__(self, *, healthy: bool = True) -> None:
        self.healthy = healthy

    def labels(self) -> list[tuple[str, str]]:
        """Return one stable key/label pair."""
        return [("netease", "NetEase")]

    def platform_keys(self) -> list[str]:
        """Return the registry keys (``labels`` order)."""
        return [key for key, _label in self.labels()]

    def platform_note(self, _platform: str) -> None:
        """No note for the fake platform."""
        return None

    def health_check(self, _platform: str, _keyword: str) -> dict[str, Any]:
        """Report the configured outcome without any network I/O."""
        if not self.healthy:
            return {"ok": False, "elapsed_ms": 1, "error": "boom"}
        return {"ok": True, "elapsed_ms": 1, "error": ""}


def _patch_music_engine(
    monkeypatch: pytest.MonkeyPatch, engine: _FakeMusicEngine
) -> None:
    """Install ``engine`` as the one the music commands load."""
    monkeypatch.setattr(music, "_require_engine", lambda: engine)


def _prepare_music_platforms(monkeypatch: pytest.MonkeyPatch, ctx: _Context) -> None:
    """Serve the platform listing from the fake engine."""
    _patch_music_engine(monkeypatch, _FakeMusicEngine())


def _prepare_music_health_success(
    monkeypatch: pytest.MonkeyPatch, ctx: _Context
) -> None:
    """Serve one healthy platform from the fake engine."""
    _patch_music_engine(monkeypatch, _FakeMusicEngine(healthy=True))


def _prepare_music_health_failure(
    monkeypatch: pytest.MonkeyPatch, ctx: _Context
) -> None:
    """Serve one failing platform from the fake engine."""
    _patch_music_engine(monkeypatch, _FakeMusicEngine(healthy=False))


def _prepare_music_unknown_platform(
    monkeypatch: pytest.MonkeyPatch, ctx: _Context
) -> None:
    """Serve a registry with exactly one key, so ``nope`` is unknown."""
    _patch_music_engine(monkeypatch, _FakeMusicEngine())


def _cli_release_asset() -> ReleaseAsset:
    """A CLI-shaped release asset for the update success case."""
    return ReleaseAsset(
        name="novel-cli-9.9.9-windows-x64.zip",
        url="https://example.invalid/novel-cli.zip",
        size=123,
        sha256="a" * 64,
    )


def _patch_update_check(
    monkeypatch: pytest.MonkeyPatch,
    info: UpdateInfo | None,
    *,
    error: str | None = None,
) -> None:
    """Replace the release lookup; the command's own guard still runs."""

    def _fake(*_args: object, **_kwargs: object) -> UpdateCheck:
        return UpdateCheck(info=info, error=error, checked_at=0.0)

    monkeypatch.setattr(update_module, "check_for_update", _fake)


def _prepare_update_available(monkeypatch: pytest.MonkeyPatch, ctx: _Context) -> None:
    """Report one newer release carrying a CLI archive."""
    asset = _cli_release_asset()
    info = UpdateInfo(
        current=__version__,
        latest="9.9.9",
        available=True,
        notes_url=None,
        asset=asset,
        assets=(asset,),
    )
    _patch_update_check(monkeypatch, info)


def _prepare_update_unreachable(monkeypatch: pytest.MonkeyPatch, ctx: _Context) -> None:
    """Report a failed release check; the error document is still promised."""
    _patch_update_check(monkeypatch, None, error="unreachable")


# ---------------------------------------------------------------------------
# payload checks
# ---------------------------------------------------------------------------


def _check_doctor_ok(payload: Any, ctx: _Context) -> None:
    assert payload["status"] in {"ok", "warnings"}
    assert payload["summary"]["ok"] is True
    assert payload["summary"]["failures"] == 0


def _check_doctor_failed(payload: Any, ctx: _Context) -> None:
    assert payload["status"] == "failed"
    assert payload["summary"]["ok"] is False
    assert payload["summary"]["failures"] == 1
    assert payload["checks"][0]["key"] == "simulated"


def _check_search_rows(payload: Any, ctx: _Context) -> None:
    assert {row["book_id"] for row in payload} == {"101", "102", "103"}


def _check_search_empty(payload: Any, ctx: _Context) -> None:
    assert payload == []


def _check_verify_ok(payload: Any, ctx: _Context) -> None:
    assert payload["ok"] is True
    assert payload["total"] == 1
    assert payload["corrupt"] == 0
    assert payload["errors"] == []


def _check_verify_corrupt(payload: Any, ctx: _Context) -> None:
    assert payload["ok"] is False
    assert payload["total"] == 2
    assert payload["healthy"] == 1
    assert payload["corrupt"] == 1
    assert payload["errors"]


def _check_backup_ok(payload: Any, ctx: _Context) -> None:
    assert payload["ok"] is True
    assert payload["count"] == 1
    assert payload["verified"] is True
    assert payload["verification_failed"] is False


def _check_backup_failed(payload: Any, ctx: _Context) -> None:
    assert payload["ok"] is False
    assert payload["count"] == 0
    assert payload["verification_failed"] is True
    assert payload["verification_error"]["reason"] == "simulated corruption"


def _check_export_ok(payload: Any, ctx: _Context) -> None:
    assert payload["command"] == "config export"
    assert payload["status"] == "created"
    assert Path(payload["path"]) == ctx.scratch / "export.toml"


def _check_export_failed(payload: Any, ctx: _Context) -> None:
    assert payload["status"] == "error"
    assert payload["error"]


def _check_import_ok(payload: Any, ctx: _Context) -> None:
    assert payload["command"] == "config import"
    assert payload["status"] == "imported"


def _check_import_failed(payload: Any, ctx: _Context) -> None:
    assert payload["status"] == "error"
    assert payload["error"]


def _check_music_platforms(payload: Any, ctx: _Context) -> None:
    assert payload == {"netease": "NetEase"}


def _check_music_health_ok(payload: Any, ctx: _Context) -> None:
    assert payload["failed"] == 0
    assert [row["platform"] for row in payload["platforms"]] == ["netease"]
    assert payload["platforms"][0]["ok"] is True


def _check_music_health_failed(payload: Any, ctx: _Context) -> None:
    assert payload["failed"] == 1
    assert payload["platforms"][0]["ok"] is False
    assert payload["platforms"][0]["error"] == "boom"


def _check_sources(payload: Any, ctx: _Context) -> None:
    # An empty registry is still a valid document; only row shape is fixed.
    assert all(isinstance(row.get("site_key"), str) for row in payload)


def _check_sources_failed(payload: Any, ctx: _Context) -> None:
    assert payload[0]["status_code"] == "fail"


def _check_update_available(payload: Any, ctx: _Context) -> None:
    assert payload["available"] is True
    assert payload["error"] is None
    assert payload["download_path"] is None


def _check_update_unreachable(payload: Any, ctx: _Context) -> None:
    assert payload["available"] is False
    assert payload["error"] == "unreachable"


# ---------------------------------------------------------------------------
# the matrix
# ---------------------------------------------------------------------------

#: ``--json`` invocations that must exit 0 and print the documented document.
SUCCESS_CASES: tuple[_Case, ...] = (
    _Case(
        ("doctor", "--json"),
        0,
        dict,
        prepare=_seed_workspace,
        check=_check_doctor_ok,
    ),
    _Case(
        ("search", "--site", "mocksearch", "--json", "斗破苍穹"),
        0,
        list,
        prepare=_seed_search_rule,
        check=_check_search_rows,
    ),
    _Case(
        ("storage", "verify", "--json"),
        0,
        dict,
        prepare=_seed_healthy_library,
        check=_check_verify_ok,
    ),
    _Case(
        ("storage", "backup", "--out", "{scratch}/backups", "--json"),
        0,
        dict,
        prepare=_seed_healthy_library,
        check=_check_backup_ok,
    ),
    _Case(
        ("config", "export", "--out", "{scratch}/export.toml", "--json"),
        0,
        dict,
        prepare=_prepare_export_source,
        check=_check_export_ok,
    ),
    _Case(
        ("config", "import", "--from", "{scratch}/settings.toml", "--json"),
        0,
        dict,
        prepare=_prepare_import_source,
        check=_check_import_ok,
    ),
    _Case(
        ("music", "platforms", "--json"),
        0,
        dict,
        prepare=_prepare_music_platforms,
        inproc=True,
        check=_check_music_platforms,
    ),
    _Case(
        ("music", "health", "--json"),
        0,
        dict,
        prepare=_prepare_music_health_success,
        inproc=True,
        check=_check_music_health_ok,
    ),
    _Case(
        ("sources", "list", "--json"),
        0,
        list,
        stdout_format=MULTI_LINE_DOCUMENT,
        check=_check_sources,
    ),
    _Case(
        ("update", "--json"),
        0,
        dict,
        prepare=_prepare_update_available,
        inproc=True,
        check=_check_update_available,
    ),
)

#: Misuse of the command line: exit 2, stderr only, never a JSON document.
#: Every case is offline by construction (validation happens before any I/O).
USAGE_CASES: tuple[_Case, ...] = (
    _Case(("doctor", "--json", "--port", "0"), USAGE_ERROR),
    _Case(
        ("search", "--json", "--updated-after", "2024-02-30", "keyword"),
        USAGE_ERROR,
    ),
    _Case(("storage", "verify", "--json", "--bogus"), USAGE_ERROR),
    _Case(("storage", "backup", "--json"), USAGE_ERROR),
    _Case(
        ("storage", "backup", "--out", "{scratch}/backups", "--dry-run", "--json"),
        USAGE_ERROR,
    ),
    _Case(("config", "export", "--json"), USAGE_ERROR),
    _Case(("config", "import", "--json"), USAGE_ERROR),
    _Case(("music", "platforms", "--json", "extra"), USAGE_ERROR),
    _Case(("music", "health", "--json", "extra"), USAGE_ERROR),
    _Case(
        ("sources", "list", "--json", "--probe", "--probe-timeout", "0"), USAGE_ERROR
    ),
    _Case(("update", "--json", "--bogus"), USAGE_ERROR),
    _Case(
        ("music", "health", "--json", "--platform", "nope"),
        USAGE_ERROR,
        prepare=_prepare_music_unknown_platform,
        inproc=True,
    ),
)

#: Runtime failures: exit 1 and, where the command documents one, a document.
FAILURE_CASES: tuple[_Case, ...] = (
    _Case(
        ("doctor", "--json"),
        RUNTIME_FAILURE,
        dict,
        prepare=_prepare_doctor_failure,
        inproc=True,
        check=_check_doctor_failed,
    ),
    _Case(
        ("search", "--json", "keyword"),
        RUNTIME_FAILURE,
        list,
        prepare=_prepare_search_failure,
        inproc=True,
        check=_check_search_empty,
    ),
    _Case(
        ("storage", "verify", "--json"),
        RUNTIME_FAILURE,
        dict,
        prepare=_seed_corrupt_library,
        check=_check_verify_corrupt,
    ),
    _Case(
        ("storage", "backup", "--out", "{scratch}/backups", "--json"),
        RUNTIME_FAILURE,
        dict,
        prepare=_prepare_backup_verification_failure,
        inproc=True,
        check=_check_backup_failed,
    ),
    _Case(
        ("config", "export", "--out", "{scratch}/export.toml", "--json"),
        RUNTIME_FAILURE,
        dict,
        check=_check_export_failed,
    ),
    _Case(
        ("config", "import", "--from", "{scratch}/missing.toml", "--json"),
        RUNTIME_FAILURE,
        dict,
        check=_check_import_failed,
    ),
    _Case(
        ("music", "health", "--json"),
        RUNTIME_FAILURE,
        dict,
        prepare=_prepare_music_health_failure,
        inproc=True,
        check=_check_music_health_failed,
    ),
    _Case(
        ("sources", "list", "--check", "--json"),
        RUNTIME_FAILURE,
        list,
        stdout_format=MULTI_LINE_DOCUMENT,
        prepare=_prepare_sources_check_failure,
        inproc=True,
        check=_check_sources_failed,
    ),
    _Case(
        ("update", "--json", "--yes"),
        RUNTIME_FAILURE,
        dict,
        prepare=_prepare_update_unreachable,
        inproc=True,
        check=_check_update_unreachable,
    ),
)


def _param(case: _Case) -> Any:
    """Give each table entry a readable id derived from its argv."""
    return pytest.param(
        case, id="-".join(part for part in case.argv if not part.startswith("--"))
    )


@pytest.mark.parametrize("case", [_param(case) for case in SUCCESS_CASES])
def test_json_success_contract(
    case: _Case,
    ctx: _Context,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every successful ``--json`` run prints its document and exits 0."""
    _assert_case(case, ctx, monkeypatch, capsys)


@pytest.mark.parametrize("case", [_param(case) for case in USAGE_CASES])
def test_json_usage_error_contract(
    case: _Case,
    ctx: _Context,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A usage error exits 2 and never mixes a partial document into stdout."""
    _assert_case(case, ctx, monkeypatch, capsys)


@pytest.mark.parametrize("case", [_param(case) for case in FAILURE_CASES])
def test_json_runtime_failure_contract(
    case: _Case,
    ctx: _Context,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A runtime failure exits 1 and still emits the promised document."""
    _assert_case(case, ctx, monkeypatch, capsys)


def test_sources_list_is_the_only_documented_multiline_json_document() -> None:
    """Record the one covered command whose documented format is multi-line.

    The output-encoding contract names ``sources list`` among the commands
    that use ``indent=2``; changing it to one line would change a documented
    user-visible contract.  The matrix therefore accepts (and locks) "one
    valid JSON document" rather than "one line" for it.  If the format is
    unified later, update the docs and flip this case to ``SINGLE_LINE``.
    """
    multi_line = {
        " ".join(case.argv[:2])
        for case in (*SUCCESS_CASES, *FAILURE_CASES)
        if case.stdout_format == MULTI_LINE_DOCUMENT
    }
    assert multi_line == {"sources list"}


@pytest.mark.xfail(
    strict=True,
    reason=(
        "documented contract: sources list --json emits indent=2 multi-line "
        "JSON; forcing one line would change the success contract"
    ),
)
def test_sources_list_json_meets_the_uniform_single_line_rule(
    ctx: _Context,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Pin the deviation: if this starts passing, update docs and the table.

    Strict xfail, so a future compaction of the document turns into an XPASS
    failure instead of silently leaving the matrix and the prose disagreeing.
    """
    case = next(
        case
        for case in SUCCESS_CASES
        if case.argv[:2] == ("sources", "list")
        and case.stdout_format == MULTI_LINE_DOCUMENT
    )
    code, stdout, stderr = _invoke(case, ctx, monkeypatch, capsys)
    assert code == 0
    _assert_clean_streams(stdout, stderr)
    _one_line_json(stdout)
