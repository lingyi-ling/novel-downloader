#!/usr/bin/env python3
"""Tests for scripts/check_android_alignment.py (pure standard library)."""

from __future__ import annotations

import importlib.util
import struct
import sys
import zipfile
from pathlib import Path
from typing import BinaryIO

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


def _elf64_segments(
    aligns: list[int],
    *,
    p_types: list[int] | None = None,
    phnum: int | None = None,
    data_encoding: int = 1,
) -> bytes:
    """A minimal 64-bit ELF whose program header table has one entry each."""
    header = bytearray(64)
    header[0:4] = b"\x7fELF"
    header[4] = 2  # ELFCLASS64
    header[5] = data_encoding
    struct.pack_into("<Q", header, 0x20, 64)  # e_phoff
    struct.pack_into("<H", header, 0x36, 56)  # e_phentsize
    struct.pack_into("<H", header, 0x38, len(aligns) if phnum is None else phnum)

    programs = bytearray()
    for index, p_align in enumerate(aligns):
        program = bytearray(56)
        p_type = 1 if p_types is None else p_types[index]
        struct.pack_into("<I", program, 0, p_type)  # PT_LOAD
        struct.pack_into("<Q", program, 0x30, p_align)
        programs += program
    return bytes(header) + bytes(programs)


def _apk(path: Path, entries: dict[str, tuple[bytes, int]]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, (payload, compress) in entries.items():
            archive.writestr(name, payload, compress_type=compress)
    return path


def _patch_central_directory(apk: Path, field_offset: int, value: int) -> None:
    """Overwrite one 16-bit field of the first central directory record."""
    raw = bytearray(apk.read_bytes())
    central = raw.index(b"PK\x01\x02")
    struct.pack_into("<H", raw, central + field_offset, value)
    apk.write_bytes(raw)


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


def test_every_pt_load_segment_must_be_aligned() -> None:
    """One over-aligned segment must not hide an under-aligned sibling."""
    data = _elf64_segments([16384, 4096])

    assert align.elf_load_alignments(data) == [16384, 4096]
    # The legacy helper reports the maximum; the gate must not rely on it.
    assert align.elf_max_p_align(data) == 16384
    assert align.elf_min_p_align(data) == 4096


def test_multi_segment_library_with_one_small_segment_fails(
    tmp_path: Path,
) -> None:
    # Compressed so that only the ELF rule (not the zip offset) can fail.
    apk = _apk(
        tmp_path / "app.apk",
        {
            "lib/arm64-v8a/libfoo.so": (
                _elf64_segments([16384, 4096]),
                zipfile.ZIP_DEFLATED,
            )
        },
    )

    report = align.check_apk(apk)

    assert not report.ok
    assert report.libs[0].elf_align == 4096
    assert any("p_align is 4096" in message for message in report.errors)


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x7fELF",
        _elf64_segments([16384], data_encoding=2),  # big endian
        _elf64_segments([16384], phnum=2),  # program header table truncated
        _elf64_segments([16384], p_types=[0]),  # no PT_LOAD segment
    ],
)
def test_unparseable_elf_is_never_silently_accepted(data: bytes) -> None:
    parsed = align.elf_load_alignments(data)
    assert parsed in (None, [])


def test_truncated_program_header_table_is_reported(tmp_path: Path) -> None:
    bad = _elf64_segments([16384], phnum=2)  # only one entry is present
    apk = _apk(
        tmp_path / "app.apk",
        {"lib/arm64-v8a/libfoo.so": (bad, zipfile.ZIP_DEFLATED)},
    )

    report = align.check_apk(apk)

    assert not report.ok
    assert report.libs[0].elf_align is None
    assert any("64-bit ELF" in message for message in report.errors)


def test_library_without_pt_load_is_reported(tmp_path: Path) -> None:
    apk = _apk(
        tmp_path / "app.apk",
        {
            "lib/arm64-v8a/libfoo.so": (
                _elf64_segments([16384], p_types=[0]),
                zipfile.ZIP_DEFLATED,
            )
        },
    )

    report = align.check_apk(apk)

    assert not report.ok
    assert any("PT_LOAD" in message for message in report.errors)


