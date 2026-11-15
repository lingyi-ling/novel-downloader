#!/usr/bin/env python3
"""
tests.apps.test_update_manifest_signature
-----------------------------------------

Contract tests for ``updater.verify_manifest_signature`` and for the "pinned
manifest" path of :func:`updater.check_for_update`.

Signed format
-------------

``scripts/build_update_manifest.py`` produces ``latest.json`` as canonical
JSON (sorted keys, ``","``/``":"`` separators, UTF-8, no ASCII escaping) and an
optional Ed25519 signature:

* the private key is a 32-byte seed, hex encoded (``--keygen`` writes it);
* the public key is the 32-byte raw Ed25519 public key, hex encoded, and is
  pasted into ``updater.TRUSTED_UPDATE_KEYS``;
* ``key_id`` is ``sha256(public_key_bytes).hexdigest()[:16]``;
* ``signature`` is the base64 of the 64-byte RFC 8032 signature over the
  canonical bytes of the document **without** its ``signature`` and ``key_id``
  fields.  Every other field -- ``schema``, ``version``, ``tag``, ``repo``,
  ``generated_from``, ``notes_url``, ``pub_date`` and each
  ``platforms.<key>`` entry with its ``name``/``url``/``sha256``/``size`` -- is
  covered.

Trust model exercised here
--------------------------

While ``TRUSTED_UPDATE_KEYS`` is empty (the shipped default) signatures are not
checked at all and the GitHub API remains the fallback, exactly as before
signing existed.  As soon as one key is configured the manifest becomes the
only trust anchor: an unsigned manifest, one signed by an unknown key, or one
whose payload was altered is refused, and the client must **not** reach the
API -- deleting the manifest (or stripping its signature) would otherwise
hand the attacker the API path they also control.

No test performs network I/O: manifests are either handed to a recording
``opener`` or served by a fake ``urlopen`` that replays bytes written to
``tmp_path``.  Key pairs are generated at run time; no key material is stored.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import sys
import urllib.error
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from novel_downloader.apps.common import updater

#: The producer is loaded from ``scripts/`` the same way ``release.py`` does,
#: so the tests can sign with the real build path and verify byte fidelity.
_BUILD_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "build_update_manifest.py"
)
_spec = importlib.util.spec_from_file_location(
    "build_update_manifest_for_signature_tests", _BUILD_SCRIPT
)
assert _spec and _spec.loader
_builder = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _builder
_spec.loader.exec_module(_builder)

_SLUG = "lingyi-ling/novel-downloader"
_TAG = "v9.9.9"
_VERSION = "9.9.9"
_INSTALLER = f"NovelDownloader-setup-{_VERSION}-x64.exe"
_PORTABLE = f"NovelDownloader-{_VERSION}-windows-x64.zip"
_DIGEST = "1" * 64
_SIZE = 43_259_516

_Mutation = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class _Key:
    """An Ed25519 key pair in the format the manifest signature uses."""

    seed: bytes
    public: str
    key_id: str


def _key_id(public_hex: str) -> str:
    return hashlib.sha256(bytes.fromhex(public_hex)).hexdigest()[:16]


def _generate_keypair() -> _Key:
    """
    A fresh key pair, generated at run time (never a fixture in the repo).

    ``cryptography`` is used when it is installed -- it is only part of the
    optional ``music`` extra, so the fallback is the pycryptodome API the
    production producer itself uses.  Both yield the same 32-byte raw seed and
    public key the signature format is defined over.
    """
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
    except ImportError:  # pragma: no cover - depends on the install profile
        from Crypto.PublicKey import ECC

        generated = ECC.generate(curve="ed25519")
        seed = generated.seed
        public = generated.public_key().export_key(format="raw")
    else:
        private = Ed25519PrivateKey.generate()
        seed = private.private_bytes_raw()
        public = private.public_key().public_bytes_raw()
    public_hex = public.hex()
    return _Key(seed=seed, public=public_hex, key_id=_key_id(public_hex))


def _sign_bytes(message: bytes, seed: bytes) -> bytes:
    """Sign *message* with a raw Ed25519 seed."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
    except ImportError:  # pragma: no cover - depends on the install profile
        from Crypto.Signature import eddsa

        return eddsa.new(eddsa.import_private_key(seed), "rfc8032").sign(message)
    return Ed25519PrivateKey.from_private_bytes(seed).sign(message)


