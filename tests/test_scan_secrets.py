"""Tests for scripts/scan_secrets.py.

Every synthetic credential is assembled at runtime (``"AK" "IA" + ...``), so
this file contains no contiguous token that the scanner would -- correctly --
flag when it scans the repository.  The integration tests build a throwaway
Git repository, which exercises the real ``git ls-files`` path without
touching the working tree.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "scan_secrets.py"


def _load_script() -> Any:
    """Import a non-package script by path, as the other script tests do."""
    spec = importlib.util.spec_from_file_location("scan_secrets_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


scanner = _load_script()

# Synthetic values.  The string literals are split with explicit ``+`` so the
# raw test source does not itself contain a token-shaped string (the formatter
# merges implicit adjacency, so it must not be used here).
FAKE_AWS = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
FAKE_GITHUB = "gh" + "p_" + "A" * 36
FAKE_GITHUB_FINE = "github" + "_pat_" + "B" * 30
FAKE_SLACK = "xox" + "b-" + "1234567890-abcdefghij"
FAKE_BEARER = "C" * 32
FAKE_PASSWORD = "Sup3r" + "S3cretValue!"
PRIVATE_KEY_HEADER = "-----BEGIN " + "RSA PRIVATE KEY-----"


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
    )


def _make_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """Create a Git repository whose index lists *files*."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    # Keep blob content byte-identical on Windows so line numbers are exact.
    _git(root, "config", "core.autocrlf", "false")
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(root, "add", "-A")
    return root


def _commit(root: Path, message: str) -> str:
    """Commit the index with an explicit identity; return the new commit hash."""
    _git(
        root,
        "-c",
        "user.name=Scan Test",
        "-c",
        "user.email=scan@example.com",
        "commit",
        "-q",
        "-m",
        message,
    )
    return _git_output(root, "rev-parse", "HEAD").strip()