def test_legacy_armeabi_is_treated_as_32_bit(tmp_path: Path) -> None:
    thirty_two = b"\x7fELF" + bytes([1]) + bytes(59) + bytes(56)
    apk = _apk(
        tmp_path / "app.apk",
        {"lib/armeabi/libfoo.so": (thirty_two, zipfile.ZIP_DEFLATED)},
    )

    assert align.check_apk(apk).ok


def test_duplicate_member_names_are_read_individually(tmp_path: Path) -> None:
    """``archive.read(name)`` returns the last duplicate; check_apk must not."""
    name = "lib/arm64-v8a/libfoo.so"
    apk = tmp_path / "app.apk"
    with (
        pytest.warns(UserWarning, match="Duplicate name"),
        zipfile.ZipFile(apk, "w", zipfile.ZIP_STORED) as archive,
    ):
        archive.writestr(name, _elf64_segments([4096]))
        archive.writestr(name, _elf64_segments([16384]))

    report = align.check_apk(apk)

    assert [lib.elf_align for lib in report.libs] == [4096, 16384]
    assert not report.ok


def test_zip64_local_headers_are_located_exactly(tmp_path: Path) -> None:
    payload = _elf64_segments([16384])
    apk = tmp_path / "app.apk"
    with (
        zipfile.ZipFile(apk, "w", zipfile.ZIP_STORED) as archive,
        archive.open("lib/arm64-v8a/libfoo.so", "w", force_zip64=True) as handle,
    ):
        handle.write(payload)

    report = align.check_apk(apk)

    assert report.libs[0].zip_offset == apk.read_bytes().index(payload)


def test_data_descriptor_local_header_is_located_exactly(tmp_path: Path) -> None:
    """Streamed (non-seekable) zips store sizes in a trailing descriptor."""
    payload = _elf64_segments([16384])
    apk = tmp_path / "app.apk"

    class _Unseekable:
        def __init__(self, raw: BinaryIO) -> None:
            self.raw = raw

        def write(self, data: bytes) -> int:
            return self.raw.write(data)

        def flush(self) -> None:
            self.raw.flush()

    with (
        apk.open("wb") as raw,
        zipfile.ZipFile(_Unseekable(raw), "w", zipfile.ZIP_STORED) as archive,
    ):
        archive.writestr("lib/arm64-v8a/libfoo.so", payload)

    report = align.check_apk(apk)

    assert report.libs[0].zip_offset == apk.read_bytes().index(payload)
    assert report.libs[0].elf_align == 16384


def test_corrupt_local_header_is_an_unreadable_apk(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    apk = _apk(
        tmp_path / "app.apk",
        {"lib/arm64-v8a/libfoo.so": (_elf64(16384), zipfile.ZIP_STORED)},
    )
    raw = bytearray(apk.read_bytes())
    assert raw[:4] == b"PK\x03\x04"
    raw[:4] = b"PK\x05\x06"
    apk.write_bytes(raw)

    assert align.main([str(apk)]) == 2
    assert "cannot read" in capsys.readouterr().err


def test_encrypted_member_is_an_unreadable_apk(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    apk = _apk(
        tmp_path / "app.apk",
        {"lib/arm64-v8a/libfoo.so": (_elf64(16384), zipfile.ZIP_DEFLATED)},
    )
    _patch_central_directory(apk, 8, 0x0001)  # general purpose bit 0: encrypted

    assert align.main([str(apk)]) == 2
    assert "cannot read" in capsys.readouterr().err


def test_unsupported_compression_is_an_unreadable_apk(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    apk = _apk(
        tmp_path / "app.apk",
        {"lib/arm64-v8a/libfoo.so": (_elf64(16384), zipfile.ZIP_STORED)},
    )
    _patch_central_directory(apk, 10, 9)  # method 9 (deflate64) is unsupported

    assert align.main([str(apk)]) == 2
    assert "cannot read" in capsys.readouterr().err
