"""Unit tests for scripts/release.py (the one-command release flow).

Nothing here builds, tags, pushes or talks to GitHub: ``subprocess`` is
replaced with a scripted fake and ``urllib.request.urlopen`` with a scripted
HTTP router.  The tests assert the contracts the release depends on:

* CHANGELOG note extraction (headers, boundaries, missing sections),
* the dry-run plan (prints everything, executes nothing),
* idempotency (existing tags / releases / assets are skipped or updated,
  never duplicated),
* the credential, remote-slug, Android-signing and checksum-merge helpers,
* the release.yml workflow contract.
"""

from __future__ import annotations

import email.message
import importlib.util
import io
import json
import subprocess
import sys
import urllib.error
import urllib.request
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
RELEASE_SCRIPT = SCRIPTS / "release.py"
RELEASE_YML = ROOT / ".github" / "workflows" / "release.yml"

VERSION = "9.9.9"
TAG = f"v{VERSION}"
SLUG = "tester/example"
NOTES = "### Feat\n\n- ninth feature\n\n### Fix\n\n- fixed a bug"

CHANGELOG = (
    "## Unreleased\n\n### Fix\n\n- nothing yet\n\n"
    f"## v{VERSION} (2026-01-01)\n\n{NOTES}\n\n"
    "## v1.1.0 (2025-01-01)\n\nold stuff\n"
)

ZIP_NAMES = (
    f"novel-cli-{VERSION}-test-x64.zip",
    f"novel-api-{VERSION}-test-x64.zip",
    f"NovelDownloader-{VERSION}-test-x64.zip",
)
APK_NAME = f"novel-downloader-{VERSION}-android.apk"
APK_BYTES = b"APK-BYTES"
SETUP_NAME = f"NovelDownloader-setup-{VERSION}-x64.exe"
SETUP_BYTES = b"SETUP-EXE"

RELEASE_JSON = {
    "id": 7,
    "html_url": f"https://github.com/{SLUG}/releases/tag/{TAG}",
    "upload_url": (
        f"https://uploads.github.com/repos/{SLUG}/releases/7/assets{{?name,label}}"
    ),
    "assets": [],
    "body": "",
}


def _load_script(path: Path, name: str) -> Any:
    """Import a non-package script file (scripts/) by path."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before executing: dataclasses resolves string annotations via
    # sys.modules[cls.__module__] and crashes on an unregistered module.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


@pytest.fixture(scope="module")
def release() -> Any:
    return _load_script(RELEASE_SCRIPT, "audit_release")


# ---------------------------------------------------------------------------
# Fake repositories and scripted subprocess / HTTP doubles
# ---------------------------------------------------------------------------


def make_repo(tmp_path: Path) -> Path:
    """A minimal repository: package version + CHANGELOG."""
    root = tmp_path / "repo"
    package = root / "src" / "novel_downloader"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        f'__version__ = "{VERSION}"\n', encoding="utf-8"
    )
    (root / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    return root


def make_release_dir(root: Path) -> Path:
    """A dist/release directory as build_release.py would leave it."""
    release_dir = root / "dist" / "release" / f"novel-downloader-{VERSION}-test-x64"
    release_dir.mkdir(parents=True)
    sums = []
    for name in ZIP_NAMES:
        data = f"payload-{name}".encode()
        (release_dir / name).write_bytes(data)
        sums.append(f"{sha256(data).hexdigest()}  {name}")
    (release_dir / "SHA256SUMS.txt").write_text(
        "\n".join(sums) + "\n", encoding="utf-8", newline="\n"
    )
    (release_dir / "manifest.json").write_text(
        '{"version": "9.9.9"}\n', encoding="utf-8"
    )
    return release_dir


def make_android(root: Path) -> None:
    """Release signing material under android/ (git-ignored in real life)."""
    android = root / "android"
    android.mkdir(parents=True, exist_ok=True)
    (android / "keystore.properties").write_text(
        "storeFile=release.jks\nstorePassword=s\nkeyAlias=k\nkeyPassword=p\n",
        encoding="utf-8",
    )
    (android / "release.jks").write_bytes(b"jks")


def make_apk(root: Path) -> Path:
    """A gradle assembleRelease output."""
    outputs = root / "android" / "app" / "build" / "outputs" / "apk" / "release"
    outputs.mkdir(parents=True, exist_ok=True)
    apk = outputs / "app-release.apk"
    apk.write_bytes(APK_BYTES)
    return apk


class FakeSubprocess:
    """A ``subprocess`` module double routing commands to scripted results."""

    CompletedProcess = subprocess.CompletedProcess
    TimeoutExpired = subprocess.TimeoutExpired

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self._routes: list[
            tuple[tuple[str, ...], subprocess.CompletedProcess[str]]
        ] = []
        self._contains: list[tuple[str, subprocess.CompletedProcess[str]]] = []

    def route(
        self,
        prefix: tuple[str, ...],
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self._routes.append(
            (
                prefix,
                subprocess.CompletedProcess(
                    args=list(prefix),
                    returncode=returncode,
                    stdout=stdout,
                    stderr=stderr,
                ),
            )
        )

    def route_contains(
        self, token: str, *, returncode: int = 0, stdout: str = ""
    ) -> None:
        self._contains.append(
            (
                token,
                subprocess.CompletedProcess(
                    args=[token],
                    returncode=returncode,
                    stdout=stdout,
                    stderr="",
                ),
            )
        )

    def commands(self) -> list[list[str]]:
        return [command for command, _ in self.calls]

    def kwargs_of(self, command: list[str]) -> dict[str, Any]:
        for recorded, kwargs in self.calls:
            if recorded == command:
                return kwargs
        raise AssertionError(f"command not recorded: {command}")

    def run(
        self, command: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(command), kwargs))
        for token, proc in self._contains:
            if token in command:
                return proc
        for prefix, proc in self._routes:
            if tuple(command[: len(prefix)]) == prefix:
                return proc
        raise AssertionError(f"unexpected subprocess command: {command}")


class _FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeHTTP:
    """A ``urlopen`` double routing (method, url fragment) to scripted replies.

    Routes are matched in registration order; register specific routes
    (uploads) before generic ones (release creation).
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self._routes: list[tuple[str, str, Any]] = []

    def route(self, method: str, fragment: str, reply: Any) -> None:
        self._routes.append((method, fragment, reply))

    def route_dynamic(self, method: str, fragment: str, reply: Any) -> None:
        """Register a route whose reply is computed when the request arrives."""
        self.route(method, fragment, reply)

    def route_json(self, method: str, fragment: str, status: int, payload: Any) -> None:
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.route(method, fragment, (status, body))

    def route_404(self, fragment: str) -> None:
        self.route(
            "GET",
            fragment,
            urllib.error.HTTPError(
                f"https://api.github.com{fragment}",
                404,
                "Not Found",
                email.message.Message(),
                io.BytesIO(b'{"message": "Not Found"}'),
            ),
        )

    def __call__(
        self, request: urllib.request.Request, timeout: float | None = None
    ) -> _FakeResponse:
        method = request.get_method()
        url = request.full_url
        headers = {key.lower(): value for key, value in request.headers.items()}
        self.requests.append(
            {
                "method": method,
                "url": url,
                "data": request.data,
                "headers": headers,
                "timeout": timeout,
            }
        )
        for route_method, fragment, reply in self._routes:
            if route_method == method and fragment in url:
                if callable(reply):
                    reply = reply()
                if isinstance(reply, Exception):
                    raise reply
                status, body = reply
                return _FakeResponse(status, body)
        raise AssertionError(f"unexpected HTTP request: {method} {url}")


