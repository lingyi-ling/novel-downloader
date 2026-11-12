#!/usr/bin/env python3
"""
Verify an Android APK is ready for 16 KB page-size devices.

Android 15 introduced 16 KB memory pages on some arm64 devices, and Google
Play requires native libraries to be built for that page size.  Two different
things have to hold, and they fail independently:

1. **Zip alignment** -- every entry that is mmapped at runtime (``.so`` files,
   and uncompressed resources) must start on a 16 KB boundary inside the
   archive.  ``zipalign -P 16`` enforces this; a 4 KB-aligned APK loads on the
   emulator and crashes on a real 16 KB device.

2. **ELF segment alignment** -- the ``p_align`` of every ``PT_LOAD`` segment in
   each ``lib/*/lib*.so`` must be at least 16 KB.  A library built without
   ``-Wl,-z,max-page-size=16384`` has ``p_align = 0x1000`` and the dynamic
   linker refuses to map it.

Checking only one of the two is a false sense of safety, so this script does
both.  It is pure standard library: CI runners have no Android SDK, and the
check must also work on a developer machine with nothing installed.

Usage::

    python scripts/check_android_alignment.py path/to/app-release.apk
    python scripts/check_android_alignment.py --json app-release.apk

Exit codes: ``0`` when everything is aligned, ``1`` on any violation,
``2`` when the APK itself is unreadable.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The page size Android 15+ uses on affected arm64 devices.
REQUIRED_ALIGNMENT = 16 * 1024

#: Native library directories whose ``.so`` files are mmapped straight out of
#: the archive when ``android:extractNativeLibs="false"`` (the default for
#: AGP 3.6+).  Only these need the *zip* offset to be 16 KB aligned.
#
#: Libraries under ``assets/`` (Chaquopy ships CPython modules there) are
#: extracted at install/runtime, so their zip offset is irrelevant -- but
#: their ELF segments still have to be page-size agnostic.
MMAPPED_PREFIX = "lib/"

_ELF_MAGIC = b"\x7fELF"
_PT_LOAD = 1
_ELFCLASS64 = 2
_ELFDATA2LSB = 1
_MAX_PHDR_TABLE = 4 * 1024 * 1024

#: 64-bit ABIs that can run on a 16 KB page-size device.
_64_BIT_ABIS = frozenset({"arm64-v8a", "x86_64"})

#: 32-bit ABIs: their processes always run with 4 KB pages, so neither the
#: zip offset nor ``p_align`` is held to the 16 KB rule.  ``armeabi`` is the
#: legacy NDK name and must not fall through to the 64-bit rule.
_32_BIT_ABIS = frozenset({"armeabi", "armeabi-v7a", "x86"})


@dataclass(frozen=True)
class LibReport:
    """Alignment findings for one native library inside the APK."""

    name: str
    abi: str | None
    mmapped: bool
    zip_offset: int
    zip_aligned: bool
    elf_align: int | None
    #: Every ``PT_LOAD`` alignment, or ``None`` when the ELF is unreadable.
    #: Not part of the JSON contract; used to validate segments individually.
    elf_aligns: tuple[int, ...] | None = None


@dataclass
class AlignmentReport:
    """Everything the check learned about one APK."""

    apk: Path
    libs: list[LibReport] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when no violation was found."""
        return not self.errors


def _elf_header_fields(data: bytes) -> tuple[int, int, int] | None:
    """Return ``(phoff, phentsize, phnum)`` for a usable 64-bit ELF header."""
    if len(data) < 64 or data[:4] != _ELF_MAGIC:
        return None
    if data[4] != _ELFCLASS64 or data[5] != _ELFDATA2LSB:
        return None
    (phoff,) = struct.unpack_from("<Q", data, 0x20)
    (phentsize,) = struct.unpack_from("<H", data, 0x36)
    (phnum,) = struct.unpack_from("<H", data, 0x38)
    if phoff < 64 or phentsize < 56 or phnum == 0:
        return None
    return phoff, phentsize, phnum


def _pt_load_alignments(table: bytes, phentsize: int, phnum: int) -> list[int]:
    """Collect ``p_align`` from every ``PT_LOAD`` entry in a complete table."""
    aligns: list[int] = []
    for index in range(phnum):
        offset = index * phentsize
        (p_type,) = struct.unpack_from("<I", table, offset)
        if p_type != _PT_LOAD:
            continue
        (p_align,) = struct.unpack_from("<Q", table, offset + 0x30)
        aligns.append(p_align)
    return aligns


