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

#: 64-bit ABIs that can run on a 16 KB page-size device.
_64_BIT_ABIS = frozenset({"arm64-v8a", "x86_64"})


@dataclass(frozen=True)
class LibReport:
    """Alignment findings for one native library inside the APK."""

    name: str
    abi: str | None
    mmapped: bool
    zip_offset: int
    zip_aligned: bool
    elf_align: int | None


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


def elf_max_p_align(data: bytes) -> int | None:
    """
    Return the largest ``p_align`` of the ``PT_LOAD`` segments in *data*.

    ``None`` means the bytes are not a 64-bit ELF file -- which is not an
    error on its own (a 32-bit library cannot run on a 16 KB device anyway,
    but it should be reported rather than silently accepted).
    """
    if len(data) < 64 or data[:4] != _ELF_MAGIC:
        return None
    if data[4] != _ELFCLASS64:
        return None
    (phoff,) = struct.unpack_from("<Q", data, 0x20)
    (phentsize,) = struct.unpack_from("<H", data, 0x36)
    (phnum,) = struct.unpack_from("<H", data, 0x38)
    if phoff <= 0 or phentsize < 56 or phnum == 0:
        return None
    aligns: list[int] = []
    for index in range(phnum):
        offset = phoff + index * phentsize
        if offset + 56 > len(data):
            break
        (p_type,) = struct.unpack_from("<I", data, offset)
        if p_type != _PT_LOAD:
            continue
        (p_align,) = struct.unpack_from("<Q", data, offset + 0x30)
        aligns.append(p_align)
    return max(aligns) if aligns else None


def _data_offset(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> int:
    """
    Byte offset of the entry's payload inside the archive.

    ``ZipInfo.extra`` comes from the central directory; the local header
    carries its own (possibly different) extra field, so the payload offset
    has to be read from the local header itself.
    """
    source = archive.fp
    if source is None:  # pragma: no cover - open archives always expose it
        return info.header_offset
    source.seek(info.header_offset)
    header = source.read(30)
    if len(header) < 30:
        return info.header_offset
    name_len, extra_len = struct.unpack_from("<HH", header, 26)
    return int(info.header_offset) + 30 + int(name_len) + int(extra_len)


def _abi_of(name: str) -> str | None:
    """Extract the ABI component from a library path, if it has one."""
    parts = name.split("/")
    for candidate in _64_BIT_ABIS | {"armeabi-v7a", "x86"}:
        if candidate in parts:
            return candidate
    return None


def check_apk(path: Path) -> AlignmentReport:
    """
    Check both zip and ELF alignment for every native library in *path*.

    :raises OSError: when the APK cannot be read as a zip archive.
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
            report.libs.append(
                LibReport(
                    name=info.filename,
                    abi=_abi_of(info.filename),
                    mmapped=mmapped,
                    zip_offset=offset,
                    zip_aligned=zip_aligned,
                    elf_align=elf_max_p_align(archive.read(info.filename)),
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
        if lib.elf_align is None:
            report.errors.append(
                f"{lib.name}: not a 64-bit ELF library; cannot run on a "
                "16 KB page-size device"
            )
        elif lib.elf_align < REQUIRED_ALIGNMENT:
            report.errors.append(
                f"{lib.name}: PT_LOAD p_align is {lib.elf_align}, needs at "
                f"least {REQUIRED_ALIGNMENT}; rebuild with "
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
    except (OSError, zipfile.BadZipFile) as exc:
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
