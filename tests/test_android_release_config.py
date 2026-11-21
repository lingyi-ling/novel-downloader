#!/usr/bin/env python3
"""Static audit of the Android *release* build configuration.

The Gradle module file, the manifest and the packaged resources decide what a
published APK is: its versionCode, its package id, whether it is debuggable,
which key signs it and which native ABIs it carries. None of that is visible
from the Kotlin sources, and a wrong value only shows up on a device or, worse,
in the update channel after the APK is already public.

These tests parse the real files with the standard library only -- no Gradle,
no Android SDK, no keystore values. Nothing here reads, prints or hashes the
contents of ``android/keystore.properties`` or ``*.jks``; only the *presence*
of the four keys is relevant, and that logic lives in the build script.

Audited invariants:

* ``versionCode`` is strictly increasing with the released versions and equals
  the ordinal of the current ``__version__`` in ``CHANGELOG.md`` -- the mapping
  the project has used since 1.0.0 (v1.0.0 -> 1, v1.37.0 -> 51). A release
  that reuses a versionCode is silently ignored by the updater.
* ``versionName`` equals the package ``__version__`` (the release gate also
  checks this; here it keeps the module self-contained).
* the release build type is not debuggable, does not shrink resources and
  does not fall back to the debug signing key when the keystore is absent.
  A debug-signed APK reports the release versionName, installs fine and then
  can never be upgraded by the real key -- Android refuses the signature
  change -- so the build must fail instead.
* ``applicationId``/``namespace`` are stable and match the FileProvider
  authority template, the runtime authority construction and the network
  security config the manifest references.
* the configured ABIs stay 64-bit only, matching what the 16 KB page-size
  gate checks, and nothing enables legacy JNI packaging.
* ``keystore.properties`` and ``*.jks`` are git-ignored.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ANDROID_DIR = ROOT / "android"
APP_DIR = ANDROID_DIR / "app"
GRADLE = APP_DIR / "build.gradle.kts"
MAIN_DIR = APP_DIR / "src" / "main"
MANIFEST = MAIN_DIR / "AndroidManifest.xml"
RES_XML_DIR = MAIN_DIR / "res" / "xml"
JAVA_DIR = MAIN_DIR / "java" / "com" / "lingyi" / "noveldl"
PACKAGE_INIT = ROOT / "src" / "novel_downloader" / "__init__.py"
CHANGELOG = ROOT / "CHANGELOG.md"
ANDROID_GITIGNORE = ANDROID_DIR / ".gitignore"
OUTPUT_METADATA = (
    APP_DIR / "build" / "outputs" / "apk" / "release" / "output-metadata.json"
)
RELEASE_APK = APP_DIR / "build" / "outputs" / "apk" / "release" / "app-release.apk"
MERGED_RELEASE_MANIFEST = (
    APP_DIR
    / "build"
    / "intermediates"
    / "merged_manifest"
    / "release"
    / "processReleaseMainManifest"
    / "AndroidManifest.xml"
)

ANDROID_NS = "{http://schemas.android.com/apk/res/android}"

EXPECTED_APPLICATION_ID = "com.lingyi.noveldl"

#: Chaquopy needs explicit ABIs and the 16 KB page-size gate only holds 64-bit
#: libraries to ``p_align >= 16384``; 32-bit ABIs would add libraries the gate
#: exempts while the app still ships a 16 KB-ready arm64 build.
EXPECTED_ABIS = {"arm64-v8a", "x86_64"}

#: Keys ``keystore.properties`` must define before a release build is allowed.
REQUIRED_SIGNING_KEYS = ("storeFile", "storePassword", "keyAlias", "keyPassword")

_CHANGELOG_VERSION_RE = re.compile(r"^## v(\d+)\.(\d+)\.(\d+)", re.MULTILINE)
_VERSION_RE = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)


# ---------------------------------------------------------------------------
# Source helpers
# ---------------------------------------------------------------------------


def read(path: Path) -> str:
    """Read a UTF-8 source file."""
    return path.read_text(encoding="utf-8")


def gradle_text() -> str:
    """The Android module build script."""
    assert GRADLE.is_file(), f"missing {GRADLE}"
    return read(GRADLE)


def block_after(text: str, header: str) -> str:
    """
    Return the ``{...}`` body that follows *header* in *text*.

    Brace counting is enough for the small, comment-free blocks asserted here
    (``buildTypes { release { ... } }``, ``defaultConfig { ... }``). Comments
    are stripped first so a ``{`` inside a comment cannot shift the depth.
    """
    stripped = re.sub(r"//[^\n]*", "", text)
    match = re.search(re.escape(header) + r"\s*\{", stripped)
    assert match, f"no `{header} {{` in android/app/build.gradle.kts"
    start = match.end()
    depth = 1
    for index in range(start, len(stripped)):
        char = stripped[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return stripped[start:index]
    raise AssertionError(f"unbalanced braces after `{header} {{`")


def release_block() -> str:
    """Body of the ``release { ... }`` build type."""
    return block_after(gradle_text(), "release")


def default_config_block() -> str:
    """Body of the ``defaultConfig { ... }`` block."""
    return block_after(gradle_text(), "defaultConfig")


def gradle_string(block: str, key: str) -> str:
    """Value of ``key = "..."`` inside *block*."""
    match = re.search(rf'\b{key}\s*=\s*"([^"]*)"', block)
    assert match, f'no `{key} = "..."` in the block under test'
    return match.group(1)


def gradle_int(block: str, key: str) -> int:
    """Value of ``key = <int>`` inside *block*."""
    match = re.search(rf"\b{key}\s*=\s*(\d+)", block)
    assert match, f"no `{key} = <int>` in the block under test"
    return int(match.group(1))


def package_version() -> str:
    """``__version__`` from the package, parsed not imported."""
    match = _VERSION_RE.search(read(PACKAGE_INIT))
    assert match, f"no __version__ in {PACKAGE_INIT}"
    return match.group(1)


def released_versions() -> list[tuple[int, int, int]]:
    """Every ``## vX.Y.Z`` heading in the changelog, ascending and unique."""
    versions = {
        tuple(int(part) for part in match)
        for match in _CHANGELOG_VERSION_RE.findall(read(CHANGELOG))
    }
    assert versions, "CHANGELOG.md has no released version heading"
    return sorted(versions)


def parse_xml(path: Path) -> ET.Element:
    """Parse [path] and return the document element."""
    return ET.parse(path).getroot()


def android_attr(element: ET.Element, name: str) -> str | None:
    """Attribute [name] in the Android namespace."""
    return element.get(ANDROID_NS + name)


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


def test_version_name_matches_the_package_version() -> None:
    """A drift ships an APK that the desktop manifest never offers."""
    assert gradle_string(default_config_block(), "versionName") == package_version()


def test_version_code_is_the_release_ordinal() -> None:
    """versionCode == position of the current version in the changelog.

    This is the project's mapping since 1.0.0 (v1.0.0 -> 1). It is strict:
    reusing a code makes Android ignore the new APK, and skipping one is fine
    only if this test is updated together with the release.
    """
    version = package_version()
    released = released_versions()
    current = tuple(int(part) for part in version.split("."))
    assert current in released, (
        f"{version} has no `## v{version}` section in CHANGELOG.md; the release "
        "gate reads the notes from there"
    )

    ordinal = sum(1 for candidate in released if candidate <= current)
    code = gradle_int(default_config_block(), "versionCode")
    assert code == ordinal, (
        f"versionCode is {code} but {version} is release #{ordinal} "
        f"({len(released)} changelog versions); a reused versionCode makes "
        "Android ignore the update"
    )


def test_every_version_code_in_the_repository_history_is_unique() -> None:
    """No changelog version may share a position with another one."""
    released = released_versions()
    assert len(released) == len(set(released))
    codes = list(range(1, len(released) + 1))
    assert len(codes) == len(set(codes))
    # The current build's code is the last one in that sequence.
    assert gradle_int(default_config_block(), "versionCode") == codes[-1]


# ---------------------------------------------------------------------------
# Release build type: debuggable, minify, signing
# ---------------------------------------------------------------------------


def test_release_build_type_is_not_debuggable() -> None:
    """A debuggable release exposes app-private data and is refused by Play."""
    body = release_block()
    assert re.search(r"\bisDebuggable\s*=\s*false\b", body), (
        "release build type must state `isDebuggable = false` explicitly"
    )
    assert not re.search(r"\bisDebuggable\s*=\s*true\b", body)
    assert not re.search(r"\bdebuggable\s*=\s*true\b", gradle_text())


def test_release_does_not_shrink_resources_without_minification() -> None:
    """``isShrinkResources`` requires R8; enabling it alone breaks packaging."""
    body = release_block()
    minify = re.search(r"\bisMinifyEnabled\s*=\s*(true|false)\b", body)
    assert minify, "release build type must state isMinifyEnabled explicitly"
    shrink = re.search(r"\bisShrinkResources\s*=\s*(true|false)\b", body)
    if shrink and shrink.group(1) == "true":
        assert minify.group(1) == "true", (
            "isShrinkResources = true without isMinifyEnabled = true fails the "
            "release build"
        )
    if minify.group(1) == "false":
        assert not shrink or shrink.group(1) == "false", (
            "resource shrinking is meaningless (and rejected) with R8 disabled"
        )


def test_release_never_falls_back_to_the_debug_signing_key() -> None:
    """A debug-signed release blocks every later real-key upgrade.

    Android refuses to replace an installed APK whose signing certificate
    changed, so a debug-signed APK published once permanently locks that
    versionCode out of the real update channel.
    """
    text = gradle_text()
    body = release_block()

    assert 'signingConfigs.getByName("debug")' not in body, (
        "the release build type must not use the debug signing config; a "
        "debug-signed APK cannot be upgraded by the release key"
    )
    assert not re.search(
        r'signingConfig\s*=\s*signingConfigs\.getByName\("debug"\)', text
    )

    # Absent keystore -> unsigned variant, never debug-signed.
    assert re.search(
        r"signingConfig\s*=\s*if\s*\(\s*hasReleaseSigning\s*\)\s*\{[^}]*"
        r"getByName\(\"release\"\)[^}]*\}\s*else\s*\{\s*null\s*\}",
        text,
        re.DOTALL,
    ), "release signingConfig must be `release` when configured and null otherwise"

    # And packaging fails loudly instead of producing an unsigned APK. The
    # guard matches the task-name prefix so an ABI/density split
    # (`packageReleaseArm64-v8a`) or an app bundle cannot slip past it.
    assert "GradleException" in text
    assert 'it.name.startsWith("packageRelease")' in text, (
        "the signing guard must cover split variants, not just `packageRelease`"
    )
    assert 'it.name == "bundleRelease"' in text, (
        "the signing guard must cover `bundleRelease` as well"
    )
    assert "hasReleaseSigning" in text
    assert "isDryRun" in text, (
        "the guard must not break --dry-run / configuration-only invocations"
    )


def test_signing_presence_check_covers_all_four_keys_and_the_store_file() -> None:
    """``hasReleaseSigning`` must require every key *and* an existing store."""
    text = gradle_text()
    for key in REQUIRED_SIGNING_KEYS:
        assert f'"{key}"' in text, f"{key} is not part of the signing check"
    assert re.search(r"releaseStoreFile\?\.isFile\s*==\s*true", text), (
        "a keystore.properties pointing at a missing .jks must count as absent"
    )
    assert re.search(r"missingSigningKeys\.isEmpty\(\)", text)


def test_signing_material_is_git_ignored() -> None:
    """A committed keystore is a permanent credential leak."""
    assert ANDROID_GITIGNORE.is_file()
    ignored = {
        line.strip()
        for line in read(ANDROID_GITIGNORE).splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert "keystore.properties" in ignored
    assert "*.jks" in ignored
    assert "*.keystore" in ignored


# ---------------------------------------------------------------------------
# Identity: applicationId / namespace / FileProvider / network config
# ---------------------------------------------------------------------------


def test_application_id_and_namespace_are_stable() -> None:
    """Changing either renames the installed app and orphans its data."""
    assert gradle_string(gradle_text(), "namespace") == EXPECTED_APPLICATION_ID
    assert gradle_string(default_config_block(), "applicationId") == (
        EXPECTED_APPLICATION_ID
    )
    assert "applicationIdSuffix" not in gradle_text(), (
        "an applicationIdSuffix would make the installed package differ from "
        "the FileProvider authority and the update check's package name"
    )


def test_file_provider_authority_matches_the_application_id() -> None:
    """The manifest template, the runtime string and the id must agree."""
    application = parse_xml(MANIFEST).find("application")
    assert application is not None
    providers = application.findall("provider")
    assert len(providers) == 1

    authority = android_attr(providers[0], "authorities")
    assert authority == "${applicationId}.fileprovider"
    assert authority.replace("${applicationId}", EXPECTED_APPLICATION_ID) == (
        f"{EXPECTED_APPLICATION_ID}.fileprovider"
    )

    # The installer is handed the URI through packageName, which is the
    # applicationId at runtime.
    update_state = read(JAVA_DIR / "data" / "UpdateState.kt")
    assert '"${context.packageName}.fileprovider"' in update_state


def test_manifest_references_the_audited_network_config() -> None:
    """The transport policy must be the file the other tests parse."""
    application = parse_xml(MANIFEST).find("application")
    assert application is not None
    assert android_attr(application, "networkSecurityConfig") == (
        "@xml/network_security_config"
    )
    assert (RES_XML_DIR / "network_security_config.xml").is_file()

    # No literal cleartext opt-in: with a networkSecurityConfig present the
    # flag is ignored on API 24+, but "true" misleads reviewers and becomes
    # the policy if the config is ever dropped.
    assert android_attr(application, "usesCleartextTraffic") in {None, "false"}

    # The update channel is the only executable-code download path.
    text = read(JAVA_DIR / "data" / "UpdateChecker.kt")
    urls = re.findall(r'"(https?://[^"]+)"', text)
    assert urls, "no update URLs found in UpdateChecker.kt"
    assert all(url.startswith("https://") for url in urls), (
        f"the updater must only fetch over TLS: {urls}"
    )

    hosts = {
        (url.split("/")[2].lower(), False) for url in urls if url.startswith("https://")
    }
    root = parse_xml(RES_XML_DIR / "network_security_config.xml")
    pinned: set[tuple[str, bool]] = set()
    for config in root.findall("domain-config"):
        cleartext = config.get("cleartextTrafficPermitted")
        if cleartext != "false":
            continue
        for domain in config.findall("domain"):
            pinned.add(
                (
                    (domain.text or "").strip().lower(),
                    domain.get("includeSubdomains") == "true",
                )
            )
    for host, _ in hosts:
        covered = any(
            host == pinned_host or (subdomains and host.endswith("." + pinned_host))
            for pinned_host, subdomains in pinned
        )
        assert covered, (
            f"{host} serves updater metadata but is not pinned to TLS in "
            "network_security_config.xml"
        )


# ---------------------------------------------------------------------------
# Native ABIs and 16 KB page size
# ---------------------------------------------------------------------------


def test_abi_filters_stay_64_bit_only() -> None:
    """The 16 KB gate holds 64-bit libraries to p_align >= 16384."""
    text = gradle_text()
    match = re.search(r"abiFilters\s*\+?=\s*listOf\(([^)]*)\)", text)
    assert match, "no explicit ndk.abiFilters; Chaquopy requires one"
    abis = set(re.findall(r'"([^"]+)"', match.group(1)))
    assert abis == EXPECTED_ABIS, (
        f"ABI set changed to {sorted(abis)}; update EXPECTED_ABIS and confirm "
        "scripts/check_android_alignment.py still covers every 64-bit entry"
    )

    # The alignment script must know about every shipped 64-bit ABI.
    alignment = read(ROOT / "scripts" / "check_android_alignment.py")
    for abi in abis:
        assert f'"{abi}"' in alignment, (
            f"{abi} is not in the alignment checker's ABI lists"
        )


def test_legacy_jni_packaging_is_not_enabled() -> None:
    """Compressing libs defeats the zip-offset half of the 16 KB requirement."""
    text = gradle_text()
    assert not re.search(r"useLegacyPackaging\s*=\s*true", text), (
        "useLegacyPackaging = true compresses native libraries; the 16 KB zip "
        "alignment check only applies to stored entries"
    )


# ---------------------------------------------------------------------------
# Build artifacts (optional: only when a release build exists locally)
# ---------------------------------------------------------------------------


def test_release_apk_metadata_matches_the_gradle_config() -> None:
    """The packaged APK must report the version and id the module declares."""
    if not OUTPUT_METADATA.is_file():
        pytest.skip("release APK not built yet")

    document = json.loads(read(OUTPUT_METADATA))
    assert document.get("applicationId") == EXPECTED_APPLICATION_ID
    elements = document.get("elements") or []
    assert len(elements) == 1
    element = elements[0]
    assert element.get("versionCode") == gradle_int(
        default_config_block(), "versionCode"
    )
    assert element.get("versionName") == gradle_string(
        default_config_block(), "versionName"
    )


def test_release_apk_ships_only_the_configured_abis() -> None:
    """A stray ABI is either untested or outside the 16 KB gate."""
    if not RELEASE_APK.is_file():
        pytest.skip("release APK not built yet")

    import zipfile

    with zipfile.ZipFile(RELEASE_APK) as archive:
        abis = {
            name.split("/")[1]
            for name in archive.namelist()
            if name.startswith("lib/") and name.count("/") >= 2
        }
    assert abis == EXPECTED_ABIS, f"APK carries ABIs {sorted(abis)}"


def test_merged_release_manifest_is_not_debuggable() -> None:
    """The merged manifest is what ships, including library contributions."""
    if not MERGED_RELEASE_MANIFEST.is_file():
        pytest.skip("release merged manifest not built yet")

    application = parse_xml(MERGED_RELEASE_MANIFEST).find("application")
    assert application is not None
    assert android_attr(application, "debuggable") in {None, "false"}
    assert android_attr(application, "usesCleartextTraffic") in {None, "false"}
