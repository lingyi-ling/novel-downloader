#!/usr/bin/env python3
"""Tests for scripts/check_android_alignment.py (pure standard library)."""

from __future__ import annotations

import importlib.util
import struct
import sys
import zipfile
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_android_alignment.py"
_spec = importlib.util.spec_from_file_location("check_android_alignment", _SCRIPT)
assert _spec and _spec.loader
align = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = align
_spec.loader.exec_module(align)


def _elf64(p_align: int, *, magic: bytes = b"\x7fELF") -> bytes:
    """A minimal 64-bit ELF with one PT_LOAD segment of the given alignment."""
    header = bytearray(64)
    header[0:4] = magic
    header[4] = 2  # ELFCLASS64
    header[5] = 1  # little endian
    struct.pack_into("<Q", header, 0x20, 64)  # e_phoff
    struct.pack_into("<H", header, 0x36, 56)  # e_phentsize
    struct.pack_into("<H", header, 0x38, 1)  # e_phnum

    program = bytearray(56)
    struct.pack_into("<I", program, 0, 1)  # PT_LOAD
    struct.pack_into("<Q", program, 0x30, p_align)
    return bytes(header) + bytes(program)


def _apk(path: Path, entries: dict[str, tuple[bytes, int]]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, (payload, compress) in entries.items():
            archive.writestr(name, payload, compress_type=compress)
    return path


def test_elf_reader_picks_the_largest_pt_load_alignment() -> None:
    assert align.elf_max_p_align(_elf64(16384)) == 16384
    assert align.elf_max_p_align(_elf64(4096)) == 4096


def test_elf_reader_rejects_non_elf_and_32_bit() -> None:
    assert align.elf_max_p_align(b"not an elf file at all") is None
    thirty_two = b"\x7fELF" + bytes([1]) + bytes(59)
    assert align.elf_max_p_align(thirty_two) is None


def test_compressed_native_library_is_exempt_from_zip_alignment(
    tmp_path: Path,
) -> None:
    apk = _apk(
        tmp_path / "app.apk",
        {"lib/arm64-v8a/libfoo.so": (_elf64(16384), zipfile.ZIP_DEFLATED)},
    )

    report = align.check_apk(apk)

    assert report.ok
    assert report.libs[0].zip_aligned is True


def test_extracted_asset_library_is_exempt_from_zip_alignment(
    tmp_path: Path,
) -> None:
    apk = _apk(
        tmp_path / "app.apk",
        {"assets/chaquopy/x86_64/zlib.so": (_elf64(16384), zipfile.ZIP_STORED)},
    )

    report = align.check_apk(apk)

    assert report.ok
    assert report.libs[0].mmapped is False


def test_misaligned_mmapped_library_fails(tmp_path: Path) -> None:
    # A stored entry at the very start of the archive is never 16 KB aligned.
    apk = _apk(
        tmp_path / "app.apk",
        {"lib/arm64-v8a/libfoo.so": (_elf64(16384), zipfile.ZIP_STORED)},
    )

    report = align.check_apk(apk)

    assert not report.ok
    assert any("not 16 KB aligned" in message for message in report.errors)


def test_small_elf_alignment_is_reported(tmp_path: Path) -> None:
    apk = _apk(
        tmp_path / "app.apk",
        {"assets/chaquopy/arm64-v8a/zlib.so": (_elf64(4096), zipfile.ZIP_DEFLATED)},
    )

    report = align.check_apk(apk)

    assert not report.ok
    assert any("p_align is 4096" in message for message in report.errors)


def test_32_bit_library_is_not_held_to_the_64_bit_rule(tmp_path: Path) -> None:
    apk = _apk(
        tmp_path / "app.apk",
        {"lib/armeabi-v7a/libfoo.so": (_elf64(4096), zipfile.ZIP_DEFLATED)},
    )

    assert align.check_apk(apk).ok


def test_an_apk_without_native_code_passes(tmp_path: Path) -> None:
    apk = _apk(
        tmp_path / "app.apk",
        {"classes.dex": (b"dex\n035", zipfile.ZIP_DEFLATED)},
    )

    report = align.check_apk(apk)

    assert report.ok
    assert report.libs == []


def test_main_reports_a_missing_apk(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert align.main([str(tmp_path / "nope.apk")]) == 2
    assert "no such APK" in capsys.readouterr().err


def test_main_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = _apk(
        tmp_path / "bad.apk",
        {"lib/arm64-v8a/libfoo.so": (_elf64(16384), zipfile.ZIP_STORED)},
    )
    assert align.main([str(bad)]) == 1
    assert "ERROR" in capsys.readouterr().err

    good = _apk(
        tmp_path / "good.apk",
        {"assets/x/arm64-v8a/z.so": (_elf64(16384), zipfile.ZIP_DEFLATED)},
    )
    assert align.main([str(good)]) == 0


def test_json_output_is_parseable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    good = _apk(
        tmp_path / "good.apk",
        {"assets/x/arm64-v8a/z.so": (_elf64(16384), zipfile.ZIP_DEFLATED)},
    )

    assert align.main(["--json", str(good)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["required_alignment"] == 16384
    assert payload["libs"][0]["abi"] == "arm64-v8a"
