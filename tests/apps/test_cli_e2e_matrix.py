#!/usr/bin/env python3
"""
tests.apps.test_cli_e2e_matrix
------------------------------

Cross-command end-to-end regression for the CLI surfaces added in v1.32.

Unlike the focused unit modules (``test_cli_search``,
``test_cli_doctor_fix``, ``test_cli_storage_backup``, ...), every check here
goes through the real entry point and the real filesystem:

* ``search`` talks to a local mock HTTP site through an installed rule source,
  so the whole chain (rule registration -> searcher -> ranking -> local
  filters -> ``--json`` document) runs offline;
* ``doctor --fix`` and ``storage backup/verify`` run in a temporary working
  directory with a real ``settings.toml`` and a real SQLite chapter database;
* ``completion`` emits the actual Fish script;
* ``music download --json`` and ``update --json`` are driven in-process
  because their engine/manifest is replaced with a mock (a monkeypatch cannot
  cross a process boundary), but still through ``cli_main`` and a real stdout.

Every test asserts the contracts the matrix cares about: exit code, exactly
one JSON line on stdout, diagnostics on stderr, and no leftover
``.tmp``/``.part``/temporary artifacts.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader.apps.cli import main as cli_main_module
from novel_downloader.apps.cli.commands import music
from novel_downloader.apps.cli.commands import update as update_module
from novel_downloader.apps.common.updater import ReleaseAsset, UpdateCheck, UpdateInfo
from novel_downloader.infra.persistence.chapter_storage import ChapterStorage
from tests.apps.test_cli_e2e import (
    _bindable_port,
    _seed_config,
    run_cli,
    run_cli_inproc,
)

#: Books served by the mock search site: (id, title, author, words, updated).
MOCK_BOOKS: tuple[tuple[str, str, str, str, str], ...] = (
    ("101", "斗破苍穹", "天蚕土豆", "30万字", "2026-01-01"),
    ("102", "斗破苍穹 外传", "天蚕土豆", "120万字", "2020-05-01"),
    ("103", "斗破苍穹 番外", "某作者", "-", ""),
)

#: Rule document template; ``{base}`` is replaced with the mock server URL.
MOCK_RULE: dict[str, Any] = {
    "site_key": "mocksearch",
    "site_name": "Mock Search Site",
    "base_url": "{base}",
    "hosts": ["127.0.0.1"],
    "search": {
        "url": "{base}/search?q={keyword}",
        "method": "GET",
        "item": "div.item",
        "priority": 10,
        "book_id_regex": r"/book/(\d+)/",
        "fields": {
            "title": {"selector": "a.title", "attr": "text"},
            "url": {"selector": "a.title", "attr": "href"},
            "author": {"selector": "span.author", "attr": "text"},
            "word": {"selector": "span.words", "attr": "text"},
            "update": {"selector": "span.update", "attr": "text"},
        },
    },
    "chapter": {
        "url": "{base}/book/{book_id}/{chapter_id}.html",
        "content": {"selector": "div#content"},
    },
}


def _mock_page() -> bytes:
    """Render the search result page for :data:`MOCK_BOOKS`."""
    rows = "\n".join(
        (
            '<div class="item">'
            f'<a class="title" href="/book/{book_id}/">{title}</a>'
            f'<span class="author">{author}</span>'
            f'<span class="words">{words}</span>'
            f'<span class="update">{updated}</span>'
            "</div>"
        )
        for book_id, title, author, words, updated in MOCK_BOOKS
    )
    return f"<html><body>{rows}</body></html>".encode()


class _MockSiteHandler(BaseHTTPRequestHandler):
    """Answer every ``/search`` request with the fixed result page."""

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler hook
        if not self.path.startswith("/search"):
            self.send_error(404)
            return
        body = _mock_page()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        """Keep the test output free of per-request access logs."""


@pytest.fixture
def mock_site() -> Iterator[str]:
    """Run the mock search site on a free loopback port."""
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


def _install_mock_rule(work: Path, base_url: str) -> Path:
    """Install the mock rule in ``work/rules`` (a scanned rule directory)."""
    rules = work / "rules"
    rules.mkdir()
    rule = json.loads(json.dumps(MOCK_RULE).replace("{base}", base_url))
    path = rules / "mocksearch.json"
    path.write_text(json.dumps(rule, ensure_ascii=False), encoding="utf-8")
    return path


def _one_line_json(out: str) -> Any:
    """Assert stdout is exactly one JSON line and return the decoded value."""
    assert out.endswith("\n"), f"JSON document is not newline-terminated: {out!r}"
    assert out.count("\n") == 1, f"JSON document spans lines: {out!r}"
    return json.loads(out)


def _residual_files(root: Path, *, ignored_roots: set[str]) -> set[Path]:
    """Return files under ``root`` outside the caller-declared expected trees."""
    found: set[Path] = set()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] in ignored_roots:
            continue
        found.add(relative)
    return found


# ---------------------------------------------------------------------------
# search: local rule source + local filters + --json contract
# ---------------------------------------------------------------------------


def test_search_filters_run_end_to_end(tmp_path: Path, mock_site: str) -> None:
    """Filters are applied by the real aggregate search, not only in argparse."""
    work = tmp_path / "work"
    home = tmp_path / "home"
    work.mkdir()
    _seed_config(work)
    _install_mock_rule(work, mock_site)
    keyword = "斗破苍穹"

    unfiltered = run_cli(
        "search", "--site", "mocksearch", "--json", keyword, cwd=work, home=home
    )
    assert unfiltered.returncode == 0, unfiltered.stderr
    rows = _one_line_json(unfiltered.stdout)
    assert {row["book_id"] for row in rows} == {"101", "102", "103"}
    assert "1 site(s) completed / 0 failed / 0 skipped" in unfiltered.stderr
    # Human diagnostics never leak into the machine-readable stream.
    assert "completed" not in unfiltered.stdout

    short = run_cli(
        "search",
        "--site",
        "mocksearch",
        "--json",
        "--min-words",
        "500000",
        keyword,
        cwd=work,
        home=home,
    )
    assert short.returncode == 0, short.stderr
    assert [row["book_id"] for row in _one_line_json(short.stdout)] == ["102"]

    fresh = run_cli(
        "search",
        "--site",
        "mocksearch",
        "--json",
        "--max-words",
        "500000",
        "--updated-after",
        "2024-01-01",
        keyword,
        cwd=work,
        home=home,
    )
    assert fresh.returncode == 0, fresh.stderr
    fresh_rows = _one_line_json(fresh.stdout)
    assert [row["book_id"] for row in fresh_rows] == ["101"]
    # The raw display label survives; only the filter parsed it.
    assert fresh_rows[0]["word_count"] == "30万字"

    # A filter that drops every row is still a successful search: the site
    # answered, so the empty array is the document and the reason is on stderr.
    empty = run_cli(
        "search",
        "--site",
        "mocksearch",
        "--json",
        "--updated-after",
        "2030-01-01",
        keyword,
        cwd=work,
        home=home,
    )
    assert empty.returncode == 0, empty.stderr
    assert _one_line_json(empty.stdout) == []
    assert "No results found" in empty.stderr

    # Usage errors never emit a document and keep the documented exit code.
    bad_date = run_cli(
        "search",
        "--json",
        "--updated-after",
        "2024-02-30",
        keyword,
        cwd=work,
        home=home,
    )
    assert bad_date.returncode == 2
    assert bad_date.stdout == ""
    assert "Invalid --updated-after date" in bad_date.stderr

    bad_bound = run_cli(
        "search", "--json", "--min-words", "-1", keyword, cwd=work, home=home
    )
    assert bad_bound.returncode == 2
    assert bad_bound.stdout == ""
    assert "must be between 0 and" in bad_bound.stderr

    # Only the configured log tree may appear; no temp/partial artifacts.
    leftovers = _residual_files(work, ignored_roots={"settings.toml", "rules", "logs"})
    assert leftovers == set()
    assert not list(work.rglob("*.part"))
    assert not list(work.rglob("*.tmp"))


# ---------------------------------------------------------------------------
# doctor: --fix --dry-run --json and --fix
# ---------------------------------------------------------------------------


def _write_aged(path: Path, *, mtime: float, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    os.utime(path, (mtime, mtime))
    return path


def test_doctor_fix_preview_then_apply(tmp_path: Path) -> None:
    """The preview changes nothing; the real fix repairs exactly the plan."""
    work = tmp_path / "work"
    home = tmp_path / "home"
    work.mkdir()
    home.mkdir()
    _seed_config(work)
    port = _bindable_port()
    ten_days_ago = 1_700_000_000.0 - 10 * 86400

    cache = home / "cache"
    old_tmp = _write_aged(cache / "old.tmp", mtime=ten_days_ago, payload=b"old")
    fresh_tmp = _write_aged(cache / "fresh.tmp", mtime=time.time(), payload=b"new")
    keep = _write_aged(cache / "keep.txt", mtime=ten_days_ago, payload=b"keep")

    dry = run_cli(
        "doctor",
        "--port",
        str(port),
        "--fix",
        "--dry-run",
        "--json",
        cwd=work,
        home=home,
    )
    assert dry.returncode == 0, dry.stderr
    assert dry.stderr == ""
    dry_doc = _one_line_json(dry.stdout)
    assert dry_doc["dry_run"] is True
    assert dry_doc["fixed"] == []
    removed = [row for row in dry_doc["skipped"] if row["action"] == "remove_file"]
    assert len(removed) == 1
    assert removed[0]["reason"] == "dry_run"
    assert removed[0]["path"].endswith("old.tmp")
    created = [row for row in dry_doc["skipped"] if row["action"] == "create_dir"]
    assert len(created) == 2
    # A preview must not create or delete anything.
    assert not (work / "downloads").exists()
    assert not (work / "logs").exists()
    assert old_tmp.exists()
    assert fresh_tmp.exists()
    assert keep.exists()

    applied = run_cli(
        "doctor", "--port", str(port), "--fix", "--json", cwd=work, home=home
    )
    assert applied.returncode == 0, applied.stderr
    assert applied.stderr == ""
    fix_doc = _one_line_json(applied.stdout)
    assert fix_doc["dry_run"] is False
    assert fix_doc["errors"] == []
    actions = [row["action"] for row in fix_doc["fixed"]]
    assert actions.count("create_dir") == 2
    assert actions.count("remove_file") == 1
    assert (work / "downloads").is_dir()
    assert (work / "logs").is_dir()
    assert not old_tmp.exists()
    assert fresh_tmp.exists()
    assert keep.exists()
    assert not list(work.rglob(".doctor-*"))
    assert not list(home.rglob(".doctor-*"))

    bad_age = run_cli(
        "doctor", "--fix", "--fix-age-days", "0", "--json", cwd=work, home=home
    )
    assert bad_age.returncode == 2
    assert bad_age.stdout == ""
    assert "at least 1 day" in bad_age.stderr


# ---------------------------------------------------------------------------
# storage: backup --keep --dry-run --json, real prune, verify --json
# ---------------------------------------------------------------------------


def _seed_chapter_db(root: Path, chapters: int = 2) -> Path:
    """Create ``raw_data/acme/book1/chapter.raw.sqlite`` with real rows."""
    book = root / "raw_data" / "acme" / "book1"
    book.mkdir(parents=True, exist_ok=True)
    with ChapterStorage(book, "chapter.raw.sqlite") as store:
        store.upsert_chapters(
            [
                {
                    "id": f"c{index}",
                    "title": f"Chapter {index}",
                    "content": f"Body {index}",
                    "extra": {},
                }
                for index in range(1, chapters + 1)
            ]
        )
    return book / "chapter.raw.sqlite"


def _seed_old_backups(source: Path, out: Path) -> list[Path]:
    """Copy ``source`` into ``out`` under three old fixed timestamps."""
    book = out / "acme" / "book1"
    book.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for stamp in ("20200101T000000Z", "20210101T000000Z", "20220101T000000Z"):
        target = book / f"chapter.raw.{stamp}.sqlite"
        shutil.copy2(source, target)
        paths.append(target)
    return paths


def test_storage_backup_retention_and_verify(tmp_path: Path) -> None:
    """Retention previews, then prunes; verify reports corruption read-only."""
    work = tmp_path / "work"
    home = tmp_path / "home"
    work.mkdir()
    _seed_config(work)
    db = _seed_chapter_db(work)

    # --keep --dry-run: the fresh backup is written, old ones are only listed.
    preview_out = work / "preview-backups"
    old = _seed_old_backups(db, preview_out)
    preview = run_cli(
        "storage",
        "backup",
        "--out",
        str(preview_out),
        "--keep",
        "2",
        "--dry-run",
        "--json",
        cwd=work,
        home=home,
    )
    assert preview.returncode == 0, preview.stderr
    assert preview.stderr == ""
    preview_doc = _one_line_json(preview.stdout)
    assert preview_doc["command"] == "storage backup"
    assert preview_doc["count"] == 1
    retention = preview_doc["retention"]
    assert retention["keep"] == 2
    assert retention["dry_run"] is True
    assert retention["removed"] == []
    assert {Path(path).name for path in retention["candidates"]} == {
        "chapter.raw.20200101T000000Z.sqlite",
        "chapter.raw.20210101T000000Z.sqlite",
    }
    assert all(path.exists() for path in old)
    assert len(list(preview_out.rglob("chapter.raw.*.sqlite"))) == 4

    # A separate directory keeps the second run out of the same-second guard.
    apply_out = work / "apply-backups"
    _seed_old_backups(db, apply_out)
    applied = run_cli(
        "storage",
        "backup",
        "--out",
        str(apply_out),
        "--keep",
        "2",
        "--json",
        cwd=work,
        home=home,
    )
    assert applied.returncode == 0, applied.stderr
    assert applied.stderr == ""
    applied_doc = _one_line_json(applied.stdout)
    assert applied_doc["count"] == 1
    entry = applied_doc["files"][0]
    assert entry["chapters"] == 2
    assert len(entry["sha256"]) == 64
    assert {Path(path).name for path in applied_doc["retention"]["removed"]} == {
        "chapter.raw.20200101T000000Z.sqlite",
        "chapter.raw.20210101T000000Z.sqlite",
    }
    remaining = sorted(p.name for p in apply_out.rglob("chapter.raw.*.sqlite"))
    assert len(remaining) == 2
    assert "chapter.raw.20220101T000000Z.sqlite" in remaining

    # verify --json: one healthy database, then a corrupt one, read-only.
    healthy = run_cli("storage", "verify", "--json", cwd=work, home=home)
    assert healthy.returncode == 0, healthy.stderr
    assert healthy.stderr == ""
    healthy_doc = _one_line_json(healthy.stdout)
    assert healthy_doc == {
        "ok": True,
        "total": 1,
        "healthy": 1,
        "corrupt": 0,
        "errors": [],
    }

    corrupt_bytes = b"not a sqlite database"
    db.write_bytes(corrupt_bytes)
    before_names = {path.name for path in db.parent.iterdir()}
    corrupt = run_cli("storage", "verify", "--json", cwd=work, home=home)
    assert corrupt.returncode == 1
    assert corrupt.stderr == ""
    corrupt_doc = _one_line_json(corrupt.stdout)
    assert corrupt_doc["ok"] is False
    assert corrupt_doc["corrupt"] == 1
    assert corrupt_doc["healthy"] == 0
    assert len(corrupt_doc["errors"]) == 1
    assert corrupt_doc["errors"][0]["path"].endswith("chapter.raw.sqlite")
    assert corrupt_doc["errors"][0]["reason"]

    # Verification must not repair, copy, rename or delete the damaged file.
    assert db.read_bytes() == corrupt_bytes
    assert {path.name for path in db.parent.iterdir()} == before_names
    assert not list(work.rglob("*.corrupt-*"))
    assert not list(work.rglob("*.pre-restore-*"))

    # Usage guards.
    bad_keep = run_cli(
        "storage",
        "backup",
        "--out",
        str(work / "bad"),
        "--keep",
        "-1",
        "--json",
        cwd=work,
        home=home,
    )
    assert bad_keep.returncode == 2
    assert bad_keep.stdout == ""
    no_keep = run_cli(
        "storage",
        "backup",
        "--out",
        str(work / "bad"),
        "--dry-run",
        "--json",
        cwd=work,
        home=home,
    )
    assert no_keep.returncode == 2
    assert no_keep.stdout == ""
    assert "--dry-run requires --keep" in no_keep.stderr


# ---------------------------------------------------------------------------
# music download --json: lyric reporting through cli_main
# ---------------------------------------------------------------------------


class _FakeMusicProvider:
    """Provider stub returning a fixed, offline result list."""

    def __init__(self, tracks: list[Any]) -> None:
        self.tracks = tracks

    def search(self, keyword: str, *, limit: int) -> tuple[list[Any], int]:
        return list(self.tracks)[:limit], len(self.tracks)

    def is_logged_in(self) -> bool:
        return True


class _FakeMusicEngine:
    """Minimal music engine that writes real files and reports results."""

    login_expired = RuntimeError

    def __init__(self, out_dir: Path, tracks: list[Any]) -> None:
        self.out_dir = out_dir
        self.tracks = tracks
        self.settings = _FakeMusicSettings(out_dir)

    def labels(self) -> list[tuple[str, str]]:
        return [("netease", "NetEase")]

    def platform_keys(self) -> list[str]:
        return ["netease"]

    def platform_note(self, _platform: str) -> None:
        return None

    def create(self, _platform: str) -> _FakeMusicProvider:
        return _FakeMusicProvider(self.tracks)

    def default_download_dir(self) -> str:
        return str(self.out_dir)

    def download_options(self, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(**kwargs)

    def download_many(
        self,
        _provider: Any,
        tracks: list[Any],
        _options: Any,
        *,
        workers: int,
        on_tick: Any,
        on_result: Any,
    ) -> tuple[int, int, int, list[Any], list[Any]]:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        for index, track in enumerate(tracks):
            audio = self.out_dir / f"{track.name} - {track.artists}.mp3"
            audio.write_bytes(b"AUDIO")
            if index == 0:
                audio.with_suffix(".lrc").write_text("[00:00.00]la\n", encoding="utf-8")
            track.path = str(audio)
            on_result(
                index,
                track,
                SimpleNamespace(status="ok", path=str(audio), reason=""),
            )
        return len(tracks), 0, 0, [], []


class _FakeMusicSettings:
    """Return deterministic music settings for :class:`_FakeMusicEngine`."""

    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir

    def load_settings(self) -> dict[str, Any]:
        return {"out_dir": str(self.out_dir), "level": "standard", "workers": 1}


def _music_tracks() -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            id=f"id{index}",
            name=f"Song {index}",
            artists="Artist",
            album="Album",
            duration_s=180 + index,
            vip=False,
            provider="netease",
            path="",
        )
        for index in range(1, 3)
    ]


def test_music_download_json_reports_written_lyrics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``lyric_path``/``lyrics_written`` reflect files that really exist."""
    out_dir = tmp_path / "music"
    engine = _FakeMusicEngine(out_dir, _music_tracks())
    monkeypatch.setattr(music, "_require_engine", lambda: engine)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "novel-cli",
            "music",
            "download",
            "--platform",
            "netease",
            "--json",
            "--pick",
            "1,2",
            "song",
        ],
    )

    code = 0
    try:
        cli_main_module.cli_main()
    except SystemExit as exc:
        code = 0 if exc.code is None else int(exc.code)
    assert code == 0

    captured = capsys.readouterr()
    payload = _one_line_json(captured.out)
    assert payload["total"] == 2
    assert payload["ok"] == 2
    assert payload["fail"] == 0
    assert payload["lyrics_written"] == 1
    first, second = payload["results"]
    assert first["status"] == "ok"
    assert first["path"].endswith("Song 1 - Artist.mp3")
    assert first["lyric_path"] == str(out_dir / "Song 1 - Artist.lrc")
    assert Path(first["lyric_path"]).is_file()
    assert second["status"] == "ok"
    assert "lyric_path" not in second
    # Human progress and tables stay on stderr.
    assert "Found 2 result(s)" in captured.err
    assert "Done: 2 succeeded" in captured.err
    assert "Found 2 result(s)" not in captured.out
    assert sorted(path.name for path in out_dir.iterdir()) == [
        "Song 1 - Artist.lrc",
        "Song 1 - Artist.mp3",
        "Song 2 - Artist.mp3",
    ]
    assert not list(out_dir.rglob("*.part"))
    assert not list(out_dir.rglob("*.tmp"))


