#!/usr/bin/env python3
"""Audit that Android backup rules cover every private data location.

The app's cloud-backup policy is "exclude the whole private tree". That is
only safe while the rule files and the code agree: a new SharedPreferences
file, a new file under ``filesDir``, or a new database is silently backed up
to the user's Google account the moment the exclude list stops covering it,
and nothing in the build or the app reports it. This module therefore derives
the inventory from the Kotlin source itself and checks the XML against it,
instead of hard-coding a second, drift-prone copy of the list.

Audited invariants:

* every ``getSharedPreferences(...)`` name is excluded (``sharedpref``) and
  its device-protected twin (``device_sharedpref``) is excluded too;
* every DataStore file is excluded (it lives in ``files/datastore``);
* every entry written below ``filesDir``/``cacheDir`` is excluded, or -- for
  the cache -- is provably outside every backup domain;
* every SQLite/Room database, if one is ever added, is excluded
  (``database``/``device_database``); the app has none today;
* no ``<include>`` re-admits what an exclude removed, in either rule file;
* the rule files parse, contain only tags/attributes/domains the platform
  parser accepts, and use no path the platform silently skips (``..``,
  ``//``, absolute paths, unknown domains);
* ``fullBackupContent``/``dataExtractionRules`` point at these exact files
  and no custom ``backupAgent`` can bypass them;
* the credential files the embedded Python engine writes (site cookies,
  ``settings.toml``, music ``session.json``/``platform_creds.json``) resolve
  inside the private tree, never into the CWD or shared storage.

AOSP ``FullBackup.java`` is the reference for the accepted syntax; its
``getDirectoryForCriteriaDomain`` accepts exactly the nine domains in
[KNOWN_DOMAINS] and ``extractCanonicalFile`` silently drops a rule whose path
contains ``..`` or ``//``. A silently dropped exclude is an unexcluded
credential, so those shapes are rejected here as well.

No Android SDK, Gradle build or device is involved.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ANDROID_DIR = ROOT / "android"
MAIN_DIR = ANDROID_DIR / "app" / "src" / "main"
MANIFEST = MAIN_DIR / "AndroidManifest.xml"
RES_XML_DIR = MAIN_DIR / "res" / "xml"
BACKUP_RULES = RES_XML_DIR / "backup_rules.xml"
DATA_EXTRACTION_RULES = RES_XML_DIR / "data_extraction_rules.xml"
JAVA_DIR = MAIN_DIR / "java" / "com" / "lingyi" / "noveldl"
PY_SRC = ROOT / "src" / "novel_downloader"

ANDROID = "{http://schemas.android.com/apk/res/android}"

#: Domains ``FullBackup.getDirectoryForCriteriaDomain`` accepts. An unknown
#: domain is not an error at build time: the platform skips the rule, which
#: silently leaves that tree backed up.
KNOWN_DOMAINS = frozenset(
    {
        "root",
        "file",
        "database",
        "sharedpref",
        "external",
        "device_root",
        "device_file",
        "device_database",
        "device_sharedpref",
    }
)

#: Credential-protected domain -> its device-protected twin. Every exclude
#: must be mirrored, otherwise a value written through
#: ``createDeviceProtectedStorageContext()`` later is the one path a cloud
#: backup still covers. ``external`` has no device-protected counterpart in
#: the platform's domain list (``getDirectoryForCriteriaDomain``).
DEVICE_TWIN = {
    "root": "device_root",
    "file": "device_file",
    "database": "device_database",
    "sharedpref": "device_sharedpref",
}

#: Attributes the platform reads per rule tag (``FullBackup``
#: ``validateInnerTagContents`` allows at most these; ``requireFlags`` is only
#: honoured on ``<include>``).
ALLOWED_RULE_ATTRIBUTES = {
    "include": {"domain", "path", "requireFlags"},
    "exclude": {"domain", "path"},
}

#: Section-level attributes ``parseRequiredTransportFlags`` reads.
ALLOWED_SECTION_ATTRIBUTES = {
    "cloud-backup": {"disableIfNoEncryptionCapabilities"},
    "device-transfer": set(),
}

#: Every place the app persists private state, as a (domain, path) pair.
#: ``cache`` is not a backup domain at all -- the platform never backs the
#: cache directory up -- and is tracked so a future write cannot be mistaken
#: for a backed-up location.
BACKUP_DOMAINS = ("root", "file", "database", "sharedpref", "external")


@dataclass(frozen=True)
class Location:
    """One private storage location found in the source tree."""

    domain: str
    path: str
    source: str

    def __str__(self) -> str:
        return f"{self.domain}:{self.path} ({self.source})"


# ---------------------------------------------------------------------------
# Source helpers
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _kotlin_sources() -> list[Path]:
    return sorted(JAVA_DIR.rglob("*.kt"))


_CONST_RE = re.compile(r"const\s+val\s+(\w+)\s*(?::\s*String\s*)?=\s*\"([^\"]*)\"")


def _const_map(text: str) -> dict[str, str]:
    """``const val NAME = "value"`` declarations in one Kotlin file."""
    return dict(_CONST_RE.findall(text))


def _all_consts() -> dict[str, set[str]]:
    """Every ``const val`` in the production tree, name -> declared values."""
    found: dict[str, set[str]] = {}
    for path in _kotlin_sources():
        for name, value in _const_map(_read(path)).items():
            found.setdefault(name, set()).add(value)
    return found


def _literal_or_const(token: str, text: str, *, where: str) -> str:
    """Resolve a quoted literal or a const declared in the same file.

    ``PREFS_NAME`` is declared in both ``MainActivity`` and
    ``SelectionStore`` with different values, so resolution is tried per file
    first; a global map would silently pick the wrong name and the coverage
    check would then verify the wrong preference file. A constant declared in
    another file (``TASK_SNAPSHOT_FILE`` lives in ``TaskPersistence.kt`` but is
    used by ``TaskStore.kt``) is resolved globally, but only when the name
    resolves to a single value -- an ambiguous name is an error, not a guess.
    """
    token = token.strip()
    if token.startswith('"') and token.endswith('"'):
        return token[1:-1]
    local = _const_map(text)
    if token in local:
        return local[token]
    values = _all_consts().get(token, set())
    if len(values) == 1:
        return next(iter(values))
    raise AssertionError(
        f"{where}: cannot resolve Kotlin constant {token!r} "
        f"(declared values: {sorted(values) or 'none'})"
    )


# ``getSharedPreferences(NAME, MODE)`` -- NAME is a literal or a const.
_SHARED_PREFS_RE = re.compile(r"getSharedPreferences\(\s*([A-Za-z_]\w*|\"[^\"]*\")")

# ``preferencesDataStore(name = "x")`` -- the DataStore file is
# files/datastore/<name>.preferences_pb.
_DATASTORE_RE = re.compile(r"preferencesDataStore\(\s*name\s*=\s*\"([^\"]+)\"")

# ``File(context.filesDir, CONST)`` / ``File(context.applicationContext.filesDir,
# CONST)`` / ``filesDir.resolve(CONST)``.
_FILES_ENTRY_RE = re.compile(
    r"(?:\.filesDir\s*,\s*([A-Za-z_]\w*|\"[^\"]*\")"
    r"|filesDir\.resolve\(\s*([A-Za-z_]\w*)\s*\))"
)

# ``File(context.cacheDir, CONST)``.
_CACHE_ENTRY_RE = re.compile(r"\.cacheDir\s*,\s*([A-Za-z_]\w*|\"[^\"]*\")")

# Any of the ways a SQLite/Room database is opened or named.
_DATABASE_RE = re.compile(
    r"openOrCreateDatabase\s*\("
    r"|SQLiteOpenHelper"
    r"|Room\.databaseBuilder"
    r"|@Database\b"
    r"|getDatabasePath\s*\("
)


def discovered_shared_preferences() -> dict[str, str]:
    """SharedPreferences file name -> declaring source file."""
    found: dict[str, str] = {}
    for path in _kotlin_sources():
        text = _read(path)
        for token in _SHARED_PREFS_RE.findall(text):
            name = _literal_or_const(token, text, where=path.name)
            found[name] = path.name
    return found


def discovered_data_stores() -> dict[str, str]:
    """DataStore name -> declaring source file."""
    found: dict[str, str] = {}
    for path in _kotlin_sources():
        text = _read(path)
        for name in _DATASTORE_RE.findall(text):
            found[name] = path.name
    return found


def discovered_files_dir_entries() -> dict[str, str]:
    """Entries created directly below ``filesDir`` -> source file.

    Interpolated paths (``"${HOME}/downloads/music"``) are collected by
    [discovered_files_dir_templates] instead: their literal text is not a
    directory name, and treating it as one would invent a phantom entry.
    """
    found: dict[str, str] = {}
    for path in _kotlin_sources():
        text = _read(path)
        for literal, const in _FILES_ENTRY_RE.findall(text):
            token = literal or const
            if "${" in token:
                continue
            found[_literal_or_const(token, text, where=path.name)] = path.name
    return found


def discovered_files_dir_templates() -> dict[str, str]:
    """Interpolated ``filesDir`` paths -> source file."""
    found: dict[str, str] = {}
    for path in _kotlin_sources():
        text = _read(path)
        for literal, const in _FILES_ENTRY_RE.findall(text):
            token = (literal or const).strip()
            if "${" not in token:
                continue
            found[token.strip('"')] = path.name
    return found


def discovered_cache_dir_entries() -> dict[str, str]:
    """Entries created below ``cacheDir`` -> source file."""
    found: dict[str, str] = {}
    for path in _kotlin_sources():
        text = _read(path)
        for token in _CACHE_ENTRY_RE.findall(text):
            found[_literal_or_const(token, text, where=path.name)] = path.name
    return found


def discovered_database_sources() -> list[str]:
    """Source files that open a SQLite/Room database."""
    return [path.name for path in _kotlin_sources() if _DATABASE_RE.search(_read(path))]


def private_locations() -> list[Location]:
    """Every private location the Kotlin source writes, as backup coordinates."""
    locations = [
        Location("sharedpref", f"{name}.xml", source)
        for name, source in discovered_shared_preferences().items()
    ]
    # DataStore writes files/datastore/<name>.preferences_pb.
    locations += [
        Location("file", f"datastore/{name}.preferences_pb", source)
        for name, source in discovered_data_stores().items()
    ]
    locations += [
        Location("file", entry, source)
        for entry, source in discovered_files_dir_entries().items()
    ]
    # A database would live in databases/<name>; the domain is the important
    # part, the exact name is only known once one exists.
    locations += [
        Location("database", "<database>", source)
        for source in discovered_database_sources()
    ]
    return locations


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------


def _parse(path: Path) -> ET.Element:
    assert path.is_file(), f"missing {path.relative_to(ROOT).as_posix()}"
    return ET.parse(path).getroot()


def _children(element: ET.Element, tag: str | None = None) -> list[ET.Element]:
    return [child for child in element if tag is None or child.tag == tag]


def _excludes(element: ET.Element) -> list[tuple[str, str]]:
    return [
        (rule.get("domain") or "", rule.get("path") or "")
        for rule in _children(element, "exclude")
    ]


def _cloud_backup_section() -> ET.Element:
    root = _parse(DATA_EXTRACTION_RULES)
    sections = _children(root, "cloud-backup")
    assert len(sections) == 1, "expected exactly one <cloud-backup> section"
    return sections[0]


def covers(exclude: tuple[str, str], domain: str, path: str) -> bool:
    """Whether an ``(domain, path)`` exclude covers ``domain:path``.

    ``path="."`` (or an empty path, which the platform treats as the domain
    root) covers everything; otherwise the path must be an ancestor
    directory of the location.
    """
    exclude_domain, exclude_path = exclude
    if exclude_domain != domain:
        return False
    if exclude_path in {"", ".", "/"}:
        return True
    prefix = exclude_path.rstrip("/")
    return path == prefix or path.startswith(prefix + "/")


def _uncovered(excludes: list[tuple[str, str]], locations: list[Location]) -> list[str]:
    return [
        str(location)
        for location in locations
        if not any(covers(rule, location.domain, location.path) for rule in excludes)
    ]


def _application() -> ET.Element:
    root = _parse(MANIFEST)
    assert root.tag == "manifest"
    applications = _children(root, "application")
    assert len(applications) == 1
    return applications[0]


# ---------------------------------------------------------------------------
# Inventory: what the app actually writes
# ---------------------------------------------------------------------------


def test_shared_preferences_inventory_is_exactly_the_reviewed_set() -> None:
    """A new preference file must be added here deliberately.

    The names are not secrets by themselves, but every preference file is a
    place a future credential can land, and the whole set is excluded from
    backup. Discovering a name that is not listed means the coverage test
    below is checking a file nobody reviewed.
    """
    assert discovered_shared_preferences() == {
        # Notification-permission throttle, in MainActivity.
        "novel_dl_ui": "MainActivity.kt",
        # Last search-site / music-platform selection, in SelectionStore.
        "novel_dl_selection": "SelectionStore.kt",
    }


def test_datastore_inventory_is_exactly_the_reviewed_set() -> None:
    """``novel_dl_settings`` holds the API key, so it must never appear new."""
    assert discovered_data_stores() == {"novel_dl_settings": "SettingsStore.kt"}


def test_files_dir_inventory_matches_the_known_writers() -> None:
    """Direct children of ``filesDir``; ``engine_home`` holds the engine tree."""
    assert discovered_files_dir_entries() == {
        "engine_home": "EmbeddedEngine.kt",
        "music_track_paths.json": "MusicPathStore.kt",
        "search_history.json": "SearchHistoryStore.kt",
        "task_snapshot.json": "TaskStore.kt",
    }
    # The only interpolated path is the provider's music range, which is
    # inside engine_home and therefore covered by the same exclude.
    assert discovered_files_dir_templates() == {
        "${EmbeddedEngine.HOME_DIRECTORY}/$EMBEDDED_MUSIC_SUBDIRECTORY": (
            "MusicLyrics.kt"
        ),
    }


def test_cache_dir_inventory_is_only_the_verified_update_apk() -> None:
    """The cache is never backed up, so it must stay the only volatile copy."""
    assert discovered_cache_dir_entries() == {"updates": "UpdateChecker.kt"}
    # No exclude is needed for cacheDir because it is outside every backup
    # domain. Pin that: a "cache" domain in a rule file would mean the author
    # misunderstood the model and probably excluded the wrong thing.
    for rules in (_parse(BACKUP_RULES), _cloud_backup_section()):
        assert "cache" not in {domain for domain, _ in _excludes(rules)}


def test_no_sqlite_or_room_database_exists_today() -> None:
    """If a database appears, the coverage test below must start seeing it."""
    assert discovered_database_sources() == []
    # The exclude is still required as defence in depth: adding a database
    # must not require touching the rule file as well.
    for rules in (_parse(BACKUP_RULES), _cloud_backup_section()):
        assert ("database", ".") in _excludes(rules)


# ---------------------------------------------------------------------------
# Coverage: every private location is excluded, in both rule files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("location", private_locations(), ids=str)
def test_legacy_rules_exclude_every_private_location(location: Location) -> None:
    """Android 11 and below read ``backup_rules.xml``."""
    uncovered = _uncovered(_excludes(_parse(BACKUP_RULES)), [location])
    assert not uncovered, (
        f"{location} would be uploaded by cloud backup on Android 11 and "
        "below; add or widen an <exclude> in res/xml/backup_rules.xml"
    )


@pytest.mark.parametrize("location", private_locations(), ids=str)
def test_cloud_backup_rules_exclude_every_private_location(
    location: Location,
) -> None:
    """Android 12+ reads the ``<cloud-backup>`` section."""
    uncovered = _uncovered(_excludes(_cloud_backup_section()), [location])
    assert not uncovered, (
        f"{location} would be uploaded by cloud backup on Android 12+; add or "
        "widen an <exclude> in the <cloud-backup> section of "
        "res/xml/data_extraction_rules.xml"
    )


def test_both_rule_files_exclude_identical_paths() -> None:
    """A path excluded from only one file leaks on the other OS version."""
    legacy = sorted(_excludes(_parse(BACKUP_RULES)))
    cloud = sorted(_excludes(_cloud_backup_section()))
    assert legacy == cloud


def test_every_credential_protected_domain_has_its_device_twin() -> None:
    """``device_*`` is a separate tree; excluding only the CE side is a leak."""
    for rules in (_parse(BACKUP_RULES), _cloud_backup_section()):
        excluded = {domain for domain, _ in _excludes(rules)}
        for domain in BACKUP_DOMAINS:
            assert domain in excluded, f"{domain} is not excluded"
        for domain, twin in DEVICE_TWIN.items():
            assert twin in excluded, (
                f"{domain} is excluded but its device-protected twin {twin} is not"
            )


def test_no_include_reopens_an_excluded_domain() -> None:
    """``<include>`` wins over ``<exclude>`` for the same path."""
    assert not _children(_parse(BACKUP_RULES), "include")
    assert not _children(_cloud_backup_section(), "include")


def test_device_transfer_is_consistent_with_cloud_backup() -> None:
    """Device-to-device transfer is a deliberate policy choice, not an accident.

    Leaving ``<device-transfer>`` out keeps it unrestricted: the copy goes to
    the user's own new device, where restoring the API key and settings is
    expected. If it is ever narrowed, it must mirror the cloud list so no
    credential stays in the one section that still copies it.
    """
    root = _parse(DATA_EXTRACTION_RULES)
    sections = _children(root, "device-transfer")
    assert len(sections) <= 1
    if sections:
        assert sorted(_excludes(sections[0])) == sorted(
            _excludes(_cloud_backup_section())
        )
        assert not _children(sections[0], "include")


# ---------------------------------------------------------------------------
# Rule file syntax: the platform must not silently skip a rule
# ---------------------------------------------------------------------------


def test_rule_files_use_only_known_tags_attributes_and_domains() -> None:
    """A skipped rule is an unexcluded credential, so reject anything unknown."""
    legacy = _parse(BACKUP_RULES)
    assert legacy.tag == "full-backup-content"

    extraction = _parse(DATA_EXTRACTION_RULES)
    assert extraction.tag == "data-extraction-rules"

    sections = list(extraction)
    assert sections, "data_extraction_rules.xml has no sections"
    for section in sections:
        assert section.tag in ALLOWED_SECTION_ATTRIBUTES, (
            f"unknown section <{section.tag}>"
        )
        assert set(section.attrib) <= ALLOWED_SECTION_ATTRIBUTES[section.tag], (
            f"<{section.tag}> has attributes the platform ignores: "
            f"{sorted(set(section.attrib) - ALLOWED_SECTION_ATTRIBUTES[section.tag])}"
        )

    rules = list(legacy) + [rule for section in sections for rule in section]
    assert rules, "no backup rules declared at all"
    for rule in rules:
        assert rule.tag in ALLOWED_RULE_ATTRIBUTES, f"unknown tag <{rule.tag}>"
        allowed = ALLOWED_RULE_ATTRIBUTES[rule.tag]
        unexpected = set(rule.attrib) - allowed
        assert not unexpected, f"<{rule.tag}> has unknown attributes {unexpected}"
        assert list(rule) == [], f"<{rule.tag}> must be empty, not a container"

        domain = rule.get("domain")
        assert domain in KNOWN_DOMAINS, (
            f"domain {domain!r} is not one the platform resolves; the rule "
            "would be skipped silently"
        )
        path = rule.get("path")
        assert path is not None, f"{domain}: a rule without path is a typo"
        # FullBackup.extractCanonicalFile drops these silently.
        assert ".." not in path, f"{domain}:{path}: '..' makes the rule a no-op"
        assert "//" not in path, f"{domain}:{path}: '//' makes the rule a no-op"
        assert not path.startswith("/"), f"{domain}:{path}: path must be relative"


def test_rule_files_are_the_only_ones_the_manifest_references() -> None:
    """No stale second rule file can be swapped in unnoticed."""
    assert sorted(RES_XML_DIR.glob("backup_rules*.xml")) == [BACKUP_RULES]
    assert sorted(RES_XML_DIR.glob("data_extraction_rules*.xml")) == [
        DATA_EXTRACTION_RULES
    ]


# ---------------------------------------------------------------------------
# Manifest wiring
# ---------------------------------------------------------------------------


def test_manifest_references_the_rules_and_no_custom_backup_agent() -> None:
    application = _application()
    assert application.get(ANDROID + "allowBackup") == "true"
    assert application.get(ANDROID + "fullBackupContent") == "@xml/backup_rules"
    assert (
        application.get(ANDROID + "dataExtractionRules") == "@xml/data_extraction_rules"
    )
    # A custom BackupAgent (or fullBackupOnly) would bypass the XML entirely.
    for attribute in ("backupAgent", "fullBackupOnly", "killAfterRestore"):
        assert application.get(ANDROID + attribute) is None, (
            f"android:{attribute} changes or bypasses the backup policy"
        )


# ---------------------------------------------------------------------------
# The embedded engine's credential files land in the private tree
# ---------------------------------------------------------------------------


def test_engine_credential_files_resolve_inside_the_private_tree() -> None:
    """Cookies and music sessions must never be written to CWD or SD card.

    The rule files exclude the app-private domains, which only protects those
    credentials if the engine actually puts them there. ``app_home()`` and
    ``music_data_dir()`` both go through platformdirs (on Android:
    ``files/<app>``), while the login cache resolves from the configured
    ``cache_dir`` which defaults below ``app_home()``.
    """
    paths = _read(PY_SRC / "infra" / "paths.py")
    assert "user_data_path(PACKAGE_NAME" in paths, (
        "app_home() no longer uses the platform data directory"
    )
    assert "Path.cwd()" not in paths

    music_paths = _read(PY_SRC / "apps" / "music" / "paths.py")
    assert 'user_data_dir("novel_downloader"' in music_paths
    for name in ("session.json", "platform_creds.json", "settings.json"):
        assert f'"{name}"' in music_paths, f"music credential {name} moved"

    login = _read(PY_SRC / "apps" / "login" / "main.py")
    assert 'COOKIE_FILENAME = "aiohttp.cookies"' in login
    assert "_configured_cache_dir()" in login
    # The cookie directory is derived from the config/cache root, not the CWD.
    assert "Path.cwd()" not in login

    config = _read(PY_SRC / "schemas" / "config.py")
    assert 'DEFAULT_CACHE_DIRNAME = "cache"' in config
    assert "app_home() / DEFAULT_CACHE_DIRNAME" in config


def test_engine_home_is_excluded_as_a_whole() -> None:
    """Enumerating the engine tree is fragile; the whole directory is excluded."""
    home = re.search(
        r'const\s+val\s+HOME_DIRECTORY\s*(?::\s*String\s*)?=\s*"([^"]+)"',
        _read(JAVA_DIR / "data" / "EmbeddedEngine.kt"),
    )
    assert home is not None
    home_dir = home.group(1)
    assert discovered_files_dir_entries()[home_dir] == "EmbeddedEngine.kt"

    for rules in (_parse(BACKUP_RULES), _cloud_backup_section()):
        excluded = _excludes(rules)
        assert not _uncovered(excluded, [Location("file", home_dir, "test")]), (
            f"{home_dir} (settings.toml, cache cookies, task state, music "
            "session) is not excluded as a directory"
        )