def _canonical(document: Mapping[str, Any]) -> bytes:
    """The documented signing bytes, re-derived here independently."""
    body = {k: v for k, v in document.items() if k not in {"signature", "key_id"}}
    return json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _signed(document: dict[str, Any], key: _Key) -> dict[str, Any]:
    """Attach ``key_id`` and ``signature`` to a copy of *document*."""
    signed = dict(document)
    signed["key_id"] = key.key_id
    signed["signature"] = base64.b64encode(
        _sign_bytes(_canonical(document), key.seed)
    ).decode("ascii")
    return signed


def _release_url(name: str, *, tag: str = _TAG) -> str:
    return f"https://github.com/{_SLUG}/releases/download/{tag}/{name}"


def _entry(
    name: str,
    *,
    tag: str = _TAG,
    sha256: str = _DIGEST,
    size: int = _SIZE,
    url: str | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "url": _release_url(name, tag=tag) if url is None else url,
        "sha256": sha256,
        "size": size,
    }


def _document() -> dict[str, Any]:
    """A valid manifest bound to the repository the client asks about."""
    return {
        "schema": updater.MANIFEST_SCHEMA,
        "version": _VERSION,
        "tag": _TAG,
        "repo": _SLUG,
        "generated_from": f"https://github.com/{_SLUG}",
        "platforms": {
            "windows-x64-installer": _entry(_INSTALLER),
            "windows-x64-portable": _entry(_PORTABLE),
        },
    }


def _parse(
    document: Mapping[str, Any],
    *,
    current: str = "1.0.0",
    platform_tag: str | None = "windows-x64",
    slug: str | None = _SLUG,
) -> updater.UpdateInfo:
    return updater.parse_update_manifest(
        document, current=current, platform_tag=platform_tag, slug=slug
    )


def _set_top(field: str, value: Any) -> _Mutation:
    def mutate(document: dict[str, Any]) -> None:
        document[field] = value

    return mutate


def _delete_top(field: str) -> _Mutation:
    def mutate(document: dict[str, Any]) -> None:
        del document[field]

    return mutate


def _set_platform(field: str, value: Any) -> _Mutation:
    def mutate(document: dict[str, Any]) -> None:
        document["platforms"]["windows-x64-installer"][field] = value

    return mutate


def _add_platform(document: dict[str, Any]) -> None:
    document["platforms"]["linux-x64-portable"] = _entry(
        f"NovelDownloader-{_VERSION}-linux-x64.zip"
    )


def _drop_platform(document: dict[str, Any]) -> None:
    del document["platforms"]["windows-x64-portable"]


# ---------------------------------------------------------------------------
# key material
# ---------------------------------------------------------------------------


@pytest.fixture
def key() -> _Key:
    return _generate_keypair()


@pytest.fixture
def pinned(monkeypatch: pytest.MonkeyPatch, key: _Key) -> _Key:
    """Configure *key* as the one trusted manifest key."""
    monkeypatch.setattr(updater, "TRUSTED_UPDATE_KEYS", {key.key_id: key.public})
    return key


