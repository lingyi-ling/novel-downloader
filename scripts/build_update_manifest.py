#!/usr/bin/env python3
"""
Build the updater manifest (``latest.json``) for a release.

Why a manifest at all
---------------------
The clients can already ask the GitHub API for ``releases/latest``.  That
works, but it has three drawbacks:

* it is rate limited (60 requests/hour per IP without a token), which the
  desktop, CLI and Android clients all share;
* the response is large and changes shape over time;
* an updater has to re-derive "which asset is mine" from a list of names.

A small, stable document fixes all three: one HTTPS GET, an explicit per
platform entry with the size and SHA-256 the client must verify, and a URL
that never changes (``releases/latest/download/latest.json``).

Design decisions (borrowed from how larger release pipelines do it)
-------------------------------------------------------------------
* **Rebuilt from the actual assets, never from build-time state.**  The
  producer of an asset is the last thing that should be trusted about what
  was published; the release payload is the source of truth.
* **Package-shape specific keys** (``windows-x64-installer`` vs
  ``windows-x64-portable``) so an installed copy updates to the same shape it
  was installed from.
* **Strict equality in ``--check``** so a stale manifest is a hard failure
  rather than a silent downgrade.
* **Canonical JSON** (sorted keys, no insignificant whitespace, UTF-8) so the
  file is reproducible and can be signed byte-for-byte later.  An optional
  Ed25519 signature covers every field except ``signature``/``key_id``
  itself -- including each platform's ``name``, ``url``, ``sha256`` and
  ``size`` -- and clients verify those exact canonical bytes.

Usage::

    python scripts/build_update_manifest.py \\
        --release-json payload.json --version 1.4.1 --tag v1.4.1 \\
        --repo lingyi-ling/novel-downloader --out latest.json
    python scripts/build_update_manifest.py ... --check latest.json
    python scripts/build_update_manifest.py --keygen ~/.novel_downloader/update.key
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import hmac
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

SCHEMA_VERSION = 1

#: How a release asset maps onto a client target key.
#:
#: Order matters: the first match wins, so the installer is checked before the
#: portable archive.  The key is ``<platform>[-<shape>]`` and the platform part
#: must match ``updater.detect_platform()`` exactly, so a client can look up
#: ``windows-x64-installer`` before falling back to ``windows-x64-portable``.
ASSET_RULES: tuple[tuple[str, str], ...] = (
    ("NovelDownloader-setup-", "installer"),
    ("NovelDownloader-", "portable"),
    ("novel-cli-", "cli"),
    ("novel-api-", "api"),
    ("novel-downloader-", "android"),
)

#: Architecture tokens that appear in our asset names.
_ARCH_TOKENS = ("arm64", "aarch64", "x64", "amd64", "x86_64")

#: OS tokens as they appear in ``build_release.py``'s platform tag.
_OS_TOKENS: tuple[tuple[str, str], ...] = (
    ("windows", "windows"),
    ("linux", "linux"),
    ("macos", "macos"),
    ("darwin", "macos"),
)

#: ``novel-downloader-<ver>-android-<stem>.apk`` — the ABI/variant suffix.
_APK_VARIANT_RE = re.compile(r"-android-([A-Za-z0-9][A-Za-z0-9._-]*)")

#: Android ABI spellings as they appear in split APK names.
_APK_ABIS: tuple[tuple[str, str], ...] = (
    ("arm64-v8a", "arm64-v8a"),
    ("armeabi-v7a", "armeabi-v7a"),
    ("x86_64", "x86_64"),
    ("universal", "universal"),
)


class ManifestError(RuntimeError):
    """Raised when the manifest cannot be built from the given assets."""


@dataclass(frozen=True)
class ManifestAsset:
    """One downloadable file that a client may update from."""

    key: str
    name: str
    url: str
    sha256: str
    size: int

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "url": self.url,
            "sha256": self.sha256,
            "size": self.size,
        }


def canonical_bytes(payload: dict[str, Any]) -> bytes:
    """Serialise *payload* reproducibly: sorted keys, no extra whitespace."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _asset_digest(item: dict[str, Any]) -> str | None:
    """Read the ``sha256:<hex>`` digest GitHub computes for an asset."""
    value = item.get("digest")
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    if value.startswith("sha256:"):
        value = value[len("sha256:") :]
    return value if len(value) == 64 else None


def _os_of(name: str, shape: str) -> str | None:
    """Infer the target OS from an asset name."""
    lowered = name.lower()
    for token, platform in _OS_TOKENS:
        if token in lowered:
            return platform
    # The Inno Setup installer carries no OS token: it is Windows-only.
    if shape == "installer" and lowered.endswith(".exe"):
        return "windows"
    return None