@pytest.fixture()
def harness(
    release: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, FakeSubprocess, FakeHTTP]:
    """A fake repo with a built release dir plus wired subprocess/HTTP fakes."""
    root = make_repo(tmp_path)
    make_release_dir(root)
    sub = FakeSubprocess()
    http = FakeHTTP()
    monkeypatch.setattr(release, "REPO_ROOT", root)
    monkeypatch.setattr(release, "subprocess", sub)
    monkeypatch.setattr(urllib.request, "urlopen", http)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    return root, sub, http


def _route_tag_steps(sub: FakeSubprocess, *, local: bool, remote: bool) -> None:
    sub.route(
        ("git", "rev-parse", "-q", "--verify"),
        returncode=0 if local else 128,
    )
    sub.route(
        ("git", "ls-remote", "origin"),
        stdout=f"abc123\trefs/tags/{TAG}\n" if remote else "",
    )
    sub.route(("git", "tag"))
    sub.route(("git", "push"))


def _route_publish(http: FakeHTTP, upload_names: tuple[str, ...]) -> None:
    """Model a release that does not exist yet and gains assets as they upload."""

    def _tag_state() -> tuple[int, bytes]:
        created = any(
            request["method"] == "POST"
            and request["url"].rstrip("/").endswith("/releases")
            for request in http.requests
        )
        if not created:
            return 404, b'{"message": "Not Found"}'
        assets = [
            {
                "name": request["url"].split("?name=", 1)[1],
                "digest": "sha256:" + sha256(request["data"] or b"").hexdigest(),
            }
            for request in http.requests
            if request["method"] == "POST" and "?name=" in request["url"]
        ]
        return 200, json.dumps({**RELEASE_JSON, "assets": assets}).encode()

    http.route_dynamic("GET", f"/releases/tags/{TAG}", _tag_state)
    for index, name in enumerate(upload_names):
        http.route_json("POST", f"?name={name}", 201, {"id": 100 + index})
    http.route_json("POST", f"{SLUG}/releases", 201, RELEASE_JSON)


def _digest(path: Path) -> str:
    return f"sha256:{sha256(path.read_bytes()).hexdigest()}"


# ---------------------------------------------------------------------------
# CHANGELOG notes extraction
# ---------------------------------------------------------------------------