@pytest.fixture
def keyless(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped default: no key configured, signatures not checked."""
    monkeypatch.setattr(updater, "TRUSTED_UPDATE_KEYS", {})


# ---------------------------------------------------------------------------
# the two network entry points, faked (no sockets)
# ---------------------------------------------------------------------------


def _spy_api(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the GitHub API call with a recorder, returning its hits."""
    hits: list[str] = []

    def fake_fetch(
        slug: str = updater.DEFAULT_REPO,
        *,
        timeout: float = 0.0,
        opener: Callable[[str, float], Any] | None = None,
    ) -> dict[str, Any]:
        hits.append(slug)
        return {"tag_name": f"v{_VERSION}", "assets": []}

    monkeypatch.setattr(updater, "fetch_latest_release", fake_fetch)
    return hits


def _manifest_opener(manifest: Any) -> Callable[[str, float], Any]:
    """An opener that only ever answers the manifest URL."""

    def opener(url: str, timeout: float) -> Any:
        assert url == updater.manifest_url(_SLUG), f"unexpected request: {url}"
        if isinstance(manifest, BaseException):
            raise manifest
        return manifest

    return opener


def _run_check(
    monkeypatch: pytest.MonkeyPatch,
    manifest: Any,
    *,
    slug: str = _SLUG,
) -> tuple[updater.UpdateCheck, list[str]]:
    """Run one check with a recording API stub; return (result, api hits)."""
    hits = _spy_api(monkeypatch)
    result = updater.check_for_update(
        "1.0.0",
        slug=slug,
        platform_tag="windows-x64",
        opener=_manifest_opener(manifest),
    )
    return result, hits


class _FakeResponse:
    """The subset of ``http.client.HTTPResponse`` the updater reads."""

    def __init__(self, body: bytes, url: str, status: int = 200) -> None:
        self._stream = io.BytesIO(body)
        self._url = url
        self.status = status
        self.headers = {"Content-Length": str(len(body))}

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


class _FakeServer:
    """A ``urlopen`` replacement serving recorded bodies; no socket is opened."""

    def __init__(
        self,
        routes: Mapping[str, bytes | BaseException],
        *,
        final_url: str | None = None,
    ) -> None:
        self._routes = dict(routes)
        self._final_url = final_url
        self.requested: list[str] = []

    def __call__(self, request: Any, timeout: float | None = None) -> _FakeResponse:
        url = request.full_url
        self.requested.append(url)
        served = self._routes.get(url)
        if served is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        if isinstance(served, BaseException):
            raise served
        return _FakeResponse(served, self._final_url or url)


def _serve(monkeypatch: pytest.MonkeyPatch, server: _FakeServer) -> _FakeServer:
    monkeypatch.setattr(updater.urllib.request, "urlopen", server)
    return server


# ---------------------------------------------------------------------------
# a correctly signed manifest is accepted
# ---------------------------------------------------------------------------


class TestSignedManifestIsAccepted:
    def test_a_freshly_signed_manifest_verifies(self, pinned: _Key) -> None:
        document = _signed(_document(), pinned)

        updater.verify_manifest_signature(document)
        info = _parse(document)

        assert info.latest == _VERSION
        assert info.available is True
        assert info.asset is not None
        assert info.asset.name == _INSTALLER
        assert info.asset.sha256 == _DIGEST

    def test_the_client_recomputes_the_documented_canonical_bytes(self) -> None:
        """The verifier must canonicalise exactly what the producer signs."""
        document = _document()
        document["notes_url"] = "https://github.com/example/版本说明"

        assert updater._manifest_signing_bytes(document) == _canonical(document)
        assert _builder._signing_bytes(document) == updater._manifest_signing_bytes(
            document
        )

    def test_the_producer_script_output_is_accepted_verbatim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A manifest signed by ``build_update_manifest`` verifies on the client."""
        key = _generate_keypair()
        signed = _builder.sign_document(_document(), key.seed.hex())
        monkeypatch.setattr(
            updater, "TRUSTED_UPDATE_KEYS", {signed["key_id"]: key.public}
        )

        assert signed["key_id"] == key.key_id
        info = _parse(signed)

        assert info.available is True

    def test_a_manifest_with_a_v_prefixed_version_verifies(self, pinned: _Key) -> None:
        document = _document()
        document["version"] = _TAG
        signed = _signed(document, pinned)

        assert _parse(signed).latest == _VERSION

    def test_the_signature_survives_reformatting_and_key_reordering(
        self, pinned: _Key
    ) -> None:
        """The signature covers values, not the bytes of the file on disk."""
        signed = _signed(_document(), pinned)
        reformatted = json.dumps(
            dict(reversed(list(signed.items()))),
            indent=2,
            ensure_ascii=True,
        )

        info = _parse(json.loads(reformatted))

        assert info.available is True

    def test_a_unicode_notes_url_is_signed_as_utf8(self, pinned: _Key) -> None:
        document = _document()
        document["notes_url"] = "https://github.com/example/版本说明"
        signed = _signed(document, pinned)

        assert _parse(signed).notes_url == document["notes_url"]

    def test_cryptography_and_pycryptodome_agree(
        self, monkeypatch: pytest.MonkeyPatch, key: _Key
    ) -> None:
        """Two Ed25519 implementations must produce interchangeable signatures."""
        pytest.importorskip("cryptography")
        from Crypto.Signature import eddsa

        message = _canonical(_document())
        signature = _sign_bytes(message, key.seed)

        public = eddsa.import_public_key(bytes.fromhex(key.public))
        eddsa.new(public, "rfc8032").verify(message, signature)

        producer = _builder.sign_document(_document(), key.seed.hex())
        monkeypatch.setattr(updater, "TRUSTED_UPDATE_KEYS", {key.key_id: key.public})

        assert producer["signature"] == base64.b64encode(signature).decode()
        assert producer["key_id"] == key.key_id
        updater.verify_manifest_signature(producer)


# ---------------------------------------------------------------------------
# unsigned manifests are refused once a key is configured
# ---------------------------------------------------------------------------


def _strip(field: str) -> _Mutation:
    return _delete_top(field)


def _set_signature(value: Any) -> _Mutation:
    return _set_top("signature", value)


def _set_key_id(value: Any) -> _Mutation:
    return _set_top("key_id", value)


class TestUnsignedManifestIsRefused:
    @pytest.mark.parametrize(
        "mutate",
        [
            _strip("signature"),
            _strip("key_id"),
            _set_signature(None),
            _set_signature(0),
            _set_signature(["AAAA"]),
            _set_key_id(None),
            _set_key_id(0),
            _set_key_id({"id": "x"}),
        ],
        ids=[
            "no-signature-field",
            "no-key-id-field",
            "signature-null",
            "signature-int",
            "signature-list",
            "key-id-null",
            "key-id-int",
            "key-id-object",
        ],
    )
    def test_a_missing_or_malformed_signature_is_refused(
        self, pinned: _Key, mutate: _Mutation
    ) -> None:
        signed = _signed(_document(), pinned)
        mutate(signed)

        with pytest.raises(ValueError, match="not signed"):
            _parse(signed)

    def test_a_completely_unsigned_manifest_is_refused(self, pinned: _Key) -> None:
        with pytest.raises(ValueError, match="not signed"):
            _parse(_document())

    def test_an_empty_signature_is_refused(self, pinned: _Key) -> None:
        """An empty string must not be treated as "signature present"."""
        signed = _signed(_document(), pinned)
        signed["signature"] = ""

        with pytest.raises(ValueError, match="does not verify"):
            _parse(signed)

    def test_a_truncated_signature_is_refused(self, pinned: _Key) -> None:
        signed = _signed(_document(), pinned)
        raw = base64.b64decode(signed["signature"])
        signed["signature"] = base64.b64encode(raw[:32]).decode("ascii")

        with pytest.raises(ValueError, match="does not verify"):
            _parse(signed)

    def test_a_zero_signature_is_refused(self, pinned: _Key) -> None:
        signed = _signed(_document(), pinned)
        signed["signature"] = base64.b64encode(bytes(64)).decode("ascii")

        with pytest.raises(ValueError, match="does not verify"):
            _parse(signed)

    def test_a_non_base64_signature_is_refused(self, pinned: _Key) -> None:
        signed = _signed(_document(), pinned)
        signed["signature"] = "not base64 !!"

        with pytest.raises(ValueError, match="does not verify"):
            _parse(signed)


# ---------------------------------------------------------------------------
# key selection is bound to the trusted table
# ---------------------------------------------------------------------------


class TestKeyBinding:
    def test_an_unknown_key_id_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        key = _generate_keypair()
        signed = _signed(_document(), key)
        monkeypatch.setattr(updater, "TRUSTED_UPDATE_KEYS", {"0" * 16: key.public})

        with pytest.raises(ValueError, match="unknown key"):
            _parse(signed)

    def test_a_signature_from_an_untrusted_key_is_refused(self, pinned: _Key) -> None:
        """A second key is not trusted merely because it signs correctly."""
        stranger = _generate_keypair()
        signed = _signed(_document(), stranger)

        with pytest.raises(ValueError, match="unknown key"):
            _parse(signed)

    def test_relabelling_a_signature_with_another_trusted_key_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, key: _Key
    ) -> None:
        """
        ``key_id`` selects the public key; it cannot redirect a signature.

        Both keys are trusted, so the only thing that can reject this manifest
        is the client resolving the key by the *claimed* id and verifying the
        signature against it.
        """
        other = _generate_keypair()
        monkeypatch.setattr(
            updater,
            "TRUSTED_UPDATE_KEYS",
            {key.key_id: key.public, other.key_id: other.public},
        )
        signed = _signed(_document(), key)
        signed["key_id"] = other.key_id

        with pytest.raises(ValueError, match="does not verify"):
            _parse(signed)

    def test_a_key_id_whose_trusted_key_differs_from_the_signer_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A rotated/mismatched table entry must fail closed, not fall open."""
        signer = _generate_keypair()
        rotated = _generate_keypair()
        signed = _signed(_document(), signer)
        monkeypatch.setattr(
            updater, "TRUSTED_UPDATE_KEYS", {signer.key_id: rotated.public}
        )

        with pytest.raises(ValueError, match="does not verify"):
            _parse(signed)

    @pytest.mark.parametrize(
        "public",
        ["", "zz", "00" * 31, "00" * 32],
        ids=["empty", "not-hex", "short", "wrong-key"],
    )
    def test_a_misconfigured_trusted_key_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, public: str
    ) -> None:
        """
        A bad hex value in ``TRUSTED_UPDATE_KEYS`` must reject, not crash.

        A typo in the pasted public key is an operational mistake; the client
        still raises the same ``ValueError`` every other rejection uses, so
        :func:`updater.check_for_update` reports it instead of propagating.
        """
        key = _generate_keypair()
        signed = _signed(_document(), key)
        monkeypatch.setattr(updater, "TRUSTED_UPDATE_KEYS", {key.key_id: public})

        with pytest.raises(ValueError, match="does not verify"):
            _parse(signed)