def _arch_of(name: str) -> str | None:
    """Normalise the architecture token in an asset name."""
    lowered = name.lower()
    arch = next((token for token in _ARCH_TOKENS if token in lowered), None)
    if arch is None:
        return None
    return "arm64" if arch in {"arm64", "aarch64"} else "x64"


def _classify(name: str) -> str | None:
    """
    Map an asset name onto a client target key, if it is an update payload.

    ``NovelDownloader-1.4.1-windows-x64.zip`` becomes
    ``windows-x64-portable``; a single Android APK becomes ``android`` and a
    per-ABI split becomes ``android-<stem>`` so several APKs can coexist.
    """
    if name in {"latest.json", "manifest.json"} or name.startswith("SHA256"):
        return None
    for prefix, shape in ASSET_RULES:
        if not name.startswith(prefix):
            continue
        if shape == "android":
            if not name.endswith(".apk"):
                return None
            variant = _APK_VARIANT_RE.search(name)
            if variant is None:
                return "android"
            stem = variant.group(1)
            lowered = stem.lower()
            for token, abi in _APK_ABIS:
                if token in lowered:
                    return f"android-{abi}"
            # Unknown split: keep the stem so two APKs still never collide.
            return "android-" + re.sub(r"[^A-Za-z0-9._-]+", "-", stem)
        platform_tag = _os_of(name, shape)
        arch = _arch_of(name)
        if platform_tag is None or arch is None:
            return None
        return f"{platform_tag}-{arch}-{shape}"
    return None


def classify_asset(name: str) -> str | None:
    """
    Public wrapper around :func:`_classify`.

    The release script re-derives the target key from each asset name so a
    manifest entry cannot point at a file of a different shape; keeping the
    rule in one place means the producer and the verifier cannot drift apart.
    """
    return _classify(name)


def build_manifest(
    release: dict[str, Any],
    *,
    version: str,
    tag: str,
    repo: str,
    notes_url: str | None = None,
    pub_date: str | None = None,
) -> dict[str, Any]:
    """
    Build the manifest document from a GitHub release payload.

    :raises ManifestError: when the release has no usable payload assets or a
        payload is missing its digest (an unverifiable download must never be
        advertised to clients).
    """
    raw_assets = release.get("assets")
    if not isinstance(raw_assets, list) or not raw_assets:
        raise ManifestError("release payload has no assets")

    platforms: dict[str, dict[str, Any]] = {}
    for item in raw_assets:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            continue
        key = _classify(name)
        if key is None:
            continue
        url = item.get("browser_download_url")
        if not isinstance(url, str) or not url.startswith("https://"):
            raise ManifestError(f"{name}: missing https download url")
        digest = _asset_digest(item)
        if digest is None:
            raise ManifestError(f"{name}: GitHub has not published a sha256 digest yet")
        size = item.get("size")
        if not isinstance(size, int) or size <= 0:
            raise ManifestError(f"{name}: missing or zero size")
        if key in platforms:
            raise ManifestError(
                f"{name}: a second asset maps to target {key!r} "
                f"({platforms[key]['name']})"
            )
        platforms[key] = ManifestAsset(
            key=key,
            name=name,
            url=url,
            sha256=digest,
            size=size,
        ).to_json()

    if not platforms:
        raise ManifestError("release has no updatable assets")

    document: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "version": version,
        "tag": tag,
        "repo": repo,
        "generated_from": release.get("html_url") or f"https://github.com/{repo}",
        "platforms": platforms,
    }
    if notes_url:
        document["notes_url"] = notes_url
    if pub_date:
        document["pub_date"] = pub_date
    return document


def manifest_bytes(document: dict[str, Any]) -> bytes:
    """Canonical serialisation with a trailing newline."""
    return canonical_bytes(document) + b"\n"


def expected_url(repo: str, tag: str, name: str) -> str:
    """The canonical ``releases/download`` URL for an asset."""
    return (
        f"https://github.com/{repo}/releases/download/"
        f"{quote(tag, safe='')}/{quote(name, safe='')}"
    )