def test_music_download_json_empty_search_still_emits_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An empty result set is a successful run with a parseable document."""
    engine = _FakeMusicEngine(tmp_path / "music", [])
    monkeypatch.setattr(music, "_require_engine", lambda: engine)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "novel-cli",
            "music",
            "download",
            "--platform",
            "netease",
            "--json",
            "--yes",
            "nothing",
        ],
    )

    code = 0
    try:
        cli_main_module.cli_main()
    except SystemExit as exc:
        code = 0 if exc.code is None else int(exc.code)
    assert code == 0

    captured = capsys.readouterr()
    payload = _one_line_json(captured.out)
    assert payload["total"] == 0
    assert payload["lyrics_written"] == 0
    assert payload["results"] == []
    assert "No results found" in captured.err
    assert not (tmp_path / "music").exists()


# ---------------------------------------------------------------------------
# completion: the emitted Fish script covers the new surface
# ---------------------------------------------------------------------------


def _fish_branch_words(script: str, path: str) -> list[str]:
    """Return the words printed by the Fish branch for ``path``."""
    lines = script.splitlines()
    labels = {
        f"""if test "$path" = '{path}'""",
        f"""else if test "$path" = '{path}'""",
    }
    for index, line in enumerate(lines):
        if line.strip() not in labels:
            continue
        body = lines[index + 1].strip()
        prefix = "printf '%s\\n' "
        assert body.startswith(prefix), body
        return [token.strip("'") for token in body[len(prefix) :].split(" ")]
    raise AssertionError(f"no Fish branch for {path!r}")


def test_fish_completion_includes_storage_verify_and_search_filters(
    tmp_path: Path,
) -> None:
    """The generated script offers the new filters where they belong."""
    work = tmp_path / "work"
    home = tmp_path / "home"
    work.mkdir()
    _seed_config(work)

    first = run_cli("completion", "--shell", "fish", cwd=work, home=home)
    assert first.returncode == 0, first.stderr
    assert first.stderr == ""
    assert "\r" not in first.stdout
    assert "complete -c novel-cli" in first.stdout

    search_words = _fish_branch_words(first.stdout, " search")
    assert {"--min-words", "--max-words", "--updated-after"} <= set(search_words)
    verify_words = _fish_branch_words(first.stdout, " storage verify")
    assert "--json" in verify_words

    second = run_cli("completion", "--shell", "fish", cwd=work, home=home)
    assert second.returncode == 0, second.stderr
    assert second.stdout == first.stdout
    assert (
        _residual_files(work, ignored_roots={"settings.toml", "rules", "logs"}) == set()
    )

    # A real Fish, when installed, must accept the generated source bytes.
    fish = shutil.which("fish")
    if fish is not None:
        script = tmp_path / "completion.fish"
        script.write_bytes(first.stdout.encode("utf-8"))
        checked = subprocess.run(
            [fish, "-n", str(script)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert checked.returncode == 0, checked.stderr


# ---------------------------------------------------------------------------
# update --json: no CLI asset for this platform
# ---------------------------------------------------------------------------


def _desktop_asset() -> ReleaseAsset:
    return ReleaseAsset(
        name="FanqieNovelDownloader-tauri-windows-x64-portable.exe",
        url="https://example.invalid/desktop.exe",
        size=123,
        sha256="0" * 64,
    )


def _no_cli_asset_info() -> UpdateInfo:
    """Release whose selected asset fell back to the desktop installer.

    ``_check`` asks the API/manifest for a CLI shape, but the shared selector
    may fall back to another shape; ``_cli_asset`` must then discard it.  The
    installer is therefore set as ``asset`` on purpose.
    """
    installer = _desktop_asset()
    return UpdateInfo(
        current="1.0.0",
        latest="9.9.9",
        available=True,
        notes_url="https://example.invalid/notes",
        asset=installer,
        assets=(installer,),
    )


def _patch_check(
    monkeypatch: pytest.MonkeyPatch,
    info: UpdateInfo | None,
    *,
    error: str | None = None,
) -> None:
    """Replace the release fetch, keeping ``_check``'s asset guard in play."""

    def _fake(*_args: object, **_kwargs: object) -> UpdateCheck:
        return UpdateCheck(info=info, error=error, checked_at=0.0)

    monkeypatch.setattr(update_module, "check_for_update", _fake)