# ---------------------------------------------------------------------------
# any content change invalidates the signature
# ---------------------------------------------------------------------------


class TestTamperedManifestIsRefused:
    @pytest.mark.parametrize(
        "mutate",
        [
            _set_top("version", "0.0.1"),
            _set_top("tag", "v0.0.1"),
            _set_top("repo", "evil/repo"),
            _set_top("schema", 2),
            _set_top("generated_from", "https://evil.example/release"),
            _set_top("notes_url", "https://evil.example/notes"),
            _set_top("unexpected", "field"),
            _set_platform("name", "evil.exe"),
            _set_platform("url", _release_url("evil.exe")),
            _set_platform("sha256", "0" * 64),
            _set_platform("size", 1),
            _drop_platform,
            _add_platform,
        ],
        ids=[
            "version",
            "tag",
            "repo",
            "schema",
            "generated-from",
            "notes-url",
            "extra-field",
            "platform-name",
            "platform-url",
            "platform-sha256",
            "platform-size",
            "dropped-platform",
            "added-platform",
        ],
    )
    def test_any_change_to_a_signed_manifest_is_refused(
        self, pinned: _Key, mutate: _Mutation
    ) -> None:
        signed = _signed(_document(), pinned)
        mutate(signed)

        with pytest.raises(ValueError, match="does not verify"):
            _parse(signed)

    def test_a_signature_lifted_onto_another_document_is_refused(
        self, pinned: _Key
    ) -> None:
        signed = _signed(_document(), pinned)
        other = _document()
        other["notes_url"] = "https://github.com/example/notes"
        other["signature"] = signed["signature"]
        other["key_id"] = signed["key_id"]

        with pytest.raises(ValueError, match="does not verify"):
            _parse(other)

    def test_duplicate_json_keys_are_judged_by_what_was_parsed(
        self, pinned: _Key
    ) -> None:
        """
        A file cannot smuggle a second value past the signature.

        Signing covers the parsed document, so a duplicate key that a naive
        reader might resolve differently still yields the value that was
        signed -- here ``1.0.0``, which no longer matches the signature.
        """
        signed = _signed(_document(), pinned)
        text = json.dumps(signed)
        assert text.count('"version"') == 1
        raw = text.replace(
            f'"version": "{_VERSION}"',
            f'"version": "{_VERSION}", "version": "1.0.0"',
            1,
        )
        parsed = json.loads(raw)
        assert parsed["version"] == "1.0.0"

        with pytest.raises(ValueError, match="does not verify"):
            _parse(parsed)

    def test_a_float_where_an_int_was_signed_is_refused(self, pinned: _Key) -> None:
        """``1`` and ``1.0`` are different canonical JSON, so the bytes differ."""
        signed = _signed(_document(), pinned)
        signed["platforms"]["windows-x64-installer"]["size"] = float(_SIZE)

        with pytest.raises(ValueError, match="does not verify"):
            _parse(signed)