def diff_manifests(actual: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    """Human-readable differences, empty when the two documents agree."""
    problems: list[str] = []
    for field in sorted(set(actual) | set(expected)):
        if field in {"platforms", "signature", "key_id"}:
            continue
        if actual.get(field) != expected.get(field):
            problems.append(
                f"{field}: expected {expected.get(field)!r}, "
                f"found {actual.get(field)!r}"
            )
    actual_platforms = actual.get("platforms")
    expected_platforms = expected.get("platforms")
    if not isinstance(actual_platforms, dict):
        problems.append("platforms: missing or not an object")
        return problems
    assert isinstance(expected_platforms, dict)
    for key in sorted(set(actual_platforms) | set(expected_platforms)):
        if key not in actual_platforms:
            problems.append(f"platforms.{key}: missing")
            continue
        if key not in expected_platforms:
            problems.append(f"platforms.{key}: unexpected")
            continue
        if actual_platforms[key] != expected_platforms[key]:
            problems.append(f"platforms.{key}: differs")
    return problems


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifestError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError(f"{path}: expected a JSON object")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build (or verify) the updater manifest for a release."
    )
    parser.add_argument(
        "--release-json",
        type=Path,
        default=None,
        help="GitHub release payload (a file containing the API response)",
    )
    parser.add_argument("--version", default=None)
    parser.add_argument("--tag", default=None)
    parser.add_argument("--repo", default=None, help="owner/name")
    parser.add_argument("--notes-url", default=None)
    parser.add_argument("--pub-date", default=None)
    parser.add_argument(
        "--out", type=Path, default=None, help="write the manifest here"
    )
    parser.add_argument(
        "--check",
        type=Path,
        default=None,
        metavar="EXISTING",
        help="compare against an existing manifest instead of writing",
    )
    parser.add_argument(
        "--sign-key",
        type=Path,
        default=None,
        metavar="PATH",
        help="sign the manifest with the 32-byte Ed25519 seed in PATH",
    )
    parser.add_argument(
        "--keygen",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "write a fresh Ed25519 seed to PATH and print its public key; "
            "refuses to replace an existing file"
        ),
    )
    parser.add_argument(
        "--public-key",
        default=None,
        metavar="HEX",
        help="with --check, also verify the existing manifest signature",
    )
    args = parser.parse_args(argv)

    if args.keygen is not None:
        seed, public = generate_keypair()
        identifier = key_id(public)
        try:
            args.keygen.parent.mkdir(parents=True, exist_ok=True)
            # ``O_EXCL`` refuses to replace an existing seed: it is the only
            # way to sign future manifests, so clobbering it would strand
            # every client that trusts the matching public key.  ``0o600`` is
            # applied at creation on POSIX so the file is never group/world
            # readable, even for an instant.
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            descriptor = os.open(args.keygen, flags, 0o600)
            try:
                with os.fdopen(
                    descriptor, "w", encoding="ascii", newline="\n"
                ) as handle:
                    handle.write(seed + chr(10))
            except OSError:
                with contextlib.suppress(OSError):
                    args.keygen.unlink()
                raise
            # Windows ignores the creation mode; re-assert the intended
            # permissions where the platform honours them.
            args.keygen.chmod(0o600)
        except FileExistsError:
            print(
                f"error: refusing to overwrite the existing key file "
                f"{args.keygen} (move it away first)",
                file=sys.stderr,
            )
            return 1
        except OSError as exc:
            print(f"error: cannot write {args.keygen}: {exc}", file=sys.stderr)
            return 1
        print(f"private seed: {args.keygen} (keep it out of the repository)")
        print(f"public key  : {public}")
        print(f"key id      : {identifier}")
        print("trusted key entry for updater.TRUSTED_UPDATE_KEYS:")
        print(f'    "{identifier}": "{public}",')
        return 0

    missing = [
        flag
        for flag, value in (
            ("--release-json", args.release_json),
            ("--version", args.version),
            ("--tag", args.tag),
            ("--repo", args.repo),
        )
        if value in (None, "")
    ]
    if missing:
        parser.error("the following arguments are required: " + ", ".join(missing))

    try:
        release = _load_json(args.release_json)
        document = build_manifest(
            release,
            version=args.version,
            tag=args.tag,
            repo=args.repo,
            notes_url=args.notes_url,
            pub_date=args.pub_date,
        )
    except ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # Every advertised URL must point at the tag being released, otherwise a
    # manifest copied between releases would send clients to the wrong file.
    for key, entry in document["platforms"].items():
        want = expected_url(args.repo, args.tag, entry["name"])
        if entry["url"] != want:
            print(
                f"error: platforms.{key}.url is {entry['url']!r}, expected {want!r}",
                file=sys.stderr,
            )
            return 1

    if args.sign_key is not None:
        try:
            seed = args.sign_key.read_text(encoding="ascii").strip()
            document = sign_document(document, seed)
        except (OSError, ManifestError) as exc:
            print(f"error: cannot sign: {exc}", file=sys.stderr)
            return 1

    if args.check is not None:
        try:
            existing = _load_json(args.check)
        except ManifestError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if args.public_key is not None and not verify_document(
            existing, args.public_key
        ):
            print(
                "stale: the existing manifest signature does not verify against "
                "the given public key",
                file=sys.stderr,
            )
            return 1
        problems = diff_manifests(existing, document)
        if problems:
            for problem in problems:
                print(f"stale: {problem}", file=sys.stderr)
            return 1
        print(f"manifest is up to date ({len(document['platforms'])} platform(s))")
        return 0

    if args.out is None:
        print("error: pass --out or --check", file=sys.stderr)
        return 2
    try:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        temp = args.out.with_name(f"{args.out.name}.tmp")
        temp.write_bytes(manifest_bytes(document))
        temp.replace(args.out)
    except OSError as exc:
        print(f"error: cannot write {args.out}: {exc}", file=sys.stderr)
        return 1
    print(
        f"wrote {args.out} ({len(document['platforms'])} platform(s), "
        f"v{document['version']})"
    )
    return 0


