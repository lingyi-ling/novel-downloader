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
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
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
    CLI_MODULE,
    _bindable_port,
    _child_env,
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


def _mock_page(
    books: tuple[tuple[str, str, str, str, str], ...] = MOCK_BOOKS,
) -> bytes:
    """Render the search result page for ``books``."""
    rows = "\n".join(
        (
            '<div class="item">'
            f'<a class="title" href="/book/{book_id}/">{title}</a>'
            f'<span class="author">{author}</span>'
            f'<span class="words">{words}</span>'
            f'<span class="update">{updated}</span>'
            "</div>"
        )
        for book_id, title, author, words, updated in books
    )
    return f"<html><body>{rows}</body></html>".encode()


class _MockSiteHandler(BaseHTTPRequestHandler):
    """Answer every ``/search`` request with the class's fixed result page."""

    #: Books served by this handler; subclasses swap in a richer corpus.
    books: tuple[tuple[str, str, str, str, str], ...] = MOCK_BOOKS

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler hook
        if not self.path.startswith("/search"):
            self.send_error(404)
            return
        body = _mock_page(self.books)
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


# ---------------------------------------------------------------------------
# search ranking matrix (v1.35/v1.36): intent, telemetry, sort/order, filters
# ---------------------------------------------------------------------------

#: Corpus that exercises every ranking signal.  Row 204 is the title/author
#: collision: its *title* is row 201's author, which is exactly the case the
#: intent-aware ordering has to keep below that author's own books.
MATRIX_BOOKS: tuple[tuple[str, str, str, str, str], ...] = (
    ("201", "斗破苍穹", "天蚕土豆", "30万字", "2026-01-01"),
    ("202", "斗破苍穹 外传", "天蚕土豆", "120万字", "2020-05-01"),
    ("203", "斗破苍穹 番外", "某作者", "-", ""),
    ("204", "天蚕土豆", "另一位", "5万字", "2023-03-03"),
)


def _handler_serving(
    books: tuple[tuple[str, str, str, str, str], ...],
) -> type[_MockSiteHandler]:
    """Return a handler class that serves ``books`` on every search request."""

    class _Handler(_MockSiteHandler):
        pass

    _Handler.books = books
    return _Handler