# ---------------------------------------------------------------------------
# structurally broken manifests are refused even when correctly signed
# ---------------------------------------------------------------------------

_STRUCTURAL_CASES: list[tuple[str, _Mutation, str]] = [
    ("schema-2", _set_top("schema", 2), "unsupported manifest schema"),
    ("schema-3", _set_top("schema", 3), "unsupported manifest schema"),
    ("schema-bool", _set_top("schema", True), "unsupported manifest schema"),
    ("schema-float", _set_top("schema", 1.0), "unsupported manifest schema"),
    ("schema-string", _set_top("schema", "1"), "unsupported manifest schema"),
    ("schema-missing", _delete_top("schema"), "unsupported manifest schema"),
    ("version-missing", _delete_top("version"), "has no version"),
    ("version-empty", _set_top("version", "   "), "has no version"),
    ("version-not-a-string", _set_top("version", 42), "has no version"),
    ("tag-mismatch", _set_top("tag", "v9.9.8"), "does not match version"),
    ("tag-missing-with-slug", _delete_top("tag"), "has no tag"),
    ("repo-mismatch", _set_top("repo", "evil/repo"), "does not match"),
    ("repo-missing", _delete_top("repo"), "does not match"),
    ("platforms-missing", _delete_top("platforms"), "has no platforms"),
    ("platforms-empty", _set_top("platforms", {}), "has no platforms"),
    ("platforms-list", _set_top("platforms", []), "has no platforms"),
    (
        "platform-wrong-shape",
        _set_platform("name", f"NovelDownloader-{_VERSION}-windows-x64.apk"),
        "does not describe",
    ),
    (
        "platform-foreign-host",
        _set_platform("url", "https://evil.example/setup.exe"),
        "untrusted url",
    ),
    (
        "platform-foreign-tag",
        _set_platform("url", _release_url(_INSTALLER, tag="v1.0.0")),
        "untrusted url",
    ),
    (
        "platform-foreign-name",
        _set_platform("url", _release_url("other.exe")),
        "untrusted url",
    ),
]


