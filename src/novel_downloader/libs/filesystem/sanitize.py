#!/usr/bin/env python3
"""
novel_downloader.libs.filesystem.sanitize
-----------------------------------------

Utility functions for cleaning and validating filenames for safe use
on different operating systems.
"""

__all__ = ["sanitize_filename"]

import os
import re

_WIN_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

_SANITIZE_PATTERN_WIN = re.compile(r'[<>:"/\\|?*\x00-\x1F]')
_SANITIZE_PATTERN_POSIX = re.compile(r"[/\x00]")
_WHITESPACE_RUN = re.compile(r"\s+")

#: POSIX caps a single path component at 255 *bytes*; Windows caps the whole
#: path at 260 UTF-16 units.  A CJK name has three bytes per character, so a
#: character-only limit still overflows on Linux (255 chars = 765 bytes).
_MAX_NAME_BYTES = 255


def _truncate_bytes(text: str, limit: int) -> str:
    """Cut ``text`` to at most ``limit`` UTF-8 bytes without splitting a char."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    return encoded[:limit].decode("utf-8", "ignore")


def sanitize_filename(
    filename: str,
    max_length: int | None = 255,
    *,
    collapse_whitespace: bool = False,
    fallback: str = "_untitled",
) -> str:
    """
    Sanitize the given filename by replacing characters
    that are invalid in file paths with '_'.

    This function checks the operating system environment and applies the appropriate
    filtering rules:
      * On Windows, it replaces characters: <>:"/\\|?*
      * On POSIX systems, it replaces the forward slash '/'

    :param filename: The input filename to sanitize.
    :param max_length: Optional maximum length of the output filename. Defaults to 255.
    :param collapse_whitespace: When true, collapse runs of whitespace (spaces,
        tabs, newlines and other Unicode whitespace) into a single space after
        the invalid-character replacement.
    :param fallback: Returned when the sanitized result would be empty.
    :return: The sanitized filename as a string.
    """
    pattern = _SANITIZE_PATTERN_WIN if os.name == "nt" else _SANITIZE_PATTERN_POSIX

    name = pattern.sub("_", filename)
    if collapse_whitespace:
        name = _WHITESPACE_RUN.sub(" ", name)
    name = name.strip(" .")

    stem, dot, ext = name.rpartition(".")
    if dot == "":  # no dot found
        stem, ext = name, ""
        dot = ""
    # Windows reserves the portion before the *first* dot, so "NUL.tar.gz" is
    # reserved just like "NUL.txt". Trailing spaces before the dot are ignored
    # by Windows too ("CON .txt"), so normalise before checking.
    device_name = name.split(".", 1)[0].strip(" .")
    if os.name == "nt" and device_name.upper() in _WIN_RESERVED_NAMES:
        stem = f"_{stem}"
    cleaned = f"{stem}{dot}{ext}" if ext else stem

    if max_length is not None and max_length > 0 and len(cleaned) > max_length:
        if ext:
            keep = max_length - len(ext) - 1
            # If the extension alone does not fit, fall back to a plain
            # truncation; otherwise the result could exceed max_length.
            cleaned = f"{cleaned[:keep]}.{ext}" if keep > 0 else cleaned[:max_length]
        else:
            cleaned = cleaned[:max_length]
        # Truncation may expose trailing spaces/dots (invalid on Windows).
        cleaned = cleaned.rstrip(" .") or fallback

    # A character limit is not enough on POSIX, where the per-component cap is
    # counted in bytes.  Enforce both so the same name is writable everywhere.
    if len(cleaned.encode("utf-8")) > _MAX_NAME_BYTES:
        if ext:
            room = _MAX_NAME_BYTES - len(ext.encode("utf-8")) - 1
            cleaned = _truncate_bytes(stem, max(room, 1)) + f".{ext}"
        else:
            cleaned = _truncate_bytes(cleaned, _MAX_NAME_BYTES)
        cleaned = cleaned.rstrip(" .") or fallback

    return cleaned or fallback