def elf_load_alignments(data: bytes) -> list[int] | None:
    """
    Return the ``p_align`` of every ``PT_LOAD`` segment in *data*.

    ``None`` means the bytes are not a readable little-endian 64-bit ELF file
    (wrong class, truncated program header table, ...).  An empty list means
    the file is a valid ELF header with no ``PT_LOAD`` segment at all.
    """
    fields = _elf_header_fields(data[:64])
    if fields is None:
        return None
    phoff, phentsize, phnum = fields
    end = phoff + phnum * phentsize
    if end > len(data):
        return None
    return _pt_load_alignments(data[phoff:end], phentsize, phnum)


def elf_max_p_align(data: bytes) -> int | None:
    """
    Return the largest ``PT_LOAD`` ``p_align``, or ``None`` when unreadable.

    Kept for callers that only want a summary; the gate itself must look at
    :func:`elf_load_alignments` because the *smallest* segment decides whether
    the library can be mapped with 16 KB pages.
    """
    aligns = elf_load_alignments(data)
    return max(aligns) if aligns else None


def elf_min_p_align(data: bytes) -> int | None:
    """Return the smallest ``PT_LOAD`` ``p_align``, or ``None`` when unreadable."""
    aligns = elf_load_alignments(data)
    return min(aligns) if aligns else None


def _read_exact(handle: Any, count: int) -> bytes | None:
    """Read exactly *count* bytes, or return ``None`` at a short read."""
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = handle.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _stream_load_alignments(handle: Any, file_size: int) -> list[int] | None:
    """
    Parse an ELF from an open member without loading the whole library.

    Only the ELF header and the program header table are read, so a stored
    zip entry that claims an enormous size cannot exhaust memory.
    """
    header = _read_exact(handle, 64)
    if header is None:
        return None
    fields = _elf_header_fields(header)
    if fields is None:
        return None
    phoff, phentsize, phnum = fields
    table_size = phentsize * phnum
    if table_size > _MAX_PHDR_TABLE:
        return None
    if phoff > file_size or table_size > file_size - phoff:
        return None
    remaining = phoff - 64
    while remaining > 0:
        chunk = handle.read(min(remaining, 64 * 1024))
        if not chunk:
            return None
        remaining -= len(chunk)
    table = _read_exact(handle, table_size)
    if table is None:
        return None
    return _pt_load_alignments(table, phentsize, phnum)