@contextmanager
def _serving_books(
    books: tuple[tuple[str, str, str, str, str], ...],
) -> Iterator[str]:
    """Run ``books`` on a free loopback port for the duration of the block."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_serving(books))
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
def ranking_site() -> Iterator[str]:
    """Serve :data:`MATRIX_BOOKS` through the same rule the other tests use."""
    with _serving_books(MATRIX_BOOKS) as base:
        yield base


def _install_named_rule(
    work: Path,
    base_url: str,
    *,
    site_key: str,
    priority: int,
) -> Path:
    """Install :data:`MOCK_RULE` under ``site_key`` with a distinct priority."""
    rules = work / "rules"
    rules.mkdir(exist_ok=True)
    rule = json.loads(json.dumps(MOCK_RULE).replace("{base}", base_url))
    rule["site_key"] = site_key
    rule["site_name"] = f"Mock {site_key}"
    rule["search"]["priority"] = priority
    path = rules / f"{site_key}.json"
    path.write_text(json.dumps(rule, ensure_ascii=False), encoding="utf-8")
    return path


def _ranking_work(
    tmp_path: Path,
    base_url: str,
    *,
    sites: tuple[tuple[str, int], ...] = (("mocksearch", 10),),
) -> tuple[Path, Path]:
    """Seed an isolated workdir/home with one rule per requested site."""
    work = tmp_path / "work"
    home = tmp_path / "home"
    work.mkdir()
    home.mkdir()
    _seed_config(work)
    for site_key, priority in sites:
        _install_named_rule(work, base_url, site_key=site_key, priority=priority)
    return work, home


def _search_json(*options: str, keyword: str, cwd: Path, home: Path) -> list[Any]:
    """Run ``search --json`` and return the single-line decoded document."""
    result = run_cli(
        "search",
        "--json",
        "--site-limit",
        "20",
        *options,
        keyword,
        cwd=cwd,
        home=home,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("\n") == 1, result.stdout
    return _one_line_json(result.stdout)


def _run_cli_closed_stdin(
    *args: str, cwd: Path, home: Path
) -> subprocess.CompletedProcess[str]:
    """Run the CLI with stdin closed, so the prompt cancels after the table."""
    return subprocess.run(
        [sys.executable, "-m", CLI_MODULE, *args],
        capture_output=True,
        cwd=str(cwd),
        env=_child_env(home),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        timeout=240,
        check=False,
    )


def _assert_telemetry(rows: list[Any]) -> None:
    """Assert every row carries a usable ``rank_score`` and ``match_tier``."""
    for row in rows:
        score = row["rank_score"]
        tier = row["match_tier"]
        assert not isinstance(score, bool)
        assert isinstance(score, (int, float))
        assert 0.0 <= score <= 1.0
        assert not isinstance(tier, bool)
        assert isinstance(tier, int)
        assert 0 <= tier <= 4


def test_search_json_intent_and_match_telemetry(
    tmp_path: Path, ranking_site: str
) -> None:
    """A title search and an author search each explain their own ordering."""
    work, home = _ranking_work(tmp_path, ranking_site)

    title_rows = _search_json(
        "--site", "mocksearch", keyword="斗破苍穹", cwd=work, home=home
    )
    _assert_telemetry(title_rows)
    title_ids = [row["book_id"] for row in title_rows]
    assert title_ids[0] == "201"
    assert set(title_ids[1:]) == {"202", "203"}
    assert {row["query_intent"] for row in title_rows} == {"title"}
    assert title_rows[0]["title"] == "斗破苍穹"
    assert title_rows[0]["match_tier"] == 4
    assert {row["match_tier"] for row in title_rows[1:]} == {3}
    # Row 204 collides on neither field; the default relevance floor drops it.
    assert "204" not in title_ids

    author_rows = _search_json(
        "--site", "mocksearch", keyword="天蚕土豆", cwd=work, home=home
    )
    _assert_telemetry(author_rows)
    author_ids = [row["book_id"] for row in author_rows]
    assert set(author_ids[:2]) == {"201", "202"}
    assert author_ids[-1] == "204"
    assert {row["query_intent"] for row in author_rows} == {"author"}
    assert [row["match_tier"] for row in author_rows[:2]] == [4, 4]
    # The title-only collision survives the relevance floor but ranks last:
    # the author field is the primary one under an author verdict.
    assert author_rows[-1]["title"] == "天蚕土豆"
    assert author_rows[-1]["match_tier"] == 0
    assert "203" not in author_ids

    # The tier is the outer key, so scores within one tier descend.
    for rows in (title_rows, author_rows):
        for left, right in zip(rows, rows[1:], strict=False):
            if left["match_tier"] == right["match_tier"]:
                assert left["rank_score"] >= right["rank_score"]

    # Non-quality sort keys ignore intent entirely, but the row telemetry the
    # badge renders is still attached.
    word_rows = _search_json(
        "--site",
        "mocksearch",
        "--sort",
        "words",
        keyword="斗破苍穹",
        cwd=work,
        home=home,
    )
    assert [row["book_id"] for row in word_rows] == ["202", "201", "204", "203"]
    assert {row["query_intent"] for row in word_rows} == {None}
    _assert_telemetry(word_rows)


#: ``(options, expected book ids)`` for every sort key and both directions.
SORT_ORDER_CASES: tuple[tuple[tuple[str, ...], list[str]], ...] = (
    ((), ["201", "202", "203"]),
    (("--sort", "quality", "--order", "desc"), ["201", "202", "203"]),
    (("--sort", "priority"), ["201", "202", "203", "204"]),
    (("--sort", "priority", "--order", "desc"), ["204", "203", "202", "201"]),
    (("--sort", "site"), ["204", "201", "202", "203"]),
    (("--sort", "title"), ["204", "201", "202", "203"]),
    (("--sort", "title", "--order", "desc"), ["203", "202", "201", "204"]),
    (("--sort", "author"), ["204", "201", "202", "203"]),
    (("--sort", "words"), ["202", "201", "204", "203"]),
    (("--sort", "words", "--order", "asc"), ["203", "204", "201", "202"]),
    (("--sort", "update"), ["201", "204", "202", "203"]),
    (("--sort", "update", "--order", "asc"), ["203", "202", "204", "201"]),
)


def test_search_sort_key_and_direction_matrix(
    tmp_path: Path, ranking_site: str
) -> None:
    """Every ``--sort`` key and explicit ``--order`` reach the real ordering."""
    work, home = _ranking_work(tmp_path, ranking_site)

    for options, expected in SORT_ORDER_CASES:
        rows = _search_json(
            "--site", "mocksearch", *options, keyword="斗破苍穹", cwd=work, home=home
        )
        assert [row["book_id"] for row in rows] == expected, options
        _assert_telemetry(rows)


def test_search_filter_combinations_and_limit_order(
    tmp_path: Path, ranking_site: str
) -> None:
    """Combined local filters intersect, and filtering runs before ``limit``."""
    work, home = _ranking_work(tmp_path, ranking_site)

    combined = _search_json(
        "--site",
        "mocksearch",
        "--min-words",
        "300000",
        "--max-words",
        "1200000",
        "--updated-after",
        "2020-01-01",
        keyword="斗破苍穹",
        cwd=work,
        home=home,
    )
    assert [row["book_id"] for row in combined] == ["201", "202"]
    # Only the filter parsed the display label; it is still the raw site text.
    assert {row["word_count"] for row in combined} == {"30万字", "120万字"}

    # Both bounds are inclusive, including their exact boundary.
    exact = _search_json(
        "--site",
        "mocksearch",
        "--min-words",
        "300000",
        "--max-words",
        "300000",
        keyword="斗破苍穹",
        cwd=work,
        home=home,
    )
    assert [row["book_id"] for row in exact] == ["201"]

    # A limit must count surviving rows: the higher-quality 201 is filtered out
    # first, so a pre-filter limit would have returned nothing here.
    limited = _search_json(
        "--site",
        "mocksearch",
        "--min-words",
        "500000",
        "--limit",
        "1",
        keyword="斗破苍穹",
        cwd=work,
        home=home,
    )
    assert [row["book_id"] for row in limited] == ["202"]

    date_and_word = _search_json(
        "--site",
        "mocksearch",
        "--updated-after",
        "2021-01-01",
        "--max-words",
        "60000",
        keyword="斗破苍穹",
        cwd=work,
        home=home,
    )
    assert [row["book_id"] for row in date_and_word] == ["204"]

    # Sorting is orthogonal to filtering: the survivors keep the words order.
    sorted_filtered = _search_json(
        "--site",
        "mocksearch",
        "--sort",
        "words",
        "--min-words",
        "300000",
        keyword="斗破苍穹",
        cwd=work,
        home=home,
    )
    assert [row["book_id"] for row in sorted_filtered] == ["202", "201"]

    # An over-tight filter is still a successful, empty document.
    empty = _search_json(
        "--site",
        "mocksearch",
        "--max-words",
        "0",
        keyword="斗破苍穹",
        cwd=work,
        home=home,
    )
    assert empty == []

    leftovers = _residual_files(work, ignored_roots={"settings.toml", "rules", "logs"})
    assert leftovers == set()
    assert not list(work.rglob("*.tmp"))
    assert not list(work.rglob("*.part"))
    assert list(home.rglob("*")) == []


def test_search_grouped_json_keeps_rank_telemetry(
    tmp_path: Path, ranking_site: str
) -> None:
    """``--group`` merges sources but keeps the best source's telemetry."""
    work, home = _ranking_work(
        tmp_path,
        ranking_site,
        sites=(("mocksearch", 10), ("mocksearch2", 100)),
    )

    flat = _search_json(
        "--site",
        "mocksearch",
        "--site",
        "mocksearch2",
        keyword="斗破苍穹",
        cwd=work,
        home=home,
    )
    assert sorted(row["book_id"] for row in flat) == [
        "201",
        "201",
        "202",
        "202",
        "203",
        "203",
    ]
    assert {row["site"] for row in flat} == {"mocksearch", "mocksearch2"}

    grouped = _search_json(
        "--site",
        "mocksearch",
        "--site",
        "mocksearch2",
        "--group",
        keyword="斗破苍穹",
        cwd=work,
        home=home,
    )
    assert [row["book_id"] for row in grouped] == ["201", "202", "203"]
    assert {row["query_intent"] for row in grouped} == {"title"}
    assert all(row["sources"] == ["mocksearch", "mocksearch2"] for row in grouped)
    _assert_telemetry(grouped)

    # With grouping, ``limit`` counts books, not raw site rows.
    limited = _search_json(
        "--site",
        "mocksearch",
        "--site",
        "mocksearch2",
        "--group",
        "--limit",
        "2",
        keyword="斗破苍穹",
        cwd=work,
        home=home,
    )
    assert len(limited) == 2