class TestChangelogNotes:
    def _write(self, tmp_path: Path, text: str) -> Path:
        path = tmp_path / "CHANGELOG.md"
        path.write_text(text, encoding="utf-8")
        return path

    def test_extracts_section_body(self, release: Any, tmp_path: Path) -> None:
        notes = release.extract_changelog_notes(
            self._write(tmp_path, CHANGELOG), VERSION
        )
        assert notes == NOTES

    def test_header_line_is_not_part_of_the_body(
        self, release: Any, tmp_path: Path
    ) -> None:
        notes = release.extract_changelog_notes(
            self._write(tmp_path, CHANGELOG), VERSION
        )
        assert "2026-01-01" not in notes
        assert not any(line.startswith("## ") for line in notes.splitlines())

    def test_bare_version_header_without_v(self, release: Any, tmp_path: Path) -> None:
        text = f"## {VERSION}\n\nbody text\n\n## v1.0.0\n\nold\n"
        assert (
            release.extract_changelog_notes(self._write(tmp_path, text), VERSION)
            == "body text"
        )

    def test_unreleased_section(self, release: Any, tmp_path: Path) -> None:
        assert (
            release.extract_changelog_notes(
                self._write(tmp_path, CHANGELOG), "Unreleased"
            )
            == "### Fix\n\n- nothing yet"
        )

    def test_prerelease_version_matches_its_own_section(
        self, release: Any, tmp_path: Path
    ) -> None:
        text = f"## v{VERSION}-beta.1 (2026-01-01)\n\nbeta body\n"
        notes = release.extract_changelog_notes(
            self._write(tmp_path, text), f"{VERSION}-beta.1"
        )
        assert notes == "beta body"

    def test_stable_version_does_not_match_prerelease_section(
        self, release: Any, tmp_path: Path
    ) -> None:
        text = f"## v{VERSION}-beta.1 (2026-01-01)\n\nbeta body\n"
        with pytest.raises(release.ReleaseError, match="no section"):
            release.extract_changelog_notes(self._write(tmp_path, text), VERSION)

    def test_version_boundary_rejects_longer_versions(
        self, release: Any, tmp_path: Path
    ) -> None:
        text = "## v9.9.90 (2026-01-01)\n\nother body\n"
        with pytest.raises(release.ReleaseError, match="no section"):
            release.extract_changelog_notes(self._write(tmp_path, text), VERSION)

    def test_missing_section_lists_available_ones(
        self, release: Any, tmp_path: Path
    ) -> None:
        with pytest.raises(release.ReleaseError) as excinfo:
            release.extract_changelog_notes(self._write(tmp_path, CHANGELOG), "8.8.8")
        message = str(excinfo.value)
        assert "8.8.8" in message
        assert f"v{VERSION} (2026-01-01)" in message
        assert "v1.1.0 (2025-01-01)" in message

    def test_missing_changelog_file(self, release: Any, tmp_path: Path) -> None:
        with pytest.raises(release.ReleaseError, match="CHANGELOG not found"):
            release.extract_changelog_notes(tmp_path / "missing.md", VERSION)


# ---------------------------------------------------------------------------
# Version, targets, remote slug
# ---------------------------------------------------------------------------


class TestReadVersion:
    def test_reads_version(self, release: Any, tmp_path: Path) -> None:
        root = make_repo(tmp_path)
        assert release.read_version(root) == VERSION

    def test_missing_package_file(self, release: Any, tmp_path: Path) -> None:
        with pytest.raises(release.ReleaseError, match="version file not found"):
            release.read_version(tmp_path)

    def test_no_version_string(self, release: Any, tmp_path: Path) -> None:
        package = tmp_path / "src" / "novel_downloader"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("nothing here\n", encoding="utf-8")
        with pytest.raises(release.ReleaseError, match="no __version__"):
            release.read_version(tmp_path)


class TestParseTargets:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("all", ["cli", "api", "desktop"]),
            ("", ["cli", "api", "desktop"]),
            ("cli", ["cli"]),
            ("desktop,cli", ["desktop", "cli"]),
            ("cli,cli,api", ["cli", "api"]),
        ],
    )
    def test_values(self, release: Any, value: str, expected: list[str]) -> None:
        assert release._parse_targets(value) == expected

    def test_unknown_target(self, release: Any) -> None:
        with pytest.raises(release.ReleaseError, match="unknown target"):
            release._parse_targets("cli,bogus")