# ---------------------------------------------------------------------------
# signing (optional)
# ---------------------------------------------------------------------------

#: Length of the ``key_id`` prefix (hex chars of the public-key digest).
_KEY_ID_LEN = 16


def key_id(public_hex: str) -> str:
    """A short, stable identifier for a public key."""
    return hashlib.sha256(bytes.fromhex(public_hex)).hexdigest()[:_KEY_ID_LEN]


def _signing_bytes(document: dict[str, Any]) -> bytes:
    """
    The bytes a signature covers: the document without its signature fields.

    Every other top-level field is signed, and because :func:`canonical_bytes`
    sorts nested objects too, each ``platforms.<key>`` entry is signed in full
    (``name``, ``url``, ``sha256``, ``size``).  Retargeting a download or
    swapping a digest therefore always invalidates the signature.
    ``key_id`` is deliberately excluded: it is only the selector a client uses
    to look the public key up, not part of the trusted payload.
    """
    payload = {k: v for k, v in document.items() if k not in {"signature", "key_id"}}
    return canonical_bytes(payload)


def generate_keypair() -> tuple[str, str]:
    """Return ``(private_seed_hex, public_key_hex)`` for a fresh Ed25519 key."""
    from Crypto.PublicKey import ECC

    key = ECC.generate(curve="ed25519")
    seed = key.seed.hex()
    public = key.public_key().export_key(format="raw").hex()
    return seed, public


def sign_document(document: dict[str, Any], private_seed_hex: str) -> dict[str, Any]:
    """
    Return a copy of *document* with ``signature`` and ``key_id`` added.

    The signature covers the canonical JSON of the document **without** those
    two fields, so a client can recompute the same bytes after parsing.  All
    other fields, ``platforms`` included, are authenticated by it.
    """
    from Crypto.Signature import eddsa

    try:
        seed = bytes.fromhex(private_seed_hex.strip())
    except ValueError as exc:
        raise ManifestError(f"signing key is not hex: {exc}") from exc
    if len(seed) != 32:
        raise ManifestError(
            f"signing key must be a 32-byte seed (64 hex chars), got {len(seed)}"
        )

    private = eddsa.import_private_key(seed)
    signature = eddsa.new(private, "rfc8032").sign(_signing_bytes(document))
    public_hex = private.public_key().export_key(format="raw").hex()

    signed = dict(document)
    signed["key_id"] = key_id(public_hex)
    signed["signature"] = base64.b64encode(signature).decode("ascii")
    return signed


def verify_document(document: dict[str, Any], public_hex: str) -> bool:
    """
    Whether *document* carries a valid signature from *public_hex*.

    The recorded ``key_id`` must also name *public_hex*: a client resolves the
    key by that id, so a valid signature under a different id would be refused
    by the client.  Checking both here keeps ``--check --public-key`` in step
    with :func:`novel_downloader.apps.common.updater.verify_manifest_signature`.

    :param document: a parsed manifest.
    :param public_hex: the Ed25519 public key to verify against, in hex.
    :return: ``True`` only for a signature this key made over this document.
    """
    from Crypto.Signature import eddsa

    encoded = document.get("signature")
    claimed = document.get("key_id")
    if not isinstance(encoded, str) or not isinstance(claimed, str):
        return False
    try:
        digest = key_id(public_hex.strip())
        if not hmac.compare_digest(claimed, digest):
            return False
        signature = base64.b64decode(encoded, validate=True)
        public = eddsa.import_public_key(bytes.fromhex(public_hex.strip()))
        eddsa.new(public, "rfc8032").verify(_signing_bytes(document), signature)
    except Exception:  # noqa: BLE001 - any failure means "not verified"
        return False
    return True


# Kept last: ``main`` reaches symbols defined throughout this module, so the
# entry point must only run after the whole file has been executed.
if __name__ == "__main__":
    raise SystemExit(main())