class TestStructuralRejection:
    @pytest.mark.parametrize(
        ("mutate", "match"),
        [(mutate, match) for _, mutate, match in _STRUCTURAL_CASES],
        ids=[case for case, _, _ in _STRUCTURAL_CASES],
    )
    def test_a_correctly_signed_but_unusable_manifest_is_refused(
        self, pinned: _Key, mutate: _Mutation, match: str
    ) -> None:
        """
        Signature verification does not replace the structural checks.

        Each document here is signed *after* being broken, so the only reason
        it can be refused is the structural rule under test.
        """
        document = _document()
        mutate(document)
        signed = _signed(document, pinned)

        with pytest.raises(ValueError, match=match):
            _parse(signed)

    def test_a_manifest_for_another_repository_is_refused_when_pinned(
        self, pinned: _Key
    ) -> None:
        document = _document()
        document["repo"] = "evil/repo"
        signed = _signed(document, pinned)

        with pytest.raises(ValueError, match="does not match"):
            _parse(signed)

    @pytest.mark.parametrize(
        "entry",
        [
            {"name": _INSTALLER, "url": _release_url(_INSTALLER)},
            {"name": _INSTALLER, "url": _release_url(_INSTALLER), "sha256": "xyz"},
            {"name": "sub/dir.exe", "url": _release_url(_INSTALLER), "sha256": _DIGEST},
            {
                "name": _INSTALLER,
                "url": _release_url(_INSTALLER).replace("https://", "http://"),
                "sha256": _DIGEST,
                "size": _SIZE,
            },
            "not-an-object",
            None,
        ],
        ids=[
            "no-digest",
            "bad-digest",
            "path-in-name",
            "plain-http-url",
            "entry-is-string",
            "entry-is-null",
        ],
    )
    def test_an_unusable_platform_entry_is_never_selected(
        self, pinned: _Key, entry: Any
    ) -> None:
        """
        A malformed entry is dropped rather than advertised.

        The client may still parse the manifest, but ``asset`` stays ``None``:
        a download is always digest-bound, so an incomplete entry can deny an
        update but never redirect it.
        """
        document = _document()
        document["platforms"] = {"windows-x64-installer": entry}
        signed = _signed(document, pinned)

        info = _parse(signed)

        assert info.asset is None
        assert info.assets == ()

    def test_a_malformed_entry_does_not_hide_a_valid_one(self, pinned: _Key) -> None:
        document = _document()
        document["platforms"]["windows-x64-installer"]["sha256"] = "nope"
        signed = _signed(document, pinned)

        info = _parse(signed, platform_tag="windows-x64")

        assert info.asset is not None
        assert info.asset.name == _PORTABLE

    def test_a_missing_size_is_tolerated_but_the_digest_is_still_required(
        self, pinned: _Key
    ) -> None:
        """``size`` is advisory; the SHA-256 is the integrity contract."""
        document = _document()
        document["platforms"]["windows-x64-installer"].pop("size")
        signed = _signed(document, pinned)

        info = _parse(signed)

        assert info.asset is not None
        assert info.asset.size is None
        assert info.asset.sha256 == _DIGEST

    def test_a_tampered_entry_cannot_outrank_a_valid_one(self, pinned: _Key) -> None:
        """Adding a second, unverified entry is a signature failure, not a merge."""
        signed = _signed(_document(), pinned)
        signed["platforms"]["windows-x64-installer-new"] = _entry("evil.exe")

        with pytest.raises(ValueError, match="does not verify"):
            _parse(signed)


# ---------------------------------------------------------------------------
# pinned clients never fall back to the GitHub API
# ---------------------------------------------------------------------------