class TestRemoteSlug:
    @pytest.mark.parametrize(
        ("url", "slug"),
        [
            (
                "https://github.com/lingyi-ling/novel-downloader.git",
                "lingyi-ling/novel-downloader",
            ),
            (
                "https://github.com/lingyi-ling/novel-downloader",
                "lingyi-ling/novel-downloader",
            ),
            (
                "https://github.com/lingyi-ling/novel-downloader/",
                "lingyi-ling/novel-downloader",
            ),
            (
                "git@github.com:lingyi-ling/novel-downloader.git",
                "lingyi-ling/novel-downloader",
            ),
            (
                "ssh://git@github.com/lingyi-ling/novel-downloader.git",
                "lingyi-ling/novel-downloader",
            ),
        ],
    )
    def test_github_urls(self, release: Any, url: str, slug: str) -> None:
        assert release.parse_remote_slug(url) == slug

    def test_non_github_url(self, release: Any) -> None:
        assert release.parse_remote_slug("https://gitee.com/a/b.git") is None

    def test_resolve_slug_uses_origin(
        self, release: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sub = FakeSubprocess()
        sub.route(
            ("git", "remote", "get-url", "origin"),
            stdout=f"https://github.com/{SLUG}.git\n",
        )
        monkeypatch.setattr(release, "subprocess", sub)
        assert release.resolve_slug(tmp_path) == SLUG

    def test_resolve_slug_without_origin(
        self, release: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sub = FakeSubprocess()
        sub.route(("git", "remote", "get-url", "origin"), returncode=128)
        monkeypatch.setattr(release, "subprocess", sub)
        with pytest.raises(release.ReleaseError, match="origin"):
            release.resolve_slug(tmp_path)


# ---------------------------------------------------------------------------
# Checksum merging
# ---------------------------------------------------------------------------


class TestMergeSums:
    def test_appends_new_entries(self, release: Any, tmp_path: Path) -> None:
        path = tmp_path / "SHA256SUMS.txt"
        path.write_text(f"{'a' * 64}  one.zip\n", encoding="utf-8")
        lines = release.merge_sums(path, [("two.apk", "b" * 64)])
        assert lines == [f"{'a' * 64}  one.zip", f"{'b' * 64}  two.apk"]
        assert path.read_text(encoding="utf-8") == (
            f"{'a' * 64}  one.zip\n{'b' * 64}  two.apk\n"
        )

    def test_updates_existing_entry_in_place(
        self, release: Any, tmp_path: Path
    ) -> None:
        path = tmp_path / "SHA256SUMS.txt"
        path.write_text(f"{'a' * 64}  one.zip\n{'b' * 64}  two.zip\n", encoding="utf-8")
        lines = release.merge_sums(path, [("one.zip", "c" * 64)])
        assert lines == [f"{'c' * 64}  one.zip", f"{'b' * 64}  two.zip"]

    def test_idempotent(self, release: Any, tmp_path: Path) -> None:
        path = tmp_path / "SHA256SUMS.txt"
        entries = [("one.zip", "a" * 64), ("two.apk", "b" * 64)]
        first = release.merge_sums(path, entries)
        second = release.merge_sums(path, entries)
        assert first == second
        assert path.read_text(encoding="utf-8") == (
            f"{'a' * 64}  one.zip\n{'b' * 64}  two.apk\n"
        )

    def test_missing_file_starts_empty(self, release: Any, tmp_path: Path) -> None:
        path = tmp_path / "sub" / "SHA256SUMS.txt"
        lines = release.merge_sums(path, [("one.zip", "a" * 64)])
        assert lines == [f"{'a' * 64}  one.zip"]


# ---------------------------------------------------------------------------
# Android helpers
# ---------------------------------------------------------------------------


class TestAndroidHelpers:
    def test_signing_ok(self, release: Any, tmp_path: Path) -> None:
        make_android(tmp_path)
        ok, reason = release.android_signing_available(tmp_path)
        assert ok and "release signing configured" in reason

    def test_signing_missing_properties(self, release: Any, tmp_path: Path) -> None:
        ok, reason = release.android_signing_available(tmp_path)
        assert not ok and "keystore.properties" in reason

    def test_signing_incomplete_keys(self, release: Any, tmp_path: Path) -> None:
        android = tmp_path / "android"
        android.mkdir()
        (android / "keystore.properties").write_text(
            "storeFile=release.jks\n", encoding="utf-8"
        )
        ok, reason = release.android_signing_available(tmp_path)
        assert not ok and "missing keys" in reason

    def test_signing_missing_keystore_file(self, release: Any, tmp_path: Path) -> None:
        android = tmp_path / "android"
        android.mkdir()
        (android / "keystore.properties").write_text(
            "storeFile=missing.jks\nstorePassword=s\nkeyAlias=k\nkeyPassword=p\n",
            encoding="utf-8",
        )
        ok, reason = release.android_signing_available(tmp_path)
        assert not ok and "missing.jks" in reason

    def test_asset_names_single_apk(self, release: Any, tmp_path: Path) -> None:
        apk = make_apk(tmp_path)
        assert release.android_asset_names(VERSION, [apk]) == [APK_NAME]

    def test_asset_names_multiple_apks(self, release: Any, tmp_path: Path) -> None:
        make_apk(tmp_path)
        outputs = tmp_path / "android" / "app" / "build" / "outputs" / "apk" / "release"
        second = outputs / "arm64-v8a.apk"
        second.write_bytes(b"another")
        names = release.android_asset_names(
            VERSION, release.find_release_apks(tmp_path)
        )
        assert names == [
            f"novel-downloader-{VERSION}-android-app-release.apk",
            f"novel-downloader-{VERSION}-android-arm64-v8a.apk",
        ]

    def test_find_release_apks(self, release: Any, tmp_path: Path) -> None:
        assert release.find_release_apks(tmp_path) == []
        apk = make_apk(tmp_path)
        assert release.find_release_apks(tmp_path) == [apk]


# ---------------------------------------------------------------------------
# Credential lookup
# ---------------------------------------------------------------------------


class TestGithubToken:
    def test_env_token_wins_without_subprocess(
        self, release: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sub = FakeSubprocess()
        monkeypatch.setattr(release, "subprocess", sub)
        monkeypatch.setenv("GITHUB_TOKEN", "env-token")
        assert release.github_token(tmp_path) == "env-token"
        assert sub.calls == []

    def test_credential_fill_password(
        self, release: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        sub = FakeSubprocess()
        sub.route(
            ("git", "credential", "fill"),
            stdout="protocol=https\nhost=github.com\nusername=u\npassword=fake-pat\n",
        )
        monkeypatch.setattr(release, "subprocess", sub)
        assert release.github_token(tmp_path) == "fake-pat"
        command, kwargs = sub.calls[0]
        assert command == ["git", "credential", "fill"]
        assert kwargs["input"] == "protocol=https\nhost=github.com\n\n"
        # Never allow an interactive prompt to hang the release.
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        assert kwargs["env"]["GCM_INTERACTIVE"] == "never"

    def test_credential_fill_failure_returns_none(
        self, release: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        sub = FakeSubprocess()
        sub.route(("git", "credential", "fill"), returncode=1)
        monkeypatch.setattr(release, "subprocess", sub)
        assert release.github_token(tmp_path) is None

    def test_credential_fill_empty_password_returns_none(
        self, release: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        sub = FakeSubprocess()
        sub.route(("git", "credential", "fill"), stdout="password=\n")
        monkeypatch.setattr(release, "subprocess", sub)
        assert release.github_token(tmp_path) is None


# ---------------------------------------------------------------------------
# GitHub REST client
# ---------------------------------------------------------------------------


class TestGitHubClient:
    def _client(self, release: Any) -> Any:
        return release.GitHubClient(SLUG, "test-token")

    def _patched(
        self, release: Any, monkeypatch: pytest.MonkeyPatch
    ) -> tuple[Any, FakeHTTP]:
        http = FakeHTTP()
        monkeypatch.setattr(urllib.request, "urlopen", http)
        return self._client(release), http

    def test_get_release_404_returns_none(
        self, release: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, http = self._patched(release, monkeypatch)
        http.route_404(f"/releases/tags/{TAG}")
        assert client.get_release_by_tag(TAG) is None

    def test_create_release_posts_notes(
        self, release: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, http = self._patched(release, monkeypatch)
        http.route_json("POST", f"{SLUG}/releases", 201, RELEASE_JSON)
        payload = client.create_release(TAG, TAG, NOTES)
        assert payload["id"] == 7
        request = http.requests[0]
        assert request["method"] == "POST"
        assert request["url"] == f"https://api.github.com/repos/{SLUG}/releases"
        body = json.loads(request["data"])
        assert body["tag_name"] == TAG
        assert body["body"] == NOTES

    def test_upload_asset_posts_file_bytes(
        self, release: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        client, http = self._patched(release, monkeypatch)
        http.route_json("POST", "?name=thing.zip", 201, {})
        path = tmp_path / "thing.zip"
        path.write_bytes(b"ZIPDATA")
        asset = release.AssetSpec(path, "thing.zip", "0" * 64)
        client.upload_asset(RELEASE_JSON["upload_url"], asset)
        request = http.requests[0]
        assert request["method"] == "POST"
        assert request["url"].endswith("/releases/7/assets?name=thing.zip")
        assert request["data"] == b"ZIPDATA"
        assert request["headers"]["content-type"] == "application/octet-stream"

    def test_upload_asset_requires_upload_url(
        self, release: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        client, _ = self._patched(release, monkeypatch)
        path = tmp_path / "thing.zip"
        path.write_bytes(b"ZIPDATA")
        asset = release.AssetSpec(path, "thing.zip", "0" * 64)
        with pytest.raises(release.ReleaseError, match="upload_url"):
            client.upload_asset("", asset)

    def test_error_status_raises_with_message(
        self, release: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, http = self._patched(release, monkeypatch)
        http.route_json("GET", f"/releases/tags/{TAG}", 403, {"message": "forbidden"})
        with pytest.raises(release.ReleaseError, match="403.*forbidden"):
            client.get_release_by_tag(TAG)

    def test_authorization_header_is_sent(
        self, release: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client, http = self._patched(release, monkeypatch)
        http.route_404(f"/releases/tags/{TAG}")
        client.get_release_by_tag(TAG)
        assert http.requests[0]["headers"]["authorization"] == "Bearer test-token"


# ---------------------------------------------------------------------------
# Dry run: everything printed, nothing executed
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_prints_plan_without_side_effects(
        self,
        release: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = make_repo(tmp_path)
        make_release_dir(root)
        make_android(root)
        make_apk(root)
        sub = FakeSubprocess()
        http = FakeHTTP()
        monkeypatch.setattr(release, "REPO_ROOT", root)
        monkeypatch.setattr(release, "subprocess", sub)
        monkeypatch.setattr(urllib.request, "urlopen", http)

        assert release.main(["--dry-run"]) == 0

        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert f"Version     : {VERSION}" in out
        assert f"Tag         : {TAG}" in out
        assert f"Repository  : {SLUG}" in out or "git remote" in out
        # Notes preview comes from the CHANGELOG.
        assert "- ninth feature" in out
        assert "### Fix" in out
        # The planned commands.
        assert "build_release.py" in out
        assert "--targets cli,api,desktop" in out
        assert "assembleRelease" in out
        assert f"git tag {TAG}" in out
        assert f"git push origin {TAG}" in out
        # Assets found on disk are listed by name.
        for name in (*ZIP_NAMES, APK_NAME):
            assert name in out
        assert "SHA256SUMS.txt (after the merge)" in out
        assert "manifest.json" in out
        # The whole point: nothing ran, nothing was fetched.
        assert sub.calls == []
        assert http.requests == []

    def test_accepts_v_prefixed_version_argument(
        self,
        release: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = make_repo(tmp_path)
        monkeypatch.setattr(release, "REPO_ROOT", root)
        monkeypatch.setattr(release, "subprocess", FakeSubprocess())
        monkeypatch.setattr(urllib.request, "urlopen", FakeHTTP())
        assert release.main(["--dry-run", "--version", f"v{VERSION}"]) == 0
        assert f"Tag         : {TAG}" in capsys.readouterr().out

    def test_plan_without_build_outputs(
        self,
        release: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = make_repo(tmp_path)
        monkeypatch.setattr(release, "REPO_ROOT", root)
        monkeypatch.setattr(release, "subprocess", FakeSubprocess())
        monkeypatch.setattr(urllib.request, "urlopen", FakeHTTP())
        assert release.main(["--dry-run"]) == 0
        out = capsys.readouterr().out
        assert "nothing built yet" in out

    def test_android_without_keystore_is_skipped(
        self,
        release: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = make_repo(tmp_path)
        monkeypatch.setattr(release, "REPO_ROOT", root)
        monkeypatch.setattr(release, "subprocess", FakeSubprocess())
        monkeypatch.setattr(urllib.request, "urlopen", FakeHTTP())
        assert release.main(["--dry-run"]) == 0
        out = capsys.readouterr().out
        assert "SKIP" in out
        assert "keystore.properties" in out

    def test_skip_build_points_at_the_release_dir(
        self,
        release: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = make_repo(tmp_path)
        release_dir = make_release_dir(root)
        monkeypatch.setattr(release, "REPO_ROOT", root)
        monkeypatch.setattr(release, "subprocess", FakeSubprocess())
        monkeypatch.setattr(urllib.request, "urlopen", FakeHTTP())
        assert release.main(["--dry-run", "--skip-build"]) == 0
        out = capsys.readouterr().out
        assert "skipped (--skip-build)" in out
        assert str(release_dir) in out

    def test_missing_notes_fail_the_dry_run(
        self,
        release: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = make_repo(tmp_path)
        monkeypatch.setattr(release, "REPO_ROOT", root)
        monkeypatch.setattr(release, "subprocess", FakeSubprocess())
        monkeypatch.setattr(urllib.request, "urlopen", FakeHTTP())
        assert release.main(["--dry-run", "--version", "8.8.8"]) == 2
        err = capsys.readouterr().err
        assert "no section" in err

    def test_unreleased_version_is_rejected(
        self,
        release: Any,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = make_repo(tmp_path)
        monkeypatch.setattr(release, "REPO_ROOT", root)
        monkeypatch.setattr(release, "subprocess", FakeSubprocess())
        monkeypatch.setattr(urllib.request, "urlopen", FakeHTTP())
        assert release.main(["--dry-run", "--version", "Unreleased"]) == 2
        assert "not a releasable version" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Full publish flow (fresh release)
# ---------------------------------------------------------------------------


class TestMainPublish:
    def test_publishes_new_release(
        self,
        release: Any,
        harness: tuple[Path, FakeSubprocess, FakeHTTP],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root, sub, http = harness
        _route_tag_steps(sub, local=False, remote=False)
        _route_publish(http, (*ZIP_NAMES, "SHA256SUMS.txt", "manifest.json"))

        rc = release.main(["--skip-build", "--skip-android", "--repo", SLUG])

        assert rc == 0
        captured = capsys.readouterr()
        out = captured.out
        assert f"Releasing novel-downloader {VERSION} as {TAG} to {SLUG}" in out
        assert RELEASE_JSON["html_url"] in out

        # Tag created and pushed; push authenticates with the env token.
        commands = sub.commands()
        assert ["git", "tag", TAG] in commands
        push = next(cmd for cmd in commands if cmd[:2] == ["git", "push"])
        assert push == [
            "git",
            "push",
            f"https://x-access-token:test-token@github.com/{SLUG}.git",
            TAG,
        ]
        # The token must never reach the printed output.
        assert "test-token" not in captured.out
        assert "test-token" not in captured.err

        # Release created with the CHANGELOG notes.
        create = next(
            request
            for request in http.requests
            if request["method"] == "POST" and request["url"].endswith("/releases")
        )
        body = json.loads(create["data"])
        assert body["tag_name"] == TAG
        assert body["name"] == TAG
        assert body["body"] == NOTES

        # Every artifact plus sums and manifest was uploaded.
        uploads = [
            request
            for request in http.requests
            if request["method"] == "POST" and "/assets?name=" in request["url"]
        ]
        uploaded = {request["url"].split("?name=")[1] for request in uploads}
        assert uploaded == {*ZIP_NAMES, "SHA256SUMS.txt", "manifest.json"}
        assert all(request["data"] for request in uploads)

        # API calls all carry the token.
        assert all(
            request["headers"]["authorization"] == "Bearer test-token"
            for request in http.requests
        )

    def test_no_token_fails_early(
        self,
        release: Any,
        harness: tuple[Path, FakeSubprocess, FakeHTTP],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root, sub, http = harness
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        sub.route(("git", "credential", "fill"), returncode=1)
        rc = release.main(["--skip-build", "--skip-android", "--repo", SLUG])
        assert rc == 2
        assert "no GitHub token" in capsys.readouterr().err
        # Nothing was tagged or pushed after the token check failed.
        assert not any(cmd[:2] == ["git", "tag"] for cmd in sub.commands())
        assert http.requests == []


# ---------------------------------------------------------------------------
# Idempotency: re-running a finished (or half-finished) release
# ---------------------------------------------------------------------------


class TestMainIdempotent:
    def _assets_payload(
        self, release_dir: Path, stale: str | None
    ) -> list[dict[str, Any]]:
        names = (*ZIP_NAMES, "SHA256SUMS.txt", "manifest.json")
        assets = []
        for index, name in enumerate(names):
            digest = (
                "sha256:" + "f" * 64 if name == stale else _digest(release_dir / name)
            )
            assets.append({"name": name, "id": 100 + index, "digest": digest})
        return assets

    def test_finished_release_is_read_only(
        self,
        release: Any,
        harness: tuple[Path, FakeSubprocess, FakeHTTP],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root, sub, http = harness
        release_dir = root / "dist" / "release" / f"novel-downloader-{VERSION}-test-x64"
        _route_tag_steps(sub, local=True, remote=True)
        http.route_json(
            "GET",
            f"/releases/tags/{TAG}",
            200,
            {
                **RELEASE_JSON,
                "body": NOTES,
                "assets": self._assets_payload(release_dir, stale=None),
            },
        )

        rc = release.main(["--skip-build", "--skip-android", "--repo", SLUG])

        assert rc == 0
        # Two reads (the lookup plus post-upload verification), zero writes.
        assert [request["method"] for request in http.requests] == ["GET", "GET"]
        # Tags exist on both sides: no tag or push commands.
        commands = sub.commands()
        assert not any(cmd[:2] == ["git", "tag"] for cmd in commands)
        assert not any(cmd[:2] == ["git", "push"] for cmd in commands)
        assert "up to date, skipping" in capsys.readouterr().out

    def test_stale_notes_and_changed_asset_are_updated(
        self,
        release: Any,
        harness: tuple[Path, FakeSubprocess, FakeHTTP],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root, sub, http = harness
        release_dir = root / "dist" / "release" / f"novel-downloader-{VERSION}-test-x64"
        stale_zip = ZIP_NAMES[1]
        assets = self._assets_payload(release_dir, stale=stale_zip)
        payload = {**RELEASE_JSON, "body": "old notes", "assets": assets}
        _route_tag_steps(sub, local=True, remote=True)

        def _current_release() -> tuple[int, bytes]:
            # After the replacement upload the server reports the new digest;
            # until then it still reports the stale one.
            replaced = any(
                request["method"] == "POST"
                and request["url"].endswith(f"?name={stale_zip}")
                for request in http.requests
            )
            body = {
                **RELEASE_JSON,
                "body": NOTES if replaced else "old notes",
                "assets": self._assets_payload(
                    release_dir, stale=None if replaced else stale_zip
                ),
            }
            return 200, json.dumps(body).encode("utf-8")

        http.route_dynamic("GET", f"/releases/tags/{TAG}", _current_release)
        http.route_json("PATCH", "/releases/7", 200, payload)
        http.route("DELETE", "/releases/assets/101", (204, b""))
        http.route_json("POST", f"?name={stale_zip}", 201, {})

        rc = release.main(["--skip-build", "--skip-android", "--repo", SLUG])

        assert rc == 0
        patch = next(
            request for request in http.requests if request["method"] == "PATCH"
        )
        assert json.loads(patch["data"]) == {"body": NOTES}
        deletes = [
            request for request in http.requests if request["method"] == "DELETE"
        ]
        assert [request["url"] for request in deletes] == [
            f"https://api.github.com/repos/{SLUG}/releases/assets/101"
        ]
        uploads = [
            request
            for request in http.requests
            if request["method"] == "POST" and "/assets?name=" in request["url"]
        ]
        assert [request["url"].split("?name=")[1] for request in uploads] == [stale_zip]
        assert "digest differs, replacing" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Android step
# ---------------------------------------------------------------------------


class TestMainAndroid:
    def test_android_build_merges_sums_and_uploads_apk(
        self,
        release: Any,
        harness: tuple[Path, FakeSubprocess, FakeHTTP],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root, sub, http = harness
        make_android(root)
        make_apk(root)
        installer = root / "dist" / SETUP_NAME
        installer.parent.mkdir(parents=True, exist_ok=True)
        installer.write_bytes(SETUP_BYTES)
        sub.route_contains("assembleRelease")
        _route_tag_steps(sub, local=False, remote=False)
        _route_publish(
            http,
            (*ZIP_NAMES, APK_NAME, SETUP_NAME, "SHA256SUMS.txt", "manifest.json"),
        )

        rc = release.main(["--skip-build", "--repo", SLUG])

        assert rc == 0
        commands = sub.commands()
        gradle = next(cmd for cmd in commands if "assembleRelease" in cmd)
        assert "--no-daemon" in gradle
        # gradlew runs with the android/ directory as its cwd.
        assert sub.kwargs_of(gradle)["cwd"] == str(root / "android")

        # The APK and the installer both reached the release assets.
        uploads = [
            request
            for request in http.requests
            if request["method"] == "POST" and "/assets?name=" in request["url"]
        ]
        uploaded = {request["url"].split("?name=")[1] for request in uploads}
        assert uploaded == {
            *ZIP_NAMES,
            APK_NAME,
            SETUP_NAME,
            "SHA256SUMS.txt",
            "manifest.json",
        }

        # ... and the merged SHA256SUMS.txt covers them (zip lines kept).
        sums_path = (
            root
            / "dist"
            / "release"
            / f"novel-downloader-{VERSION}-test-x64"
            / "SHA256SUMS.txt"
        )
        sums = sums_path.read_text(encoding="utf-8")
        for name in ZIP_NAMES:
            assert any(line.endswith(f"  {name}") for line in sums.splitlines())
        assert f"{sha256(APK_BYTES).hexdigest()}  {APK_NAME}" in sums
        assert f"{sha256(SETUP_BYTES).hexdigest()}  {SETUP_NAME}" in sums

    def test_missing_keystore_warns_and_skips_android(
        self,
        release: Any,
        harness: tuple[Path, FakeSubprocess, FakeHTTP],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root, sub, http = harness
        _route_tag_steps(sub, local=False, remote=False)
        _route_publish(http, (*ZIP_NAMES, "SHA256SUMS.txt", "manifest.json"))

        rc = release.main(["--skip-build", "--repo", SLUG])

        assert rc == 0
        out = capsys.readouterr().out
        assert "Android release APK skipped" in out
        assert not any("assembleRelease" in cmd for cmd in sub.commands())
        uploads = [
            request
            for request in http.requests
            if request["method"] == "POST" and "/assets?name=" in request["url"]
        ]
        assert APK_NAME not in {
            request["url"].split("?name=")[1] for request in uploads
        }

    def test_gradle_failure_aborts_the_release(
        self,
        release: Any,
        harness: tuple[Path, FakeSubprocess, FakeHTTP],
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root, sub, http = harness
        make_android(root)
        make_apk(root)
        sub.route_contains("assembleRelease", returncode=1, stdout="BUILD FAILED\n")

        rc = release.main(["--skip-build", "--repo", SLUG])

        assert rc == 2
        err = capsys.readouterr().err
        assert "assembleRelease" in err and "BUILD FAILED" in err
        # No release was created.
        assert http.requests == []


# ---------------------------------------------------------------------------
# release.yml workflow contract
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def workflow_text() -> str:
    assert RELEASE_YML.is_file()
    return RELEASE_YML.read_text(encoding="utf-8")


class TestReleaseWorkflowContract:
    def test_workflow_exists(self, workflow_text: str) -> None:
        assert workflow_text

    def test_is_not_triggered_by_tags(self, workflow_text: str) -> None:
        """A tag push must not start a second, conflicting publish.

        ``scripts/release.py`` pushes the tag itself and then uploads the
        assets.  A tag-triggered run starts at the same moment, builds its own
        bundles (without the signed APK or the installer) and overwrites them.
        """
        assert 'tags: ["v*"]' not in workflow_text
        assert "push:" not in workflow_text

    def test_can_be_dispatched_manually(self, workflow_text: str) -> None:
        assert "workflow_dispatch:" in workflow_text

    def test_needs_contents_write_permission(self, workflow_text: str) -> None:
        assert "contents: write" in workflow_text

    def test_publishes_with_the_workflow_token(self, workflow_text: str) -> None:
        assert "secrets.GITHUB_TOKEN" in workflow_text

    def test_runs_the_release_script(self, workflow_text: str) -> None:
        assert "scripts/release.py" in workflow_text

    def test_uses_a_full_checkout(self, workflow_text: str) -> None:
        assert "fetch-depth: 0" in workflow_text


# ---------------------------------------------------------------------------
# post-upload verification
# ---------------------------------------------------------------------------


class _Asset:
    def __init__(self, name: str, digest: str) -> None:
        self.name = name
        self.digest = digest


class _ReleaseClient:
    """Minimal stand-in exposing only what ``verify_assets`` needs."""

    def __init__(self, assets: list[_Asset] | None) -> None:
        self._assets = assets
        self.calls = 0

    def get_release_by_tag(self, tag: str) -> dict[str, Any] | None:
        self.calls += 1
        if self._assets is None:
            return None
        return {
            "tag_name": tag,
            "assets": [
                {"name": a.name, "digest": f"sha256:{a.digest}"} for a in self._assets
            ],
        }


def _specs(module: Any, tmp_path: Path) -> list[Any]:
    specs = []
    for name in ("a.zip", "b.apk"):
        path = tmp_path / name
        path.write_bytes(name.encode())
        digest = sha256(name.encode()).hexdigest()
        specs.append(module.AssetSpec(path, name, digest))
    return specs


class TestVerifyAssets:
    def test_passes_when_every_asset_is_present(
        self, release: Any, tmp_path: Path
    ) -> None:
        specs = _specs(release, tmp_path)
        client = _ReleaseClient([_Asset(s.name, s.sha256) for s in specs])

        release.verify_assets(client, TAG, specs)

        assert client.calls == 1

    def test_fails_when_an_asset_is_missing(self, release: Any, tmp_path: Path) -> None:
        specs = _specs(release, tmp_path)
        client = _ReleaseClient([_Asset(specs[0].name, specs[0].sha256)])

        with pytest.raises(release.ReleaseError, match="missing 1 asset"):
            release.verify_assets(client, TAG, specs)

    def test_fails_when_content_is_stale(self, release: Any, tmp_path: Path) -> None:
        specs = _specs(release, tmp_path)
        client = _ReleaseClient(
            [
                _Asset(specs[0].name, specs[0].sha256),
                _Asset(specs[1].name, "0" * 64),
            ]
        )

        with pytest.raises(release.ReleaseError, match="stale content"):
            release.verify_assets(client, TAG, specs)

    def test_fails_when_the_release_disappeared(
        self, release: Any, tmp_path: Path
    ) -> None:
        with pytest.raises(release.ReleaseError, match="vanished"):
            release.verify_assets(_ReleaseClient(None), TAG, _specs(release, tmp_path))


# ---------------------------------------------------------------------------
# installer build step
# ---------------------------------------------------------------------------


def _build_prefix(module: Any, script: str) -> tuple[str, ...]:
    """The argv prefix release.py uses to invoke a script in ``scripts/``."""
    scripts = Path(module.__file__).resolve().parent
    return (module.sys.executable, str(scripts / script))


class TestInstallerStep:
    def test_builds_the_installer_when_desktop_is_targeted(
        self,
        release: Any,
        harness: tuple[Path, FakeSubprocess, FakeHTTP],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root, sub, http = harness
        _route_tag_steps(sub, local=False, remote=False)
        _route_publish(http, (*ZIP_NAMES, "SHA256SUMS.txt", "manifest.json"))
        sub.route(_build_prefix(release, "build_release.py"))
        sub.route(_build_prefix(release, "build_desktop.py"))
        monkeypatch.setattr(release.sys, "platform", "win32")

        rc = release.main(["--repo", SLUG, "--skip-android"])

        assert rc == 0
        joined = [" ".join(command) for command in sub.commands()]
        assert any("build_release.py" in line for line in joined)
        assert any("build_desktop.py" in line for line in joined)

    def test_installer_build_is_skipped_without_the_desktop_target(
        self,
        release: Any,
        harness: tuple[Path, FakeSubprocess, FakeHTTP],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root, sub, http = harness
        _route_tag_steps(sub, local=False, remote=False)
        _route_publish(http, (*ZIP_NAMES, "SHA256SUMS.txt", "manifest.json"))
        sub.route(_build_prefix(release, "build_release.py"))
        sub.route(_build_prefix(release, "build_desktop.py"))
        monkeypatch.setattr(release.sys, "platform", "win32")

        rc = release.main(["--repo", SLUG, "--skip-android", "--targets", "cli"])

        assert rc == 0
        joined = [" ".join(command) for command in sub.commands()]
        assert not any("build_desktop.py" in line for line in joined)

    def test_a_failed_installer_build_aborts_the_release(
        self,
        release: Any,
        harness: tuple[Path, FakeSubprocess, FakeHTTP],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root, sub, http = harness
        _route_tag_steps(sub, local=False, remote=False)
        _route_publish(http, (*ZIP_NAMES, "SHA256SUMS.txt", "manifest.json"))
        sub.route(_build_prefix(release, "build_release.py"))
        sub.route(
            _build_prefix(release, "build_desktop.py"),
            returncode=1,
            stdout="ISCC failed\n",
        )
        monkeypatch.setattr(release.sys, "platform", "win32")

        rc = release.main(["--repo", SLUG, "--skip-android"])

        assert rc == 2
        assert "build_desktop.py exited with 1" in capsys.readouterr().err
        # Nothing was tagged or published.
        assert not any(command[:2] == ["git", "tag"] for command in sub.commands())
        assert http.requests == []
