"""Yuewen NodeDecryptor: input guards, asset preparation, subprocess contract."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from novel_downloader.plugins.utils.yuewen.node_decryptor import (
    NodeDecryptor,
    RemoteAssetSpec,
)

NODE_AVAILABLE = shutil.which("node") is not None
FAKE_NODE = "definitely-not-a-real-node-binary-xyz"


def _local_spec(src: Path, filename: str = "main.js") -> dict[str, Any]:
    return {"type": "local", "src": src, "filename": filename}


def _make_decryptor(
    tmp_path: Path, *, node_bin: str = FAKE_NODE, **kwargs: Any
) -> NodeDecryptor:
    """A decryptor whose ``has_node`` is normally False (fake binary)."""
    script_dir = tmp_path / "scripts"
    # In production _prepare() creates this dir before _ensure_asset() runs;
    # direct _ensure_asset() tests rely on the same precondition.
    script_dir.mkdir(parents=True, exist_ok=True)
    return NodeDecryptor(
        script_dir=script_dir,
        script=_local_spec(tmp_path / "src.js"),
        node_bin=node_bin,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# availability / input guards
# ---------------------------------------------------------------------------


def test_has_node_false_for_unknown_binary(tmp_path: Path) -> None:
    dec = _make_decryptor(tmp_path)
    assert dec.has_node is False


def test_decrypt_skipped_without_node(tmp_path: Path) -> None:
    dec = _make_decryptor(tmp_path)
    assert dec.decrypt("ct", "1", "fkp", "fuid") is None
    # never attempted to materialize the script
    assert not (dec.script_dir / "main.js").exists()


def test_decrypt_skipped_when_params_missing(tmp_path: Path) -> None:
    dec = _make_decryptor(tmp_path)
    dec.has_node = True  # pretend node exists; guards must fire first
    for args in [
        ("", "1", "fkp", "fuid"),
        ("ct", "", "fkp", "fuid"),
        ("ct", "1", "", "fuid"),
        ("ct", "1", "fkp", ""),
    ]:
        assert dec.decrypt(*args) is None
    assert dec._script_path is None


# ---------------------------------------------------------------------------
# asset preparation
# ---------------------------------------------------------------------------


def test_prepare_copies_local_script_and_assets(tmp_path: Path) -> None:
    script_src = tmp_path / "src.js"
    script_src.write_bytes(b"console.log('hi');")
    asset_src = tmp_path / "helper.js"
    asset_src.write_bytes(b"module.exports = 42;")

    dec = NodeDecryptor(
        script_dir=tmp_path / "scripts",
        script=_local_spec(script_src, "main.js"),
        assets=[_local_spec(asset_src, "helper.js")],
        node_bin=FAKE_NODE,
    )
    dec._prepare()

    assert dec.has_node is False  # fake binary: _prepare is a no-op
    # Without node, _prepare bails out before touching the disk.
    assert not (tmp_path / "scripts" / "main.js").exists()


def test_ensure_asset_local_copy(tmp_path: Path) -> None:
    src = tmp_path / "src.js"
    src.write_bytes(b"payload-bytes")
    dec = _make_decryptor(tmp_path)

    dst = dec._ensure_asset(_local_spec(src, "main.js"))  # type: ignore[arg-type]
    assert dst == dec.script_dir / "main.js"
    assert dst.read_bytes() == b"payload-bytes"


def test_ensure_asset_cache_short_circuits(tmp_path: Path) -> None:
    src = tmp_path / "src.js"
    src.write_bytes(b"payload-bytes")
    dec = _make_decryptor(tmp_path)
    spec = _local_spec(src, "main.js")

    first = dec._ensure_asset(spec)  # type: ignore[arg-type]
    first.unlink()
    second = dec._ensure_asset(spec)  # type: ignore[arg-type]

    # filename already in cache: no re-copy even though the file is gone
    assert second == first
    assert not second.exists()


def test_ensure_asset_accepts_traversable_source(tmp_path: Path) -> None:
    class FakeTraversable:
        """Duck-typed Traversable returning fixed bytes."""

        def read_bytes(self) -> bytes:
            return b"traversable-bytes"

    dec = _make_decryptor(tmp_path)
    spec: dict[str, Any] = {
        "type": "local",
        "src": FakeTraversable(),
        "filename": "main.js",
    }
    # Not a Path: exercises the package-Traversable branch of _ensure_asset,
    # which only duck-types read_bytes().
    assert not isinstance(spec["src"], Path)

    dst = dec._ensure_asset(spec)  # type: ignore[arg-type]
    assert dst.read_bytes() == b"traversable-bytes"


def test_ensure_asset_remote_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_get(url: str, **kwargs: Any) -> SimpleNamespace:
        calls.append({"url": url, **kwargs})
        return SimpleNamespace(content=b"remote-bytes", raise_for_status=lambda: None)

    monkeypatch.setattr(
        "novel_downloader.plugins.utils.yuewen.node_decryptor.requests.get", fake_get
    )
    dec = _make_decryptor(tmp_path)
    spec: RemoteAssetSpec = {
        "type": "remote",
        "url": "https://example.invalid/main.js",
        "filename": "main.js",
    }

    dst = dec._ensure_asset(spec)
    assert dst.read_bytes() == b"remote-bytes"
    assert calls[0]["url"] == "https://example.invalid/main.js"


def test_ensure_asset_remote_http_error_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_get(url: str, **kwargs: Any) -> SimpleNamespace:
        def raise_status() -> None:
            msg = "404"
            raise RuntimeError(msg)

        return SimpleNamespace(content=b"", raise_for_status=raise_status)

    monkeypatch.setattr(
        "novel_downloader.plugins.utils.yuewen.node_decryptor.requests.get", fake_get
    )
    dec = _make_decryptor(tmp_path)
    spec: RemoteAssetSpec = {
        "type": "remote",
        "url": "https://example.invalid/main.js",
        "filename": "main.js",
    }

    with pytest.raises(RuntimeError, match="404"):
        dec._ensure_asset(spec)


# ---------------------------------------------------------------------------
# decrypt(): preparation failures
# ---------------------------------------------------------------------------


def test_decrypt_returns_none_when_script_missing(tmp_path: Path) -> None:
    # has_node is False by default, so force it on to reach _prepare()
    dec = _make_decryptor(tmp_path)
    dec.has_node = True
    dec.script = _local_spec(tmp_path / "does-not-exist.js")  # type: ignore[assignment]

    assert dec.decrypt("ct", "1", "fkp", "fuid") is None
    assert dec._script_path is None
    assert dec._prepared is True  # failure is not retried


# ---------------------------------------------------------------------------
# decrypt(): subprocess contract (Node binary faked at subprocess.run)
# ---------------------------------------------------------------------------


def _fake_run_factory(
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
    raise_exc: Exception | None = None,
) -> tuple[Any, list[tuple[list[str], dict[str, Any]]]]:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(args: list[str], **kwargs: Any) -> SimpleNamespace:
        calls.append((args, kwargs))
        if raise_exc is not None:
            raise raise_exc
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    return fake_run, calls


@pytest.fixture()
def node_ready_decryptor(tmp_path: Path) -> NodeDecryptor:
    script_src = tmp_path / "src.js"
    script_src.write_bytes(b"// fake node script")
    dec = _make_decryptor(tmp_path)
    dec.has_node = True
    dec.script = _local_spec(script_src)  # type: ignore[assignment]
    return dec


def test_decrypt_success_returns_stripped_stdout(
    tmp_path: Path,
    node_ready_decryptor: NodeDecryptor,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_run, calls = _fake_run_factory(stdout="  decrypted text \n")
    monkeypatch.setattr(subprocess, "run", fake_run)

    result = node_ready_decryptor.decrypt("CIPHER", "42", "fkp", "fuid")

    assert result == "decrypted text"
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == node_ready_decryptor.node_bin
    assert args[1].endswith("main.js")
    assert kwargs["input"] == json.dumps(["CIPHER", "42", "fkp", "fuid"])
    assert kwargs["cwd"] == node_ready_decryptor.script_dir
    assert kwargs["timeout"] == pytest.approx(15.0)


def test_decrypt_uses_configured_timeout(
    node_ready_decryptor: NodeDecryptor, monkeypatch: pytest.MonkeyPatch
) -> None:
    node_ready_decryptor.request_timeout = 1.5
    fake_run, calls = _fake_run_factory(stdout="ok")
    monkeypatch.setattr(subprocess, "run", fake_run)

    node_ready_decryptor.decrypt("ct", "1", "fkp", "fuid")
    assert calls[0][1]["timeout"] == pytest.approx(1.5)


def test_decrypt_nonzero_exit_returns_none(
    node_ready_decryptor: NodeDecryptor, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_run, _ = _fake_run_factory(returncode=1, stderr="node blew up")
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert node_ready_decryptor.decrypt("ct", "1", "fkp", "fuid") is None


def test_decrypt_nonzero_exit_without_stderr_returns_none(
    node_ready_decryptor: NodeDecryptor, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_run, _ = _fake_run_factory(returncode=1, stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert node_ready_decryptor.decrypt("ct", "1", "fkp", "fuid") is None


def test_decrypt_timeout_returns_none(
    node_ready_decryptor: NodeDecryptor, monkeypatch: pytest.MonkeyPatch
) -> None:
    exc = subprocess.TimeoutExpired(cmd=["node"], timeout=15.0)
    fake_run, _ = _fake_run_factory(raise_exc=exc)
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert node_ready_decryptor.decrypt("ct", "1", "fkp", "fuid") is None


def test_decrypt_generic_failure_returns_none(
    node_ready_decryptor: NodeDecryptor, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_run, _ = _fake_run_factory(raise_exc=OSError("node disappeared"))
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert node_ready_decryptor.decrypt("ct", "1", "fkp", "fuid") is None


# ---------------------------------------------------------------------------
# end-to-end with a real Node.js runtime (skipped when node is absent)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not NODE_AVAILABLE, reason="Node.js not installed")
def test_decrypt_end_to_end_with_real_node(tmp_path: Path) -> None:
    script_src = tmp_path / "src.js"
    # Reads the JSON array from stdin and writes a deterministic transform.
    script_src.write_text(
        "\n".join(
            [
                'let raw = "";',
                'process.stdin.setEncoding("utf8");',
                'process.stdin.on("data", (c) => { raw += c; });',
                'process.stdin.on("end", () => {',
                "  const [ct, chapter] = JSON.parse(raw);",
                "  const out =",
                '    ct.split("").reverse().join("") + "@" + chapter;',
                "  process.stdout.write(out);",
                "});",
                "",
            ]
        ),
        encoding="utf-8",
    )
    dec = NodeDecryptor(
        script_dir=tmp_path / "scripts",
        script=_local_spec(script_src),
        node_bin="node",
    )

    assert dec.has_node is True
    assert dec.decrypt("abc", "42", "fkp", "fuid") == "cba@42"


@pytest.mark.skipif(not NODE_AVAILABLE, reason="Node.js not installed")
def test_decrypt_end_to_end_timeout_with_real_node(tmp_path: Path) -> None:
    script_src = tmp_path / "src.js"
    script_src.write_text("setTimeout(() => {}, 60000);\n", encoding="utf-8")
    dec = NodeDecryptor(
        script_dir=tmp_path / "scripts",
        script=_local_spec(script_src),
        node_bin="node",
        request_timeout=1.0,
    )

    assert dec.decrypt("abc", "42", "fkp", "fuid") is None