class TestNoFallbackWhilePinned:
    def test_a_signed_manifest_is_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        key = _generate_keypair()
        monkeypatch.setattr(updater, "TRUSTED_UPDATE_KEYS", {key.key_id: key.public})

        result, hits = _run_check(monkeypatch, _signed(_document(), key))

        assert result.source == "manifest"
        assert result.error is None
        assert result.available is True
        assert hits == []

    @pytest.mark.parametrize(
        "manifest",
        [
            _document(),
            _signed(_document(), _generate_keypair()),
        ],
        ids=["unsigned", "signed-by-a-stranger"],
    )
    def test_an_untrusted_manifest_is_refused_without_the_api(
        self, monkeypatch: pytest.MonkeyPatch, manifest: dict[str, Any]
    ) -> None:
        key = _generate_keypair()
        monkeypatch.setattr(updater, "TRUSTED_UPDATE_KEYS", {key.key_id: key.public})

        result, hits = _run_check(monkeypatch, manifest)

        assert result.error is not None
        assert result.error.startswith("untrusted-manifest")
        assert result.available is False
        assert hits == []

    def test_a_tampered_manifest_is_refused_without_the_api(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        key = _generate_keypair()
        monkeypatch.setattr(updater, "TRUSTED_UPDATE_KEYS", {key.key_id: key.public})
        signed = _signed(_document(), key)
        signed["platforms"]["windows-x64-installer"]["sha256"] = "0" * 64

        result, hits = _run_check(monkeypatch, signed)

        assert result.error is not None
        assert result.error.startswith("untrusted-manifest")
        assert result.available is False
        assert hits == []

    @pytest.mark.parametrize(
        "failure",
        [
            OSError("connection reset"),
            TimeoutError("timed out"),
            urllib.error.URLError("dns failure"),
            urllib.error.HTTPError(
                "https://github.com/x", 404, "Not Found", None, None
            ),
            ValueError("bad payload"),
        ],
        ids=["oserror", "timeout", "urlerror", "http-404", "valueerror"],
    )
    def test_a_manifest_download_failure_never_reaches_the_api(
        self, monkeypatch: pytest.MonkeyPatch, failure: BaseException
    ) -> None:
        key = _generate_keypair()
        monkeypatch.setattr(updater, "TRUSTED_UPDATE_KEYS", {key.key_id: key.public})

        result, hits = _run_check(monkeypatch, failure)

        assert result.error == "manifest-unavailable"
        assert result.info is None
        assert hits == []

    @pytest.mark.parametrize("payload", [[], "text", 0, None])
    def test_a_manifest_that_is_not_a_json_object_never_reaches_the_api(
        self, monkeypatch: pytest.MonkeyPatch, payload: Any
    ) -> None:
        key = _generate_keypair()
        monkeypatch.setattr(updater, "TRUSTED_UPDATE_KEYS", {key.key_id: key.public})

        result, hits = _run_check(monkeypatch, payload)

        assert result.error == "manifest-unavailable"
        assert result.info is None
        assert hits == []


# ---------------------------------------------------------------------------
# the real HTTP path, replayed from local files (still no sockets)
# ---------------------------------------------------------------------------


class TestManifestOverFakeHttp:
    def _uploaded_bytes(self, tmp_path: Path, signed: dict[str, Any]) -> bytes:
        """Write the manifest the way the release pipeline publishes it."""
        uploaded = tmp_path / "latest.json"
        uploaded.write_bytes(_builder.manifest_bytes(signed))
        return uploaded.read_bytes()

    def test_the_published_file_is_fetched_verified_and_used(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        key = _generate_keypair()
        monkeypatch.setattr(updater, "TRUSTED_UPDATE_KEYS", {key.key_id: key.public})
        body = self._uploaded_bytes(tmp_path, _signed(_document(), key))
        server = _serve(monkeypatch, _FakeServer({updater.manifest_url(_SLUG): body}))

        result = updater.check_for_update(
            "1.0.0", slug=_SLUG, platform_tag="windows-x64"
        )

        assert result.source == "manifest"
        assert result.available is True
        assert server.requested == [updater.manifest_url(_SLUG)]

    def test_a_non_json_body_is_refused_without_the_api(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        key = _generate_keypair()
        monkeypatch.setattr(updater, "TRUSTED_UPDATE_KEYS", {key.key_id: key.public})
        uploaded = tmp_path / "latest.json"
        uploaded.write_bytes(b"<html>404</html>")
        server = _serve(
            monkeypatch,
            _FakeServer({updater.manifest_url(_SLUG): uploaded.read_bytes()}),
        )

        result = updater.check_for_update(
            "1.0.0", slug=_SLUG, platform_tag="windows-x64"
        )

        assert result.error == "manifest-unavailable"
        assert result.info is None
        assert server.requested == [updater.manifest_url(_SLUG)]

    def test_a_plain_http_redirect_is_refused_without_the_api(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The manifest is the trust anchor; it must arrive over TLS."""
        key = _generate_keypair()
        monkeypatch.setattr(updater, "TRUSTED_UPDATE_KEYS", {key.key_id: key.public})
        body = self._uploaded_bytes(tmp_path, _signed(_document(), key))
        server = _serve(
            monkeypatch,
            _FakeServer(
                {updater.manifest_url(_SLUG): body},
                final_url="http://mirror.example/latest.json",
            ),
        )

        result = updater.check_for_update(
            "1.0.0", slug=_SLUG, platform_tag="windows-x64"
        )

        assert result.error == "manifest-unavailable"
        assert server.requested == [updater.manifest_url(_SLUG)]

    def test_an_oversized_manifest_is_refused_without_the_api(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        key = _generate_keypair()
        monkeypatch.setattr(updater, "TRUSTED_UPDATE_KEYS", {key.key_id: key.public})
        monkeypatch.setattr(updater, "_MAX_API_BYTES", 64)
        body = self._uploaded_bytes(tmp_path, _signed(_document(), key))
        assert len(body) > 64
        server = _serve(monkeypatch, _FakeServer({updater.manifest_url(_SLUG): body}))

        result = updater.check_for_update(
            "1.0.0", slug=_SLUG, platform_tag="windows-x64"
        )

        assert result.error == "manifest-unavailable"
        assert server.requested == [updater.manifest_url(_SLUG)]

    def test_an_http_error_is_refused_without_the_api(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        key = _generate_keypair()
        monkeypatch.setattr(updater, "TRUSTED_UPDATE_KEYS", {key.key_id: key.public})
        failure = urllib.error.HTTPError(
            updater.manifest_url(_SLUG), 404, "Not Found", None, None
        )
        server = _serve(
            monkeypatch, _FakeServer({updater.manifest_url(_SLUG): failure})
        )

        result = updater.check_for_update(
            "1.0.0", slug=_SLUG, platform_tag="windows-x64"
        )

        assert result.error == "manifest-unavailable"
        assert server.requested == [updater.manifest_url(_SLUG)]


# ---------------------------------------------------------------------------
# with no key configured, the pre-signing behaviour is preserved
# ---------------------------------------------------------------------------


class TestKeylessBehaviour:
    def test_an_unsigned_manifest_is_used_directly(self, keyless: None) -> None:
        result = updater.check_for_update(
            "1.0.0",
            slug=_SLUG,
            platform_tag="windows-x64",
            opener=_manifest_opener(_document()),
        )

        assert result.source == "manifest"
        assert result.available is True

    def test_a_signature_is_ignored_entirely_while_no_key_is_trusted(
        self, keyless: None
    ) -> None:
        """
        Without a configured key a signature is merely an extra field.

        This is the shipped default before signing is enabled; it is asserted
        so the transition to "pinned" cannot silently leave a hole where a
        signature is half-checked.
        """
        signed = _signed(_document(), _generate_keypair())
        signed["platforms"]["windows-x64-installer"]["sha256"] = "0" * 64

        info = _parse(signed)

        assert info.available is True

    def test_a_missing_manifest_falls_back_to_the_api(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result, hits = _run_check(monkeypatch, OSError("offline"))

        assert result.source == "api"
        assert result.error is None
        assert hits == [_SLUG]

    def test_a_structurally_unusable_manifest_falls_back_to_the_api(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        document = _document()
        document["schema"] = 2

        result, hits = _run_check(monkeypatch, document)

        assert result.source == "api"
        assert result.error is None
        assert hits == [_SLUG]

    def test_a_non_json_body_falls_back_to_the_api(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        uploaded = tmp_path / "latest.json"
        uploaded.write_bytes(b"{not json")
        api_payload = json.dumps({"tag_name": f"v{_VERSION}", "assets": []}).encode(
            "utf-8"
        )
        server = _serve(
            monkeypatch,
            _FakeServer(
                {
                    updater.manifest_url(_SLUG): uploaded.read_bytes(),
                    updater.release_api_url(_SLUG): api_payload,
                }
            ),
        )

        result = updater.check_for_update(
            "1.0.0", slug=_SLUG, platform_tag="windows-x64"
        )

        assert result.source == "api"
        assert result.error is None
        assert server.requested == [
            updater.manifest_url(_SLUG),
            updater.release_api_url(_SLUG),
        ]
