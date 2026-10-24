#!/usr/bin/env python3
"""
novel_downloader.infra.cookies
------------------------------

Utility for normalizing cookie input from user configuration.
"""

__all__ = ["parse_cookies", "CookieStore"]

import json
import re
from collections.abc import Mapping
from pathlib import Path

# Cookie pairs are normally separated by ``;``. Users frequently paste values
# that also contain CR/LF (copied from a multi-line editor) and, less often,
# comma-separated pairs.  Commas are ambiguous because they are legal inside a
# cookie value (``Expires=Wed, 21 Oct ...``), so a comma is only treated as a
# separator when it is followed by something that looks like a new ``name=``
# pair.
_PAIR_SEPARATORS = re.compile(r"[;\r\n]+")
_COMMA_PAIR_START = re.compile(r",\s*(?=[^=;,\s]+=)")
# Names must not contain whitespace, control characters, ``;`` or ``,``.
_INVALID_NAME_CHARS = re.compile(r"[\x00-\x20\x7f;,]")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _is_valid_cookie_name(name: str) -> bool:
    """Return True when *name* can safely be used as a cookie name."""
    return bool(name) and _INVALID_NAME_CHARS.search(name) is None


def _strip_control_chars(value: str) -> str:
    """Remove control characters (including NUL and stray CR/LF)."""
    return _CONTROL_CHARS.sub("", value)


def _normalize_pair(key: str, value: str) -> tuple[str, str] | None:
    """Trim a raw name/value pair, returning ``None`` for unusable names."""
    key = key.strip()
    value = _strip_control_chars(value.strip())
    if not _is_valid_cookie_name(key):
        return None
    return key, value


def parse_cookies(cookies: str | Mapping[str, str]) -> dict[str, str]:
    """
    Parse cookies from a string or dictionary into a standard dictionary.

    Supports input like:
      * `"key1=value1; key2=value2"`
      * `"key1=value1\\r\\nkey2=value2"` (pasted multi-line values)
      * `"key1=value1, key2=value2"` (comma separated)
      * `{"key1": "value1", "key2": "value2"}`

    Values may contain ``=`` and commas; only the first ``=`` of a pair is
    treated as the separator, and a comma is split on only when the following
    text looks like another ``name=value`` pair.  Empty names, names containing
    whitespace/control characters, and malformed fragments are ignored.

    :param cookies: Cookie string or dict-like object (e.g., from config)
    :return: A normalized cookie dictionary (key -> value)
    :raises TypeError: If the input is neither string nor dict-like
    """
    if isinstance(cookies, str):
        text = _COMMA_PAIR_START.sub(";", cookies.lstrip("\ufeff"))
        result: dict[str, str] = {}
        for part in _PAIR_SEPARATORS.split(text):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            pair = _normalize_pair(key, value)
            if pair is not None:
                result[pair[0]] = pair[1]
        return result
    elif isinstance(cookies, Mapping):
        result = {}
        for raw_key, raw_value in cookies.items():
            pair = _normalize_pair(str(raw_key), str(raw_value))
            if pair is not None:
                result[pair[0]] = pair[1]
        return result
    raise TypeError("Unsupported cookie format: must be str or dict-like")


class CookieStore:
    """
    A simple cookie storage and loader utility that reads cookies from multiple
    supported client formats and caches them in memory.

    Supported cookie files (by default):
      * aiohttp.cookies
      * curl_cffi.cookies
      * httpx.cookies
    """

    DEFAULT_FILENAMES = ["aiohttp.cookies", "curl_cffi.cookies", "httpx.cookies"]

    def __init__(self, cookies_dir: Path, filenames: list[str] | None = None) -> None:
        """
        Initialize a new CookieStore.

        :param cookies_dir: Directory containing cookie state files.
        :param filenames: Optional list of cookie filenames to read.
        """
        self.cookies_dir = cookies_dir
        self.filenames = filenames or self.DEFAULT_FILENAMES
        self.cache: dict[str, str] = {}
        self.mtimes: dict[str, float] = {}
        # Last successfully parsed content per file, used to rebuild ``cache``
        # when a file changes and drops a cookie.
        self._file_cookies: dict[str, dict[str, str]] = {}

    def get(self, key: str) -> str:
        """
        Retrieve a cookie value by name.

        :param key: The name of the cookie to retrieve.
        :return: The cookie value if found, otherwise an empty string.
        """
        self.load_all()
        return self.cache.get(key, "")

    def load_all(self) -> None:
        """
        Load or refresh cookies from all known cookie files.

        For each configured cookie file, this method:
          * Checks whether the file exists.
          * Compares its modification time (`mtime`) to the last cached value.
          * If changed, reads and parses the JSON content.
          * Extracts valid `name`/`value` string pairs into the in-memory cache.

        Malformed files and entries are ignored.  When a file changes (or is
        removed), the merged cache is rebuilt so deleted cookies do not linger.
        """
        changed = False
        for filename in self.filenames:
            state_file = self.cookies_dir / filename
            if not state_file.exists():
                if self._file_cookies.pop(filename, None) is not None:
                    self.mtimes.pop(filename, None)
                    changed = True
                continue
            try:
                mtime = state_file.stat().st_mtime
                if self.mtimes.get(filename) == mtime:
                    continue
                data = json.loads(state_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue

            self.mtimes[filename] = mtime
            entries: dict[str, str] = {}
            if isinstance(data, list):
                for entry in data:
                    if not isinstance(entry, Mapping):
                        continue
                    name = entry.get("name")
                    value = entry.get("value")
                    if not isinstance(name, str) or not isinstance(value, str):
                        continue
                    if not _is_valid_cookie_name(name):
                        continue
                    entries[name] = _strip_control_chars(value)
            self._file_cookies[filename] = entries
            changed = True

        if changed:
            merged: dict[str, str] = {}
            for filename in self.filenames:
                merged.update(self._file_cookies.get(filename, {}))
            self.cache = merged
