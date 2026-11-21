#!/usr/bin/env python3
"""Static audit of the Android FileProvider exposure surface.

The provider config, the manifest declaration and the grant sites only exist
in the source tree, so a regression (a broad ``<root-path>``, a writable grant,
a second authority, an exported intent that turns a URI into a local path)
would otherwise only show up on a device. These tests parse the real files a
release build packages and fail the plain ``pytest`` gate.

No Android SDK, Gradle build or device is involved. Components contributed by
dependencies are checked by Gradle lint against the *merged* manifest, which is
out of scope here.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "android" / "app"
MAIN = APP / "src" / "main"
MANIFEST = MAIN / "AndroidManifest.xml"
RES_XML = MAIN / "res" / "xml"
FILE_PATHS = RES_XML / "file_paths.xml"
JAVA = MAIN / "java" / "com" / "lingyi" / "noveldl"

ANDROID_NS = "{http://schemas.android.com/apk/res/android}"

#: FileProvider roots that would expose a whole storage domain.
BROAD_TAGS = {
    "root-path",
    "external-path",
    "external-files-path",
    "external-cache-path",
}

#: The two files allowed to hand a private file to another app.
EXPECTED_GRANT_FILES = {"UpdateState.kt", "MusicLyricOpener.kt"}


def android_attr(element: ET.Element, name: str) -> str | None:
    """Attribute [name] in the Android namespace, or ``None`` when absent."""
    return element.get(ANDROID_NS + name)


def parse_xml(path: Path) -> ET.Element:
    """Parse [path] and return the document element."""
    return ET.parse(path).getroot()


def read(path: Path) -> str:
    """Read a UTF-8 source file."""
    return path.read_text(encoding="utf-8")


def kotlin_const(source: str, name: str) -> str:
    """Value of a Kotlin ``const val`` with or without an explicit type."""
    match = re.search(
        rf'const val {re.escape(name)}\s*(?::\s*String\s*)?=\s*"([^"]*)"',
        source,
    )
    assert match is not None, f"const val {name} not found"
    return match.group(1)


def main_kotlin_files() -> list[Path]:
    """Every production Kotlin source file."""
    return sorted(JAVA.rglob("*.kt"))


# ----------------------------------------------------------------------
# file_paths.xml: what the provider can reach
# ----------------------------------------------------------------------


def test_only_the_two_narrow_ranges_are_declared() -> None:
    """No root/external domain and exactly the update APK and music output."""
    root = parse_xml(FILE_PATHS)
    assert root.tag == "paths"

    entries = [(child.tag, child.get("name"), child.get("path")) for child in root]
    assert entries == [
        ("cache-path", "updates", "updates/"),
        ("files-path", "music", "engine_home/downloads/music/"),
    ]

    # Every root must be a named subdirectory, never a domain root or a
    # wildcard. A missing path attribute defaults to the whole root on
    # Android, so it is as dangerous as an explicit empty path.
    for tag, name, path in entries:
        assert tag not in BROAD_TAGS
        assert name
        assert path, f"{tag} must name an explicit subdirectory"
        assert not path.startswith("/")
        assert "*" not in path and "?" not in path
        assert ".." not in path.split("/")
        assert path.endswith("/")

    names = [name for _, name, _ in entries]
    assert len(names) == len(set(names))

    # A second paths file would silently widen the provider if it were the one
    # the manifest referenced; keep exactly one.
    assert sorted(RES_XML.glob("file_paths*.xml")) == [FILE_PATHS]
    assert not any(
        element.tag in BROAD_TAGS for element in parse_xml(FILE_PATHS).iter()
    )


def test_declared_paths_match_the_runtime_constants() -> None:
    """The XML range and the code that computes the paths cannot drift."""
    root = parse_xml(FILE_PATHS)
    by_name = {child.get("name"): child for child in root}

    updater = read(JAVA / "data" / "UpdateChecker.kt")
    updates_dir = kotlin_const(updater, "UPDATES_DIR")
    updates = by_name["updates"]
    assert updates.tag == "cache-path"
    assert updates.get("path") == f"{updates_dir}/"
    # The downloader must actually write below cacheDir/<UPDATES_DIR>; the
    # cache-path root is getCacheDir().
    assert "File(context.cacheDir, UPDATES_DIR)" in updater

    engine = read(JAVA / "data" / "EmbeddedEngine.kt")
    home = kotlin_const(engine, "HOME_DIRECTORY")
    lyrics = read(JAVA / "ui" / "music" / "MusicLyrics.kt")
    music_subdir = kotlin_const(lyrics, "EMBEDDED_MUSIC_SUBDIRECTORY")

    music = by_name["music"]
    assert music.tag == "files-path"
    assert music.get("path") == f"{home}/{music_subdir}/"


# ----------------------------------------------------------------------
# Manifest: one private, per-URI-granting provider
# ----------------------------------------------------------------------


def test_provider_is_private_unique_and_scoped_to_the_paths_file() -> None:
    """exported=false, one authority, per-URI grants, @xml/file_paths."""
    app = parse_xml(MANIFEST).find("application")
    assert app is not None

    providers = app.findall("provider")
    assert len(providers) == 1

    provider = providers[0]
    assert android_attr(provider, "name") == "androidx.core.content.FileProvider"
    assert android_attr(provider, "authorities") == "${applicationId}.fileprovider"
    assert android_attr(provider, "exported") == "false"
    assert android_attr(provider, "grantUriPermissions") == "true"
    # A provider-level permission is not needed: access is only ever granted
    # per URI. Keeping it unset also means no non-existent permission is
    # referenced.
    assert android_attr(provider, "permission") is None

    metadata = provider.findall("meta-data")
    assert len(metadata) == 1
    assert android_attr(metadata[0], "name") == "android.support.FILE_PROVIDER_PATHS"
    assert android_attr(metadata[0], "resource") == "@xml/file_paths"

    # No second manifest may declare another provider/authority; the merged
    # manifest is checked by Gradle lint instead.
    authorities: list[str] = []
    for manifest in sorted((APP / "src").rglob("AndroidManifest.xml")):
        for element in parse_xml(manifest).iter("provider"):
            authorities.append(android_attr(element, "authorities") or "")
    assert authorities == ["${applicationId}.fileprovider"]


# ----------------------------------------------------------------------
# Grant sites: single URI, read-only, no write/persistable/prefix grant
# ----------------------------------------------------------------------


def test_every_get_uri_for_file_site_grants_a_single_read_only_uri() -> None:
    """All FileProvider hand-offs go through the two reviewed call sites."""
    grant_files: dict[str, str] = {}
    for path in main_kotlin_files():
        text = read(path)
        if re.search(r"FileProvider\.getUriForFile\s*\(", text):
            grant_files[path.name] = text

    assert set(grant_files) == EXPECTED_GRANT_FILES, (
        f"a new FileProvider hand-off needs review: {sorted(grant_files)}"
    )

    for name, text in grant_files.items():
        assert "Intent.ACTION_VIEW" in text, name
        assert "setDataAndType(uri," in text, name
        # One URI, also visible to viewers that only inspect clip data.
        assert "ClipData.newRawUri(null, uri)" in text, name
        assert "FLAG_GRANT_READ_URI_PERMISSION" in text, name
        # Write access to an app-private file is never required, and a
        # persistable/prefix grant would outlive the intent.
        assert "FLAG_GRANT_WRITE_URI_PERMISSION" not in text, name
        assert "FLAG_GRANT_PERSISTABLE_URI_PERMISSION" not in text, name
        assert "FLAG_GRANT_PREFIX_URI_PERMISSION" not in text, name
        # The authority is derived from the installed application id, which is
        # what ${applicationId}.fileprovider resolves to.
        assert re.search(r"\$\{context\.packageName\}\.fileprovider", text), name

    # There is no share (ACTION_SEND) path; adding one must be a deliberate
    # change that re-runs this audit.
    all_sources = "\n".join(read(path) for path in main_kotlin_files())
    assert "Intent.ACTION_SEND" not in all_sources


def test_lyric_opener_is_limited_to_the_exposed_directory_and_lrc_files() -> None:
    """The opener re-checks the exact provider range, not the whole sandbox."""
    text = read(JAVA / "ui" / "music" / "MusicLyricOpener.kt")

    assert "MusicLyrics.providerRoot(context)" in text
    assert "MusicLocalPaths.isInside(" in text
    assert "MusicLocalPaths.deviceRoots(" not in text
    # The music directory also holds audio; only .lrc sidecars are handed out.
    assert "endsWith(MusicLyrics.EXTENSION, ignoreCase = true)" in text
    assert "MusicLyricOpenResult.Missing" in text


# ----------------------------------------------------------------------
# Incoming intents: no exported VIEW/OPEN handler, no URI-to-path bridge
# ----------------------------------------------------------------------


def test_exported_activity_accepts_no_view_or_data_intent() -> None:
    """Only MAIN/LAUNCHER, so no external URI reaches the activity."""
    app = parse_xml(MANIFEST).find("application")
    assert app is not None
    activities = app.findall("activity")
    assert len(activities) == 1

    activity = activities[0]
    assert android_attr(activity, "name") == ".MainActivity"
    assert android_attr(activity, "exported") == "true"

    filters = activity.findall("intent-filter")
    assert len(filters) == 1
    assert [android_attr(a, "name") for a in filters[0].findall("action")] == [
        "android.intent.action.MAIN"
    ]
    assert [android_attr(c, "name") for c in filters[0].findall("category")] == [
        "android.intent.category.LAUNCHER"
    ]
    # No VIEW / SEND / OPEN_DOCUMENT filter and no scheme/type/path matcher.
    assert filters[0].findall("data") == []


def test_activity_reads_only_the_notification_action_and_task_id() -> None:
    """Notification navigation never converts intent data into a file path."""
    text = read(JAVA / "MainActivity.kt")

    assert "getStringExtra(TaskOpen.EXTRA_TASK_ID)" in text
    assert "TaskOpen.shouldHandleDelivery(" in text
    # No URI is read from the delivered intent, so no arbitrary URI can be
    # translated into a provider file or a local path.
    assert not re.search(r"\bgetData\s*\(", text)
    assert "intent.data" not in text
    assert "intent?.data" not in text
    assert "openInputStream" not in text
    assert "contentResolver" not in text
    assert "DocumentFile" not in text
