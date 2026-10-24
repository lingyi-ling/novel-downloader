"""Tests for ``scripts/gen_release_notes.py``.

Each test builds a throwaway git repository in ``tmp_path`` so the real
project history is never touched.  The directory is removed by pytest when the
test finishes.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = ROOT / "scripts" / "gen_release_notes.py"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is required")


@pytest.fixture(scope="module")
def notes() -> object:
    """Load the generator script without making ``scripts`` a package."""

    spec = importlib.util.spec_from_file_location("gen_release_notes", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        encoding="utf-8",
        text=True,
    )


def _commit(repo: Path, subject: str, body: str | None = None) -> None:
    args = ["commit", "--allow-empty", "-m", subject]
    if body:
        args.extend(["-m", body])
    _git(repo, *args)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A repository with a known, deliberately mixed commit history."""

    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.name", "Test User")
    _git(tmp_path, "config", "user.email", "test@example.com")

    _commit(tmp_path, "feat(core): add alpha feature")
    _commit(tmp_path, "fix(ui): correct beta bug")
    _commit(
        tmp_path,
        "feat!: drop legacy api",
        "BREAKING CHANGE: legacy api removed",
    )
    _commit(tmp_path, "docs: update readme")
    _commit(tmp_path, "chore(deps): bump deps")
    _commit(tmp_path, "perf: speed up gamma")
    _commit(tmp_path, "not a conventional commit")
    _commit(tmp_path, "test: add coverage")
    _commit(
        tmp_path,
        "fix(config): repair delta",
        "BREAKING CHANGE: config key renamed",
    )
    _commit(tmp_path, "fix+perf: tune epsilon")
    return tmp_path


def _build(notes: object, repo: Path, **kwargs: object) -> dict[str, object]:
    defaults = {"version": "v1.1.0", "release_date": "2026-01-02"}
    defaults.update(kwargs)
    return notes.build_release_notes(repo, **defaults)  # type: ignore[attr-defined]


def test_groups_and_breaking_changes(notes: object, repo: Path) -> None:
    data = _build(notes, repo)

    stats = data["stats"]
    assert stats == {
        "commits": 10,
        "groups": 7,
        "breaking_changes": 2,
        "contributors": 1,
    }

    groups = {group["type"]: group for group in data["groups"]}
    assert list(groups) == ["feat", "fix", "perf", "docs", "test", "chore", "other"]
    assert len(groups["feat"]["commits"]) == 2
    # ``fix+perf`` is grouped under its first recognised type.
    assert len(groups["fix"]["commits"]) == 3
    assert len(groups["perf"]["commits"]) == 1
    other = groups["other"]["commits"][0]
    assert other["description"] == "not a conventional commit"

    scopes = {
        commit["description"]: commit["scope"] for commit in groups["fix"]["commits"]
    }
    assert scopes["correct beta bug"] == "ui"
    assert scopes["repair delta"] == "config"

    breaking = {commit["description"]: commit for commit in data["breaking_changes"]}
    assert set(breaking) == {"drop legacy api", "repair delta"}
    assert breaking["drop legacy api"]["breaking_notes"] == ["legacy api removed"]
    assert breaking["repair delta"]["breaking_notes"] == ["config key renamed"]
    assert data["contributors"] == [
        {"name": "Test User", "email": "test@example.com", "commits": 10}
    ]


def test_markdown_output(notes: object, repo: Path) -> None:
    markdown = notes.render_markdown(_build(notes, repo))  # type: ignore[attr-defined]

    assert markdown.startswith("# Release v1.1.0\n")
    assert "**Date:** 2026-01-02" in markdown
    assert "## Breaking Changes" in markdown
    assert "legacy api removed" in markdown
    assert "## Features" in markdown
    assert "**core:** add alpha feature" in markdown
    assert "## Other Changes" in markdown
    assert "## Contributors" in markdown
    assert "- Test User (10)" in markdown


def test_default_range_includes_first_commit(notes: object, repo: Path) -> None:
    data = _build(notes, repo)  # no ``from_ref``

    assert data["stats"]["commits"] == 10
    assert data["range"]["from"] is None
    assert data["range"]["label"] == "first commit..HEAD"


def test_default_range_uses_latest_tag(notes: object, repo: Path) -> None:
    _git(repo, "tag", "v1.0.0", "HEAD~4")
    data = _build(notes, repo)  # no ``from_ref``

    assert data["stats"]["commits"] == 4
    assert data["range"]["from"] == "v1.0.0"
    assert data["range"]["label"] == "v1.0.0..HEAD"
    assert notes.detect_version(repo, "HEAD") == "v1.0.0"  # type: ignore[attr-defined]


def test_explicit_range_filters_commits(notes: object, repo: Path) -> None:
    first = _git(repo, "rev-list", "--max-parents=0", "HEAD").stdout.strip()
    data = _build(notes, repo, from_ref=first)

    # The root commit is excluded by ``<from>..HEAD``.
    assert data["stats"]["commits"] == 9
    assert data["range"]["label"] == f"{first}..HEAD"


def test_empty_range_is_friendly(notes: object, repo: Path) -> None:
    data = _build(notes, repo, from_ref="HEAD", to_ref="HEAD")

    assert data["empty"] is True
    assert data["stats"] == {
        "commits": 0,
        "groups": 0,
        "breaking_changes": 0,
        "contributors": 0,
    }
    assert data["groups"] == []
    assert data["message"] == "No commits found in range HEAD..HEAD."

    markdown = notes.render_markdown(data)  # type: ignore[attr-defined]
    assert "_No commits found in range `HEAD..HEAD`._" in markdown


def test_json_cli_output(
    notes: object, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = notes.main(  # type: ignore[attr-defined]
        ["--repo", str(repo), "--version", "v2.0.0", "--json"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["version"] == "v2.0.0"
    assert payload["stats"]["commits"] == 10
    assert payload["groups"][0]["type"] == "feat"


def test_output_file(notes: object, repo: Path, tmp_path: Path) -> None:
    destination = tmp_path / "nested" / "RELEASE.md"
    exit_code = notes.main(  # type: ignore[attr-defined]
        ["--repo", str(repo), "--version", "v3.0.0", "--output", str(destination)]
    )

    assert exit_code == 0
    assert destination.read_text(encoding="utf-8").startswith("# Release v3.0.0")


def test_invalid_ref_reports_error(notes: object, repo: Path) -> None:
    with pytest.raises(notes.ReleaseNotesError):  # type: ignore[attr-defined]
        _build(notes, repo, from_ref="does-not-exist")