def _git_output(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _make_history_repo(
    tmp_path: Path, revisions: list[dict[str, str | None]]
) -> tuple[Path, list[str]]:
    """Build a repository with one commit per revision.

    A ``None`` value deletes that path, which lets a test prove the scanner
    sees a credential whose file no longer exists in the working tree.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "core.autocrlf", "false")
    commits: list[str] = []
    for index, files in enumerate(revisions):
        for name, content in files.items():
            path = root / name
            if content is None:
                path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        _git(root, "add", "-A")
        commits.append(_commit(root, f"revision {index}"))
    return root, commits


# ---------------------------------------------------------------------------
# Rule coverage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "rule"),
    [
        (f'key = "{PRIVATE_KEY_HEADER}"', "private-key"),
        (f'key = "{FAKE_AWS}"', "aws-access-key"),
        (f'value = "{FAKE_GITHUB}"', "github-token"),
        (f'value = "{FAKE_GITHUB_FINE}"', "github-token"),
        (f'value = "{FAKE_SLACK}"', "slack-token"),
        (f'Authorization = "Bearer {FAKE_BEARER}"', "bearer-token"),
        (f'password = "{FAKE_PASSWORD}"', "generic-secret"),
        (f'api-key: "{FAKE_PASSWORD}"', "generic-secret"),
        (f'AWS_SECRET_ACCESS_KEY = "{FAKE_PASSWORD}"', "generic-secret"),
    ],
)
def test_detects_credential_shapes(line: str, rule: str) -> None:
    findings = scanner.scan_text("src/example.py", line + "\n")

    assert [finding.rule for finding in findings] == [rule]


@pytest.mark.parametrize(
    "line",
    [
        'password = "your-password-here"',
        'password = "changeme"',
        'token = "${API_TOKEN}"',
        'token = "{{ token }}"',
        'api_key = os.environ["API_KEY"]',
        "secret = None",
        "token = get_token()",
        "token = token.strip()",
        "token = settings.api_token",
        "TOKEN=my-secret",
        "X-API-Key: my-secret",
        "const token = ++searchSeq;",
        'url = "https://example.com/?token=abcdefgh&x=2"',
        "client_secret = request.json()['client_secret']",
    ],
)
def test_ignores_placeholders_and_references(line: str) -> None:
    assert scanner.scan_text("src/example.py", line + "\n") == []


def test_bearer_match_is_case_insensitive() -> None:
    findings = scanner.scan_text("src/example.py", f'header = "bearer {FAKE_BEARER}"\n')

    assert [finding.rule for finding in findings] == ["bearer-token"]


def test_reports_location_only() -> None:
    findings = scanner.scan_text(
        "src/example.py", "# comment\n" + f'password = "{FAKE_PASSWORD}"\n'
    )

    assert findings == [scanner.Finding("src/example.py", 2, "generic-secret")]


# ---------------------------------------------------------------------------
# Allowlist and file selection
# ---------------------------------------------------------------------------


def test_test_fixtures_are_relaxed_but_private_keys_are_not() -> None:
    generic = scanner.scan_text(
        "tests/apps/test_api.py", f'password = "{FAKE_PASSWORD}"\n'
    )
    private = scanner.scan_text(
        "tests/apps/test_api.py", f'key = "{PRIVATE_KEY_HEADER}"\n'
    )

    assert generic == []
    assert [finding.rule for finding in private] == ["private-key"]


def test_vendored_public_constants_only_relax_generic_rule() -> None:
    vendor = "src/novel_downloader/apps/music/providers/_vendor/musicdl/x.py"

    assert scanner.scan_text(vendor, f'client_secret = "{FAKE_PASSWORD}"\n') == []
    assert [
        finding.rule
        for finding in scanner.scan_text(vendor, f'value = "{FAKE_GITHUB}"\n')
    ] == ["github-token"]


def test_sensitive_file_names_are_reported(tmp_path: Path) -> None:
    root = _make_repo(
        tmp_path,
        {
            "android/release.jks": "",
            "android/keystore.p12": "",
            "keys/id_rsa": "",
            "tests/fixtures/debug.keystore": "",
        },
    )

    findings = scanner.scan_repository(root)

    assert sorted((item.path, item.line, item.rule) for item in findings) == [
        ("android/keystore.p12", 0, "sensitive-file"),
        ("android/release.jks", 0, "sensitive-file"),
        ("keys/id_rsa", 0, "sensitive-file"),
        ("tests/fixtures/debug.keystore", 0, "sensitive-file"),
    ]


def test_binary_and_non_text_files_are_skipped(tmp_path: Path) -> None:
    root = _make_repo(
        tmp_path,
        {
            "assets/logo.png": f"not really a png {FAKE_GITHUB}",
            "blob.py": "\x00" + FAKE_GITHUB,
        },
    )

    assert scanner.scan_repository(root) == []


def test_oversized_files_are_skipped(tmp_path: Path) -> None:
    filler = "x = 1\n" * (scanner.MAX_FILE_BYTES // 6 + 10)
    root = _make_repo(tmp_path, {"big.py": filler + f'value = "{FAKE_GITHUB}"\n'})

    assert scanner.scan_repository(root) == []


# ---------------------------------------------------------------------------
# History scan
# ---------------------------------------------------------------------------


def test_history_finds_credential_removed_from_worktree(tmp_path: Path) -> None:
    root, commits = _make_history_repo(
        tmp_path,
        [
            {"app.py": "print('hello')\n"},
            {"config.py": "# comment\n" + f'password = "{FAKE_PASSWORD}"\n'},
            {"config.py": "print('clean')\n"},
        ],
    )

    report = scanner.scan_history(root)

    assert report.findings == [
        scanner.HistoryFinding(commits[1], "config.py", 2, "generic-secret")
    ]
    # The current tree is clean, so only the history pass can see this.
    assert scanner.scan_repository(root) == []


def test_history_finds_credential_in_deleted_file(tmp_path: Path) -> None:
    root, commits = _make_history_repo(
        tmp_path,
        [
            {"app.py": "print('hello')\n"},
            {"leaked.py": f'value = "{FAKE_GITHUB}"\n'},
            {"leaked.py": None},
        ],
    )

    report = scanner.scan_history(root)

    assert report.findings == [
        scanner.HistoryFinding(commits[1], "leaked.py", 1, "github-token")
    ]


def test_history_since_excludes_older_commits(tmp_path: Path) -> None:
    root, commits = _make_history_repo(
        tmp_path,
        [
            {"a.py": f'password = "{FAKE_PASSWORD}"\n'},
            {"b.py": f'value = "{FAKE_GITHUB}"\n'},
        ],
    )

    report = scanner.scan_history(root, since=commits[0])

    assert [item.path for item in report.findings] == ["b.py"]
    assert report.commits_scanned == 1


def test_history_max_commits_bounds_the_walk(tmp_path: Path) -> None:
    root, commits = _make_history_repo(
        tmp_path,
        [
            {"a.py": f'password = "{FAKE_PASSWORD}"\n'},
            {"b.py": f'value = "{FAKE_GITHUB}"\n'},
        ],
    )

    report = scanner.scan_history(root, max_commits=1)

    assert report.commits_scanned == 1
    assert report.findings == [
        scanner.HistoryFinding(commits[1], "b.py", 1, "github-token")
    ]


def test_history_shares_the_path_allowlist(tmp_path: Path) -> None:
    root, commits = _make_history_repo(
        tmp_path,
        [
            {
                "tests/fixtures/config.py": (
                    f'password = "{FAKE_PASSWORD}"\n' + f'key = "{FAKE_AWS}"\n'
                )
            }
        ],
    )

    report = scanner.scan_history(root)

    # The generic heuristic is relaxed for tests/, provider tokens are not.
    assert report.findings == [
        scanner.HistoryFinding(
            commits[0], "tests/fixtures/config.py", 2, "aws-access-key"
        )
    ]


def test_history_detects_sensitive_file_names(tmp_path: Path) -> None:
    # A binary keystore has no ``+++`` header (binary diffs carry no patch), so
    # the filename in ``diff --git`` is the only signal.
    root, commits = _make_history_repo(
        tmp_path,
        [{"android/release.jks": "\x00binary"}],
    )

    report = scanner.scan_history(root)

    assert report.findings == [
        scanner.HistoryFinding(commits[0], "android/release.jks", 0, "sensitive-file")
    ]


def test_history_skips_binary_blobs(tmp_path: Path) -> None:
    root, _commits = _make_history_repo(
        tmp_path,
        [{"assets/logo.png": "\x00" + FAKE_GITHUB}],
    )

    assert scanner.scan_history(root).findings == []


def test_history_skips_oversized_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _commits = _make_history_repo(
        tmp_path,
        [
            {"app.py": "print('hello')\n"},
            {"config.py": f'password = "{FAKE_PASSWORD}"\n'},
        ],
    )
    monkeypatch.setattr(scanner, "MAX_COMMIT_PATCH_BYTES", 1)

    report = scanner.scan_history(root)

    assert report.findings == []
    assert report.skipped_commits == report.commits_scanned == 2


def test_history_stops_at_total_size_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _commits = _make_history_repo(
        tmp_path,
        [{"config.py": f'password = "{FAKE_PASSWORD}"\n'}],
    )
    monkeypatch.setattr(scanner, "MAX_TOTAL_PATCH_BYTES", 1)

    report = scanner.scan_history(root)

    assert report.truncated is True
    assert report.commits_scanned == 1


def test_history_cli_reports_commit_without_leaking_value(
    tmp_path: Path, capsys: Any
) -> None:
    root, commits = _make_history_repo(
        tmp_path,
        [{"config.py": f'value = "{FAKE_GITHUB}"\n'}],
    )

    code = scanner.main(["--root", str(root), "--history", "--json"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 1
    assert payload["count"] == 1
    assert payload["commits_scanned"] == 1
    assert payload["findings"] == [
        {
            "commit": commits[0],
            "path": "config.py",
            "line": 1,
            "rule": "github-token",
        }
    ]
    assert FAKE_GITHUB not in captured.out
    assert FAKE_GITHUB not in captured.err


def test_history_cli_text_output_uses_short_hash(tmp_path: Path, capsys: Any) -> None:
    root, commits = _make_history_repo(
        tmp_path,
        [{"config.py": f'password = "{FAKE_PASSWORD}"\n'}],
    )

    code = scanner.main(["--root", str(root), "--history"])

    captured = capsys.readouterr()
    assert code == 1
    assert f"{commits[0][:12]}:config.py:1: generic-secret" in captured.out
    assert FAKE_PASSWORD not in captured.out


def test_history_cli_rejects_conflicting_flags(tmp_path: Path, capsys: Any) -> None:
    root = _make_repo(tmp_path, {"app.py": "print('hello')\n"})

    staged = scanner.main(["--root", str(root), "--history", "--staged"])
    since = scanner.main(["--root", str(root), "--since", "HEAD~1"])

    captured = capsys.readouterr()
    assert staged == 1
    assert since == 1
    assert "--history" in captured.err


def test_history_cli_requires_at_least_one_commit(tmp_path: Path, capsys: Any) -> None:
    root = _make_repo(tmp_path, {"app.py": "print('hello')\n"})

    code = scanner.main(["--root", str(root), "--history", "--max-commits", "0"])

    captured = capsys.readouterr()
    assert code == 1
    assert "--max-commits" in captured.err


# ---------------------------------------------------------------------------
# CLI contract
# ---------------------------------------------------------------------------


def test_cli_returns_zero_when_clean(tmp_path: Path, capsys: Any) -> None:
    root = _make_repo(tmp_path, {"app.py": "print('hello')\n"})

    code = scanner.main(["--root", str(root), "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"count": 0, "findings": []}


def test_cli_reports_finding_without_leaking_value(tmp_path: Path, capsys: Any) -> None:
    root = _make_repo(tmp_path, {"config.py": f'password = "{FAKE_PASSWORD}"\n'})

    code = scanner.main(["--root", str(root), "--json"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 1
    assert payload == {
        "count": 1,
        "findings": [{"line": 1, "path": "config.py", "rule": "generic-secret"}],
    }
    assert FAKE_PASSWORD not in captured.out
    assert FAKE_PASSWORD not in captured.err


def test_cli_human_output_does_not_leak_value(tmp_path: Path, capsys: Any) -> None:
    root = _make_repo(tmp_path, {"config.py": f'value = "{FAKE_GITHUB}"\n'})

    code = scanner.main(["--root", str(root)])

    captured = capsys.readouterr()
    assert code == 1
    assert "config.py:1: github-token" in captured.out
    assert FAKE_GITHUB not in captured.out


def test_staged_scan_reads_the_index_not_the_worktree(
    tmp_path: Path, capsys: Any
) -> None:
    root = _make_repo(tmp_path, {"config.py": "password = 'placeholder'\n"})
    # Stage the clean version above, then dirty only the working tree.
    (root / "config.py").write_text(f'password = "{FAKE_PASSWORD}"\n', encoding="utf-8")

    staged_code = scanner.main(["--root", str(root), "--staged", "--json"])
    worktree_code = scanner.main(["--root", str(root), "--json"])

    assert staged_code == 0
    assert worktree_code == 1
    capsys.readouterr()


def test_current_repository_is_clean(capsys: Any) -> None:
    code = scanner.main(["--root", str(ROOT)])

    captured = capsys.readouterr()
    assert code == 0, captured.out