def _data_offset(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> int:
    """
    Byte offset of the entry's payload inside the archive.

    ``ZipInfo.extra`` comes from the central directory; the local header
    carries its own (possibly different) extra field, so the payload offset
    has to be read from the local header itself.  A header that is truncated
    or does not carry the local-file magic means the payload offset cannot be
    trusted, which makes the archive unreadable rather than silently aligned.
    """
    source = archive.fp
    if source is None:  # pragma: no cover - open archives always expose it
        raise zipfile.BadZipFile(f"archive has no file object for {info.filename!r}")
    source.seek(0, 2)
    archive_size = int(source.tell())
    source.seek(info.header_offset)
    header = source.read(30)
    if len(header) < 30 or header[:4] != b"PK\x03\x04":
        raise zipfile.BadZipFile(f"bad local file header for {info.filename!r}")
    name_len, extra_len = struct.unpack_from("<HH", header, 26)
    offset = int(info.header_offset) + 30 + int(name_len) + int(extra_len)
    if offset > archive_size or info.compress_size > archive_size - offset:
        raise zipfile.BadZipFile(f"truncated payload for {info.filename!r}")
    return offset


def _abi_of(name: str) -> str | None:
    """Extract the ABI component from a library path, if it has one."""
    parts = name.split("/")
    for candidate in _64_BIT_ABIS | _32_BIT_ABIS:
        if candidate in parts:
            return candidate
    return None


def check_apk(path: Path) -> AlignmentReport:
    """
    Check both zip and ELF alignment for every native library in *path*.

    :raises OSError: when the APK cannot be read from disk.
    :raises zipfile.BadZipFile: when the archive or one of its local headers
        is malformed.
    :raises RuntimeError: when a member is encrypted or uses an unsupported
        compression method.
    """
    report = AlignmentReport(apk=path)
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if not info.filename.endswith(".so"):
                continue
            # The data starts right after the *local* file header, whose
            # name/extra lengths can differ from the central directory copy.
            offset = _data_offset(archive, info)
            mmapped = info.filename.startswith(MMAPPED_PREFIX)
            # Compressed entries are unpacked by the installer, so their
            # offset inside the archive does not matter.
            needs_alignment = mmapped and info.compress_type == zipfile.ZIP_STORED
            zip_aligned = offset % REQUIRED_ALIGNMENT == 0 if needs_alignment else True
            # ``archive.open(info)`` reads *this* member even when duplicate
            # names exist; reading by name would resolve to the last one.
            with archive.open(info) as handle:
                aligns = _stream_load_alignments(handle, info.file_size)
            report.libs.append(
                LibReport(
                    name=info.filename,
                    abi=_abi_of(info.filename),
                    mmapped=mmapped,
                    zip_offset=offset,
                    zip_aligned=zip_aligned,
                    elf_align=min(aligns) if aligns else None,
                    elf_aligns=None if aligns is None else tuple(aligns),
                )
            )

    if not report.libs:
        # A pure-Python APK has no native code; nothing to align.
        return report

    for lib in report.libs:
        if lib.mmapped and not lib.zip_aligned:
            report.errors.append(
                f"{lib.name}: mmapped zip entry is not 16 KB aligned "
                f"(offset {lib.zip_offset}); rebuild with 'zipalign -P 16'"
            )
        # ELF segment alignment only matters for 64-bit ABIs: 32-bit
        # processes always run with 4 KB pages.
        if lib.abi is not None and lib.abi not in _64_BIT_ABIS:
            continue
        if lib.elf_aligns is None:
            report.errors.append(
                f"{lib.name}: not a readable 64-bit ELF library; cannot run on "
                "a 16 KB page-size device"
            )
        elif not lib.elf_aligns:
            report.errors.append(
                f"{lib.name}: ELF library has no PT_LOAD segment; it cannot be "
                "mapped by the dynamic linker"
            )
        elif min(lib.elf_aligns) < REQUIRED_ALIGNMENT:
            # Every PT_LOAD segment must be page-size independent; a single
            # 4 KB segment prevents the whole library from being mmapped.
            report.errors.append(
                f"{lib.name}: PT_LOAD p_align is {min(lib.elf_aligns)}, needs "
                f"at least {REQUIRED_ALIGNMENT}; rebuild with "
                "'-Wl,-z,max-page-size=16384'"
            )
    return report


def _as_json(report: AlignmentReport) -> dict[str, Any]:
    return {
        "apk": str(report.apk),
        "ok": report.ok,
        "required_alignment": REQUIRED_ALIGNMENT,
        "libs": [
            {
                "name": lib.name,
                "abi": lib.abi,
                "mmapped": lib.mmapped,
                "zip_offset": lib.zip_offset,
                "zip_aligned": lib.zip_aligned,
                "elf_align": lib.elf_align,
            }
            for lib in report.libs
        ],
        "errors": report.errors,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check an APK for 16 KB page-size alignment."
    )
    parser.add_argument("apk", type=Path, help="path to the APK to inspect")
    parser.add_argument(
        "--json", action="store_true", help="print a machine-readable report"
    )
    args = parser.parse_args(argv)

    if not args.apk.is_file():
        print(f"no such APK: {args.apk}", file=sys.stderr)
        return 2
    try:
        report = check_apk(args.apk)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        # RuntimeError covers encrypted members and unsupported compression
        # methods; the archive must be reported, never traced back at the user.
        print(f"cannot read {args.apk}: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(_as_json(report), ensure_ascii=False, indent=2))
    else:
        print(f"APK: {report.apk}")
        print(f"native libraries: {len(report.libs)}")
        for lib in report.libs:
            print(
                f"  {lib.name}: abi={lib.abi} mmapped={lib.mmapped} "
                f"zip_offset={lib.zip_offset} zip_aligned={lib.zip_aligned} "
                f"elf_p_align={lib.elf_align}"
            )
        if report.ok:
            print("OK: every native library is 16 KB aligned")
        else:
            for message in report.errors:
                print(f"ERROR: {message}", file=sys.stderr)

    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