def test_search_human_table_shows_intent_and_match_suffix(
    tmp_path: Path, ranking_site: str
) -> None:
    """The interactive table carries the same verdict and badge as JSON."""
    work, home = _ranking_work(tmp_path, ranking_site)

    title = _run_cli_closed_stdin(
        "search",
        "--site",
        "mocksearch",
        "--site-limit",
        "20",
        "斗破苍穹",
        cwd=work,
        home=home,
    )
    assert title.returncode == 0, title.stderr
    # Human output is diagnostics; stdout stays reserved for machine documents.
    assert title.stdout == ""
    title_err = title.stderr
    assert "Search Results · Page 1/1 · Title match" in title_err
    assert re.search(r"斗破苍穹 · Exact \d\.\d{2}", title_err)
    assert re.search(r"斗破苍穹 外传 · Prefix \d\.\d{2}", title_err)
    assert re.search(r"斗破苍穹 番外 · Prefix \d\.\d{2}", title_err)
    assert (
        title_err.index("斗破苍穹 · Exact")
        < title_err.index("斗破苍穹 外传 · Prefix")
        < title_err.index("斗破苍穹 番外 · Prefix")
    )
    # A redirected stream never receives ANSI escapes.
    assert "\x1b" not in title_err

    author = _run_cli_closed_stdin(
        "search",
        "--site",
        "mocksearch",
        "--site-limit",
        "20",
        "天蚕土豆",
        cwd=work,
        home=home,
    )
    assert author.returncode == 0, author.stderr
    assert author.stdout == ""
    author_err = author.stderr
    assert "Search Results · Page 1/1 · Author match" in author_err
    assert re.search(r"斗破苍穹 · Exact \d\.\d{2}", author_err)
    assert re.search(r"天蚕土豆 · No match \d\.\d{2}", author_err)
    assert author_err.index("斗破苍穹 · Exact") < author_err.index(
        "天蚕土豆 · No match"
    )

    # A non-quality sort has no intent note, but the badge still explains each
    # row's score and its rank_score-derived order.
    words = _run_cli_closed_stdin(
        "search",
        "--site",
        "mocksearch",
        "--site-limit",
        "20",
        "--sort",
        "words",
        "斗破苍穹",
        cwd=work,
        home=home,
    )
    assert words.returncode == 0, words.stderr
    assert words.stdout == ""
    words_err = words.stderr
    assert "Title match" not in words_err
    assert "Author match" not in words_err
    assert re.search(r"斗破苍穹 外传 · Prefix \d\.\d{2}", words_err)
    assert words_err.index("斗破苍穹 外传 · Prefix") < words_err.index(
        "斗破苍穹 · Exact"
    )

    leftovers = _residual_files(work, ignored_roots={"settings.toml", "rules", "logs"})
    assert leftovers == set()
    assert not list(work.rglob("*.tmp"))
    assert not list(work.rglob("*.part"))
    assert list(home.rglob("*")) == []


@pytest.mark.parametrize(
    ("options", "needles"),
    [
        (("--sort", "sideways"), ("--sort", "invalid choice")),
        (("--order", "sideways"), ("--order", "invalid choice")),
        (("--max-words", "1000000001"), ("--min-words/--max-words", "between 0")),
        (("--updated-after", "2024-13-01"), ("Invalid --updated-after",)),
        (("--json", "--first"), ("--json and --first cannot be combined",)),
        (
            ("--min-words", "100", "--max-words", "50"),
            ("Min words cannot be greater than max words",),
        ),
    ],
)
def test_search_invalid_values_exit_usage(
    tmp_path: Path,
    options: tuple[str, ...],
    needles: tuple[str, ...],
) -> None:
    """Malformed sort/order/filter values fail before any search is attempted."""
    # No server is needed: every case is rejected before the searcher runs.
    work, home = _ranking_work(tmp_path, "http://127.0.0.1:9")

    result = run_cli("search", *options, "斗破苍穹", cwd=work, home=home)

    assert result.returncode == 2
    assert result.stdout == ""
    for needle in needles:
        assert needle in result.stderr
    assert not list(work.rglob("*.tmp"))
    assert not list(work.rglob("*.part"))