def test_update_json_no_cli_asset_is_one_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--json --yes`` reports ``no-cli-asset`` and exits 1, on one line."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    _patch_check(monkeypatch, _no_cli_asset_info())

    code = run_cli_inproc(
        monkeypatch, ["update", "--json", "--yes"], cwd=tmp_path, home=home
    )
    assert code == 1

    captured = capsys.readouterr()
    payload = _one_line_json(captured.out)
    assert payload["available"] is True
    assert payload["error"] == update_module.NO_CLI_ASSET_ERROR
    assert payload["asset_name"] is None
    assert payload["asset_shape"] is None
    assert payload["download_path"] is None
    # Machine mode keeps the document self-contained: no extra stderr chatter.
    assert captured.err == ""
    assert not (home / "updates").exists()


def test_update_json_check_only_reports_no_asset_without_downloading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--json`` alone behaves like ``--check`` and still exits 0."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    _patch_check(monkeypatch, _no_cli_asset_info())

    code = run_cli_inproc(monkeypatch, ["update", "--json"], cwd=tmp_path, home=home)
    assert code == 0

    captured = capsys.readouterr()
    payload = _one_line_json(captured.out)
    assert payload["error"] is None
    assert payload["asset_shape"] is None
    assert payload["download_path"] is None
    assert captured.err == ""
    assert not (home / "updates").exists()


def test_update_json_check_failure_is_one_line_and_exits_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failed check still produces the documented document and exit code."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NOVEL_HOME", str(home))
    _patch_check(monkeypatch, None, error="unreachable")

    code = run_cli_inproc(
        monkeypatch, ["update", "--json", "--yes"], cwd=tmp_path, home=home
    )
    assert code == 1

    captured = capsys.readouterr()
    payload = _one_line_json(captured.out)
    assert payload["error"] == "unreachable"
    assert payload["available"] is False
    assert captured.err == ""
