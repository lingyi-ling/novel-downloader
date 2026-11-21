#!/usr/bin/env python3
"""Static security audit of the Android packaging configuration.

The APK's attack surface is declared in XML, not in Kotlin, so these checks
read exactly the files a release build packages -- the manifest, the network
security config, the backup rules and the FileProvider paths -- with the
standard library only: no Gradle, no Android SDK, no device.

Audited invariants:

* every component with an intent filter declares ``android:exported``
  explicitly (mandatory from API 31) and only the launcher activity is
  exported;
* no intent filter accepts a deep link (scheme/host/MIME type), so no
  attacker-controlled URI ever reaches app code;
* the single FileProvider is private and exposes exactly two narrow
  directories -- the verified update APK and the music output that holds the
  lyrics -- never a domain root or external storage;
* cloud backup excludes every private storage domain, including
  device-protected storage, in both the Android 11 and the Android 12+ rule
  files;
* permissions stay at the three product-necessary entries;
* the update channel (the only path that delivers executable code) refuses
  cleartext and user-installed CAs, while global cleartext remains a
  deliberate, documented product decision for the loopback engine and
  user-configured LAN servers (Android cannot whitelist arbitrary RFC1918
  addresses).

The Kotlin counterpart ``AndroidSecurityConfigTest`` asserts the same files
from the Gradle unit-test gate; this module keeps the Python gate independent
of the Android toolchain.

No assertion here reads Kotlin business logic except the path constants that
must stay in lockstep with ``file_paths.xml``.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ANDROID_DIR = ROOT / "android"
MAIN_DIR = ANDROID_DIR / "app" / "src" / "main"
MANIFEST = MAIN_DIR / "AndroidManifest.xml"
RES_XML_DIR = MAIN_DIR / "res" / "xml"
APP_GRADLE = ANDROID_DIR / "app" / "build.gradle.kts"
JAVA_DIR = MAIN_DIR / "java" / "com" / "lingyi" / "noveldl"

ANDROID_NS = "http://schemas.android.com/apk/res/android"
ANDROID = f"{{{ANDROID_NS}}}"

MAIN_ACTIVITY = ".MainActivity"
FILE_PROVIDER = "androidx.core.content.FileProvider"
FILE_PROVIDER_AUTHORITY = "${applicationId}.fileprovider"

#: The only exported component the app declares itself. The launcher entry
#: point must be exported; it carries nothing but MAIN/LAUNCHER.
EXPORTED_COMPONENTS = {
    ("activity", MAIN_ACTIVITY): (
        "LAUNCHER entry point, required to start the app; its only intent "
        "filter is MAIN/LAUNCHER and it declares no data scheme"
    ),
}

COMPONENT_TAGS = ("activity", "activity-alias", "service", "receiver", "provider")

EXPECTED_PERMISSIONS = frozenset(
    {
        "android.permission.INTERNET",
        "android.permission.POST_NOTIFICATIONS",
        "android.permission.REQUEST_INSTALL_PACKAGES",
    }
)

#: Permissions that would widen the app's reach far beyond the product's
#: needs: unrestricted storage, package inventory, overlays, battery
#: exemptions, location, telephony and contacts.
OVERBROAD_PERMISSIONS = frozenset(
    {
        "android.permission.MANAGE_EXTERNAL_STORAGE",
        "android.permission.READ_EXTERNAL_STORAGE",
        "android.permission.WRITE_EXTERNAL_STORAGE",
        "android.permission.QUERY_ALL_PACKAGES",
        "android.permission.SYSTEM_ALERT_WINDOW",
        "android.permission.REQUEST_IGNORE_BATTERY_OPTIMIZATIONS",
        "android.permission.READ_PHONE_STATE",
        "android.permission.ACCESS_FINE_LOCATION",
        "android.permission.ACCESS_COARSE_LOCATION",
        "android.permission.READ_CONTACTS",
        "android.permission.CAMERA",
        "android.permission.RECORD_AUDIO",
    }
)

#: Backup domains that hold app-private data. ``root`` is the app data
#: directory; ``file``/``database``/``sharedpref``/``external`` are its
#: siblings and ``device_*`` is the parallel device-protected tree.
PRIVATE_BACKUP_DOMAINS = frozenset(
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

#: FileProvider tags that would expose a whole storage domain. Only narrow
#: ``cache-path``/``files-path`` subdirectories are acceptable.
FORBIDDEN_PATH_TAGS = frozenset(
    {
        "root-path",
        "external-path",
        "external-files-path",
        "external-cache-path",
        "external-media-path",
    }
)

#: Hosts a future ``cleartextTrafficPermitted="true"`` domain-config may
#: name. Cleartext for the loopback engine (and the emulator host) is the
#: product's transport contract; anything public must use TLS.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "10.0.2.2"})

#: The base config keeps cleartext enabled because the app must reach the
#: loopback engine and a user-configured LAN server over plain HTTP, and a
#: static config cannot enumerate arbitrary private addresses. Flipping this
#: changes the product's connectivity contract, so it is asserted explicitly;
#: the compensating controls (update-channel pinning, origin-scoped API key
#: and redirect downgrade refusal in NetworkHttp.kt) are checked separately.
BASE_CLEARTEXT_PERMITTED = True

#: Exported components a merged *release* manifest may contain, mapped to the
#: permission that must protect them (``None`` for the launcher only). These
#: come from AndroidX libraries, not from this repository.
MERGED_RELEASE_EXPORTED = {
    ("activity", "com.lingyi.noveldl.MainActivity"): None,
    (
        "receiver",
        "androidx.profileinstaller.ProfileInstallReceiver",
    ): "android.permission.DUMP",
}


# ---------------------------------------------------------------------------
# XML helpers
# ---------------------------------------------------------------------------


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _parse(path: Path) -> ET.Element:
    assert path.is_file(), f"missing {_rel(path)}"
    return ET.parse(path).getroot()


def _android(element: ET.Element, name: str) -> str | None:
    return element.get(ANDROID + name)


def _children(element: ET.Element, tag: str | None = None) -> list[ET.Element]:
    return [child for child in element if tag is None or child.tag == tag]


def _single(element: ET.Element, tag: str) -> ET.Element:
    matches = _children(element, tag)
    assert len(matches) == 1, f"expected exactly one <{tag}>, found {len(matches)}"
    return matches[0]


def _application() -> ET.Element:
    root = _parse(MANIFEST)
    assert root.tag == "manifest", f"{_rel(MANIFEST)}: root is <{root.tag}>"
    return _single(root, "application")


def _components(application: ET.Element) -> list[tuple[str, ET.Element]]:
    return [
        (tag, element)
        for tag in COMPONENT_TAGS
        for element in _children(application, tag)
    ]


def _kotlin_const(path: Path, name: str) -> str:
    """Literal value of ``const val NAME [: String] = "..."`` in a Kotlin file."""
    text = path.read_text(encoding="utf-8")
    pattern = (
        rf"const\s+val\s+{re.escape(name)}\s*"
        rf"(?::\s*[A-Za-z0-9_.<>?]+)?\s*=\s*\"([^\"]+)\""
    )
    match = re.search(pattern, text)
    assert match, f'{_rel(path)}: no `const val {name} = "..."` declaration'
    return match.group(1)


# ---------------------------------------------------------------------------
# Manifest: exported components and permissions
# ---------------------------------------------------------------------------


def test_target_sdk_is_new_enough_to_require_explicit_exported() -> None:
    text = APP_GRADLE.read_text(encoding="utf-8")
    match = re.search(r"\btargetSdk\s*=\s*(\d+)", text)
    assert match, f"{_rel(APP_GRADLE)}: no `targetSdk = <n>` declaration"
    target_sdk = int(match.group(1))
    assert target_sdk >= 31, (
        f"targetSdk is {target_sdk}; API 31+ requires android:exported on every "
        "filtered component, and the audit below assumes that rule is in force"
    )


def test_every_filtered_component_declares_exported_explicitly() -> None:
    application = _application()
    offenders: list[str] = []
    for tag, element in _components(application):
        name = _android(element, "name") or "<unnamed>"
        if not _children(element, "intent-filter"):
            continue
        if _android(element, "exported") not in {"true", "false"}:
            offenders.append(f"<{tag} {name}> has an intent-filter without exported")
    assert not offenders, (
        "Android 12+ refuses to install components with an intent filter and no "
        "explicit android:exported:\n" + "\n".join(offenders)
    )


def test_only_the_launcher_activity_is_exported() -> None:
    application = _application()
    exported = {
        (tag, _android(element, "name") or "<unnamed>")
        for tag, element in _components(application)
        if _android(element, "exported") == "true"
    }
    unexpected = sorted(exported - set(EXPORTED_COMPONENTS))
    assert not unexpected, (
        "components exported to other apps without a review: "
        f"{unexpected}. Every entry needs a rationale in EXPORTED_COMPONENTS."
    )
    missing = sorted(set(EXPORTED_COMPONENTS) - exported)
    assert not missing, f"whitelisted exported components disappeared: {missing}"


def test_launcher_activity_accepts_no_deep_links() -> None:
    activities = _children(_application(), "activity")
    assert len(activities) == 1
    activity = activities[0]
    assert _android(activity, "name") == MAIN_ACTIVITY
    assert _android(activity, "permission") is None, (
        "the launcher must stay startable by the system launcher"
    )

    filters = _children(activity, "intent-filter")
    assert len(filters) == 1
    intent_filter = filters[0]
    assert [_android(el, "name") for el in _children(intent_filter, "action")] == [
        "android.intent.action.MAIN"
    ]
    assert [_android(el, "name") for el in _children(intent_filter, "category")] == [
        "android.intent.category.LAUNCHER"
    ]
    assert not _children(intent_filter, "data"), (
        "an <data> element turns the launcher into a deep-link target; any "
        "scheme/host/path it declares is attacker-controllable"
    )


def test_no_intent_filter_is_a_deep_link_entry_point() -> None:
    manifest = _parse(MANIFEST)
    offenders: list[str] = []
    for intent_filter in manifest.iter("intent-filter"):
        for data in _children(intent_filter, "data"):
            declared = {
                name: _android(data, name)
                for name in (
                    "scheme",
                    "host",
                    "port",
                    "path",
                    "pathPrefix",
                    "pathPattern",
                    "mimeType",
                )
                if _android(data, name)
            }
            if declared:
                offenders.append(str(declared))
        for action in _children(intent_filter, "action"):
            if _android(action, "name") == "android.intent.action.VIEW":
                offenders.append("VIEW action")
        for category in _children(intent_filter, "category"):
            if _android(category, "name") == "android.intent.category.BROWSABLE":
                offenders.append("BROWSABLE category")
    assert not offenders, (
        "unexpected deep-link surface; URL arguments would need scheme/host "
        "allowlisting and per-parameter validation before use:\n" + "\n".join(offenders)
    )


def test_no_services_or_receivers_are_declared() -> None:
    application = _application()
    assert not _children(application, "service"), (
        "any manifest service would be a long-lived externally reachable "
        "component; the app has none"
    )
    assert not _children(application, "receiver"), (
        "the only receiver (DownloadManager.ACTION_DOWNLOAD_COMPLETE) is "
        "registered in-process with RECEIVER_NOT_EXPORTED in HistoryScreen"
    )


def test_permissions_are_the_minimal_product_set() -> None:
    manifest = _parse(MANIFEST)
    declared = {
        _android(element, "name") for element in _children(manifest, "uses-permission")
    }
    assert declared == set(EXPECTED_PERMISSIONS), (
        "permission set changed; update EXPECTED_PERMISSIONS only after "
        f"justifying the new entry. declared={sorted(declared)}"
    )
    overbroad = sorted(declared & OVERBROAD_PERMISSIONS)
    assert not overbroad, f"over-broad permissions requested: {overbroad}"


def test_manifest_does_not_opt_into_debuggable_or_global_cleartext() -> None:
    application = _application()
    assert _android(application, "debuggable") in {None, "false"}
    assert _android(application, "usesCleartextTraffic") in {None, "false"}, (
        "with a networkSecurityConfig in place the flag is ignored on API 24+, "
        "but a literal 'true' misleads reviewers and would become the policy if "
        "the config were ever dropped"
    )


# ---------------------------------------------------------------------------
# FileProvider
# ---------------------------------------------------------------------------


def test_file_provider_is_private_and_has_a_single_meta_data() -> None:
    providers = _children(_application(), "provider")
    assert len(providers) == 1
    provider = providers[0]
    assert _android(provider, "name") == FILE_PROVIDER
    assert _android(provider, "authorities") == FILE_PROVIDER_AUTHORITY
    assert _android(provider, "exported") == "false"
    assert _android(provider, "grantUriPermissions") == "true"

    metadata = _single(provider, "meta-data")
    assert _android(metadata, "name") == "android.support.FILE_PROVIDER_PATHS"
    assert _android(metadata, "resource") == "@xml/file_paths"


def test_file_provider_exposes_only_two_narrow_directories() -> None:
    paths = _parse(RES_XML_DIR / "file_paths.xml")
    assert paths.tag == "paths"

    entries = _children(paths)
    assert len(entries) == 2, (
        "every additional <paths> entry widens what a granted URI can reach"
    )
    tags = {entry.tag for entry in entries}
    forbidden = tags & FORBIDDEN_PATH_TAGS
    assert not forbidden, (
        f"storage-domain roots are exposed to the provider: {forbidden}"
    )
    assert tags <= {"cache-path", "files-path"}

    by_name = {entry.get("name"): entry for entry in entries}
    assert set(by_name) == {"updates", "music"}

    for entry in entries:
        path = entry.get("path") or ""
        assert path, f"{entry.get('name')}: missing path"
        name = entry.get("name")
        assert path not in {".", "/"}, f"{name}: exposes a domain root"
        assert not path.startswith("/"), f"{name}: path must be relative"
        assert ".." not in path.split("/"), f"{name}: path escapes the root"
        assert path.endswith("/"), (
            f"{name}: use a directory range ('{path}/'), not a single file"
        )

    # The declared ranges must be the directories the code actually hands out,
    # otherwise a verified APK/lyric is either unreachable or a wider path gets
    # added to compensate.
    updates_dir = _kotlin_const(JAVA_DIR / "data" / "UpdateChecker.kt", "UPDATES_DIR")
    home_dir = _kotlin_const(JAVA_DIR / "data" / "EmbeddedEngine.kt", "HOME_DIRECTORY")
    music_dir = _kotlin_const(
        JAVA_DIR / "ui" / "music" / "MusicLyrics.kt",
        "EMBEDDED_MUSIC_SUBDIRECTORY",
    )
    assert (by_name["updates"].tag, by_name["updates"].get("path")) == (
        "cache-path",
        f"{updates_dir}/",
    )
    assert (by_name["music"].tag, by_name["music"].get("path")) == (
        "files-path",
        f"{home_dir}/{music_dir}/",
    )


# ---------------------------------------------------------------------------
# Backup / data extraction
# ---------------------------------------------------------------------------


def _excludes(element: ET.Element) -> set[tuple[str, str]]:
    return {
        (entry.get("domain") or "", entry.get("path") or "")
        for entry in _children(element, "exclude")
    }


def test_cloud_backup_excludes_every_private_domain() -> None:
    application = _application()
    assert _android(application, "allowBackup") == "true"
    assert _android(application, "fullBackupContent") == "@xml/backup_rules"
    assert _android(application, "dataExtractionRules") == "@xml/data_extraction_rules"

    legacy_root = _parse(RES_XML_DIR / "backup_rules.xml")
    assert legacy_root.tag == "full-backup-content"
    extraction_root = _parse(RES_XML_DIR / "data_extraction_rules.xml")
    assert extraction_root.tag == "data-extraction-rules"
    cloud = _single(extraction_root, "cloud-backup")

    expected = {(domain, ".") for domain in PRIVATE_BACKUP_DOMAINS}
    assert _excludes(legacy_root) == expected, (
        "Android 11 and below would cloud-back up an app-private domain"
    )
    assert _excludes(cloud) == expected, (
        "Android 12+ would cloud-back up an app-private domain"
    )
    assert not _children(legacy_root, "include")
    assert not _children(cloud, "include"), (
        "an <include> re-admits files the excludes removed"
    )


def test_device_transfer_is_the_one_unrestricted_path_by_design() -> None:
    extraction_root = _parse(RES_XML_DIR / "data_extraction_rules.xml")
    assert not _children(extraction_root, "device-transfer"), (
        "device-to-device transfer is intentionally unrestricted: it is a local, "
        "user-initiated copy to the user's own new device, where restoring the "
        "API key and settings is expected. Narrowing it is a product decision, "
        "not a silent hardening -- update this test together with the docs."
    )


# ---------------------------------------------------------------------------
# Network security
# ---------------------------------------------------------------------------


def _trust_sources(config: ET.Element) -> list[str]:
    anchors = _children(config, "trust-anchors")
    assert len(anchors) == 1
    return [
        certificate.get("src") or ""
        for certificate in _children(anchors[0], "certificates")
    ]


def test_update_channel_refuses_cleartext_and_user_cas() -> None:
    application = _application()
    assert (
        _android(application, "networkSecurityConfig") == "@xml/network_security_config"
    )

    root = _parse(RES_XML_DIR / "network_security_config.xml")
    assert root.tag == "network-security-config"
    assert not _children(root, "debug-overrides"), (
        "debug-overrides would let a debuggable build trust a user CA at runtime"
    )

    base = _single(root, "base-config")
    expected = "true" if BASE_CLEARTEXT_PERMITTED else "false"
    assert base.get("cleartextTrafficPermitted") == expected, (
        "global cleartext policy changed; see BASE_CLEARTEXT_PERMITTED for why "
        "the product keeps loopback/LAN HTTP"
    )
    assert not _children(base, "domain")
    assert _trust_sources(base) == ["system"], (
        "a user-installed CA must never be trusted for app traffic"
    )

    domain_configs = _children(root, "domain-config")
    assert domain_configs, "the update channel must be pinned explicitly"

    by_host: dict[str, tuple[str, bool]] = {}
    for config in domain_configs:
        cleartext = config.get("cleartextTrafficPermitted")
        assert cleartext in {"true", "false"}, (
            "every domain-config must state cleartextTrafficPermitted explicitly"
        )
        assert _trust_sources(config) == ["system"]
        domains = _children(config, "domain")
        assert domains, "an empty domain-config has no effect but hides intent"
        hosts = [(domain.text or "").strip().lower() for domain in domains]
        if cleartext == "true":
            unexpected = sorted(set(hosts) - LOOPBACK_HOSTS)
            assert not unexpected, (
                "cleartext may only be re-enabled for loopback/emulator hosts; "
                f"public hosts need TLS: {unexpected}"
            )
        for domain, host in zip(domains, hosts, strict=True):
            subdomains = domain.get("includeSubdomains") == "true"
            by_host[host] = (cleartext or "", subdomains)

    # api.github.com and codeload.github.com fall under github.com;
    # objects.githubusercontent.com serves the redirected release assets.
    assert by_host.get("github.com") == ("false", True)
    assert by_host.get("githubusercontent.com") == ("false", True)


# ---------------------------------------------------------------------------
# Merged manifest (optional: only when a release build artifact exists)
# ---------------------------------------------------------------------------


def test_merged_release_manifest_exports_only_reviewed_components() -> None:
    """Audit the manifest actually packaged by AGP, including library merges.

    The source manifest cannot show components contributed by dependencies.
    The check is skipped when no release artifact has been built; it is a
    second line of defence, not a replacement for the source checks above.
    """
    candidates = sorted(
        (
            ANDROID_DIR
            / "app"
            / "build"
            / "intermediates"
            / "merged_manifest"
            / "release"
        ).rglob("AndroidManifest.xml")
    )
    if not candidates:
        pytest.skip("release merged manifest not built yet")

    application = _single(_parse(candidates[-1]), "application")
    assert _android(application, "debuggable") in {None, "false"}

    exported = {
        (tag, _android(element, "name") or "<unnamed>"): _android(element, "permission")
        for tag, element in _components(application)
        if _android(element, "exported") == "true"
    }
    unexpected = sorted(set(exported) - set(MERGED_RELEASE_EXPORTED))
    assert not unexpected, (
        "dependencies exported components without a review: "
        f"{unexpected}. Add them to MERGED_RELEASE_EXPORTED only with a "
        "protection rationale."
    )
    for component, required_permission in MERGED_RELEASE_EXPORTED.items():
        if required_permission is None:
            continue
        assert exported.get(component) == required_permission, (
            f"{component} is exported without {required_permission}"
        )

    names = {name for _, name in exported}
    assert "androidx.compose.ui.tooling.PreviewActivity" not in names, (
        "Compose tooling is a debugImplementation and must not ship in release"
    )
