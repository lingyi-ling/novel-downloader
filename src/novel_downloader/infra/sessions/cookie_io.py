#!/usr/bin/env python3
"""
novel_downloader.infra.sessions.cookie_io
---------------------------------------------

Shared helpers for reading and writing session cookie files.

The legacy on-disk format is a JSON list of objects with ``name`` and
``value`` strings, optionally extended with ``domain`` and ``path``.  It is
still accepted and keeps its historical shape when read, so an old file is
never rewritten into a lossy in-memory representation.

New writes keep the same top-level list (readers such as
:class:`novel_downloader.infra.cookies.CookieStore` keep working) but every
entry carries ``version`` plus the attributes a browser needs to replay the
cookie faithfully: ``secure``, ``http_only``, ``same_site``, ``expires``,
``max_age``, ``domain_specified``, ``domain_initial_dot``,
``path_specified`` and ``rest``.

``expires`` is an absolute Unix timestamp; ``None`` means a session cookie
and is never replaced by an invented deadline.  Entries whose deadline has
passed are dropped on load.  Malformed entries are skipped instead of
raising, and cookie values are never logged.
"""

from __future__ import annotations

import contextlib
import json
import math
import ntpath
import os
import tempfile
import time
from collections.abc import Iterable, Mapping
from datetime import UTC
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Final

from novel_downloader.infra.cookies import (
    MAX_COOKIE_FILE_BYTES,
    _is_valid_cookie_name,
    _strip_control_chars,
)

__all__ = [
    "COOKIE_ITEM_VERSION",
    "DEFAULT_SAME_SITE",
    "MAX_COOKIE_FILE_BYTES",
    "load_cookie_items",
    "resolve_cookie_path",
    "write_cookie_items",
]

#: Schema marker written on every entry by :func:`write_cookie_items`.  An
#: entry without it is an old ``name``/``value``/``domain``/``path`` record.
COOKIE_ITEM_VERSION: Final = 1

#: Value used when a file carries an unusable SameSite token.  Browsers fall
#: back to Lax for an invalid/absent SameSite attribute.
DEFAULT_SAME_SITE: Final = "Lax"

_SAME_SITE_VALUES: Final = {
    "strict": "Strict",
    "lax": "Lax",
    "none": "None",
}

#: Fields that only the richer, versioned entries carry.  Their presence (or
#: the ``version`` marker itself) selects the rich parser.
_RICH_FIELDS: Final = frozenset(
    {
        "version",
        "secure",
        "http_only",
        "same_site",
        "expires",
        "max_age",
        "domain_specified",
        "domain_initial_dot",
        "path_specified",
        "rest",
    }
)

_WINDOWS_RESERVED_NAMES: Final = frozenset(
    {"con", "prn", "aux", "nul", "clock$"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


def resolve_cookie_path(cookies_dir: Path, filename: str) -> Path:
    """
    Join *filename* onto *cookies_dir*, rejecting path traversal attempts.

    Only a plain file name is accepted: directory separators, drive letters,
    drive-relative names (``C:foo``), NTFS alternate data streams (``a:b``),
    reserved device names and trailing dots/spaces that Windows silently
    normalizes are all refused.

    :param cookies_dir: Directory that must contain the cookie file.
    :param filename: Plain file name (no directory components).
    :return: The resolved path.
    :raises ValueError: If the file name is empty or unsafe.
    """
    if (
        not filename
        or filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or "\x00" in filename
        or ":" in filename
    ):
        raise ValueError(f"Invalid cookie filename: {filename!r}")
    if ntpath.splitdrive(filename)[0]:
        raise ValueError(f"Invalid cookie filename: {filename!r}")
    # Windows strips trailing dots and spaces from path components, so the
    # name that lands on disk can silently differ from the one we validated.
    if filename != filename.rstrip(" ."):
        raise ValueError(f"Invalid cookie filename: {filename!r}")
    stem = filename.split(".", 1)[0].strip().lower()
    if stem in _WINDOWS_RESERVED_NAMES:
        raise ValueError(f"Invalid cookie filename: {filename!r}")
    return Path(cookies_dir) / filename


def _read_payload(path: Path) -> tuple[bool, Any]:
    """
    Read *path* as JSON, refusing oversized and deeply nested documents.

    :return: ``(True, parsed)`` or ``(False, None)``; never raises for bad
        input.
    """
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_COOKIE_FILE_BYTES + 1)
    except OSError:
        return False, None
    if len(raw) > MAX_COOKIE_FILE_BYTES:
        return False, None
    try:
        # utf-8-sig tolerates a BOM saved by Windows editors.
        return True, json.loads(raw.decode("utf-8-sig"))
    except (ValueError, RecursionError):
        return False, None


def _looks_rich(entry: Mapping[str, Any]) -> bool:
    """Whether *entry* uses the versioned format with security attributes."""
    return any(field in entry for field in _RICH_FIELDS)


def _as_bool(value: Any, default: bool = False) -> bool:
    """Coerce a JSON value to ``bool``, falling back to *default*."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off", ""}:
            return False
    return default


def _as_expiry(value: Any) -> float | None:
    """Parse an absolute expiry (epoch number or HTTP date), or ``None``."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        result = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            result = float(text)
        except ValueError:
            try:
                parsed = parsedate_to_datetime(text)
            except (TypeError, ValueError, OverflowError):
                return None
            if parsed is None:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            try:
                result = parsed.timestamp()
            except (OverflowError, OSError, ValueError):
                return None
    else:
        return None
    if not math.isfinite(result):
        return None
    return result


def _as_max_age(value: Any) -> int | None:
    """Parse a relative lifetime in seconds, or ``None`` when unusable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _normalize_same_site(value: Any) -> str | None:
    """
    Canonicalize a SameSite token.

    ``None``/empty means "attribute absent".  A non-empty token that is not
    Strict/Lax/None falls back to :data:`DEFAULT_SAME_SITE` instead of
    propagating an invalid value (or raising).
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return _SAME_SITE_VALUES.get(text.lower(), DEFAULT_SAME_SITE)


def _normalize_rest(value: Any) -> dict[str, Any]:
    """Return a JSON-safe mapping of unmodelled cookie attributes."""
    if not isinstance(value, Mapping):
        return {}
    rest: dict[str, Any] = {}
    for raw_key, raw_value in value.items():
        if not isinstance(raw_key, str):
            continue
        key = _strip_control_chars(raw_key).strip()
        if not key:
            continue
        if raw_value is None or isinstance(raw_value, str):
            rest[key] = (
                raw_value if raw_value is None else _strip_control_chars(raw_value)
            )
        elif isinstance(raw_value, bool | int | float):
            if isinstance(raw_value, float) and not math.isfinite(raw_value):
                continue
            rest[key] = raw_value
    return rest


def _rest_value(rest: Mapping[str, Any], name: str) -> Any:
    """Case-insensitive lookup in the ``rest`` attribute bag."""
    target = name.lower()
    for key, value in rest.items():
        if isinstance(key, str) and key.lower() == target:
            return value
    return None


def _rest_has(rest: Mapping[str, Any], name: str) -> bool:
    """Whether the ``rest`` bag contains *name* (case-insensitively)."""
    target = name.lower()
    return any(isinstance(key, str) and key.lower() == target for key in rest)


def _cookie_rest(rest: Mapping[str, Any]) -> dict[str, Any]:
    """Convert a normalized ``rest`` bag to the shape ``Cookie`` expects."""
    return {
        key: None if value is None else str(value)
        for key, value in rest.items()
        if isinstance(key, str)
    }


def _normalize_legacy_item(entry: Mapping[str, Any]) -> dict[str, Any] | None:
    """Normalize an old ``name``/``value``/``domain``/``path`` entry."""
    name = entry.get("name")
    value = entry.get("value")
    if not isinstance(name, str) or not isinstance(value, str):
        return None
    if not _is_valid_cookie_name(name):
        return None

    item: dict[str, Any] = {"name": name, "value": _strip_control_chars(value)}
    domain = entry.get("domain")
    path_value = entry.get("path")
    if isinstance(domain, str) and domain:
        item["domain"] = domain
    if isinstance(path_value, str) and path_value.startswith("/"):
        item["path"] = path_value
    return item


def _normalize_rich_item(
    entry: Mapping[str, Any],
    now: float,
    *,
    drop_expired: bool,
) -> dict[str, Any] | None:
    """Normalize a versioned entry, optionally dropping it when expired."""
    name = entry.get("name")
    value = entry.get("value")
    if not isinstance(name, str) or not isinstance(value, str):
        return None
    if not _is_valid_cookie_name(name):
        return None

    rest = _normalize_rest(entry.get("rest"))

    domain_raw = entry.get("domain")
    domain = (
        _strip_control_chars(domain_raw).strip() if isinstance(domain_raw, str) else ""
    )
    domain_specified = _as_bool(entry.get("domain_specified"), bool(domain))
    domain_initial_dot = _as_bool(
        entry.get("domain_initial_dot"), domain.startswith(".")
    )

    path_raw = entry.get("path")
    path = _strip_control_chars(path_raw) if isinstance(path_raw, str) else ""
    if not path.startswith("/"):
        path = "/"
    path_specified = _as_bool(
        entry.get("path_specified"),
        isinstance(path_raw, str) and path_raw.startswith("/"),
    )

    secure = _as_bool(entry.get("secure"), _rest_has(rest, "secure"))
    http_only = _as_bool(entry.get("http_only"), _rest_has(rest, "httponly"))

    same_site = _normalize_same_site(entry.get("same_site"))
    if same_site is None:
        same_site = _normalize_same_site(_rest_value(rest, "samesite"))

    max_age = _as_max_age(entry.get("max_age"))
    if max_age is None:
        max_age = _as_max_age(_rest_value(rest, "max-age"))

    expires = _as_expiry(entry.get("expires"))
    if expires is None:
        expires = _as_expiry(_rest_value(rest, "expires"))

    if expires is None and max_age is not None and max_age > 0:
        # A relative lifetime without an absolute deadline: pin it now so a
        # later load does not extend the cookie's life.
        expires = now + max_age

    if drop_expired:
        # max-age <= 0 means "delete now" per RFC 6265.  A session cookie has
        # no expires at all and must never be treated as already expired.
        if max_age is not None and max_age <= 0:
            return None
        if expires is not None and expires <= now:
            return None

    version: int = COOKIE_ITEM_VERSION
    raw_version = entry.get("version")
    if raw_version is not None and not isinstance(raw_version, bool):
        try:
            candidate = int(raw_version)
        except (TypeError, ValueError, OverflowError):
            candidate = COOKIE_ITEM_VERSION
        if candidate > 0:
            version = candidate

    return {
        "name": name,
        "value": _strip_control_chars(value),
        "version": version,
        "domain": domain,
        "path": path,
        "secure": secure,
        "http_only": http_only,
        "same_site": same_site,
        "expires": expires,
        "max_age": max_age,
        "domain_specified": domain_specified,
        "domain_initial_dot": domain_initial_dot,
        "path_specified": path_specified,
        "rest": rest,
    }


def load_cookie_items(path: Path) -> list[dict[str, Any]] | None:
    """
    Read a cookie JSON file and return normalized entries.

    Old files (a bare list of ``name``/``value`` entries) load with exactly
    their historical shape.  Versioned entries additionally report
    ``version``, ``secure``, ``http_only``, ``same_site``, ``expires``,
    ``max_age``, ``domain_specified``, ``domain_initial_dot``,
    ``path_specified`` and ``rest``.  Expired cookies are discarded.

    :param path: Cookie file path.
    :return: List of normalized entry dicts, or ``None`` when the file cannot
        be read or its top-level document is not a cookie list.
    """
    ok, data = _read_payload(path)
    if not ok:
        return None

    if isinstance(data, list):
        entries = data
    elif isinstance(data, Mapping):
        # Tolerate the object wrapper some tools emit; the canonical writer
        # keeps a bare list so older readers continue to work.
        wrapped = data.get("cookies")
        if not isinstance(wrapped, list):
            return None
        entries = wrapped
    else:
        return None

    now = time.time()
    items: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        if _looks_rich(entry):
            item = _normalize_rich_item(entry, now, drop_expired=True)
        else:
            item = _normalize_legacy_item(entry)
        if item is not None:
            items.append(item)

    return items


def _prepare_write_item(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Return the versioned representation of one entry for serialization."""
    out = dict(entry)
    name = entry.get("name")
    value = entry.get("value")
    if (
        not isinstance(name, str)
        or not isinstance(value, str)
        or not _is_valid_cookie_name(name)
    ):
        # Preserve the old writer's behaviour: an unusable entry is still
        # serialized (and json.dumps reports unserializable values).
        return out

    normalized = _normalize_rich_item(entry, time.time(), drop_expired=False)
    if normalized is None:
        return out
    merged = {**out, **normalized}
    merged["version"] = COOKIE_ITEM_VERSION
    return merged


def write_cookie_items(path: Path, items: Iterable[Mapping[str, Any]]) -> None:
    """
    Atomically write cookie entries to *path* in the versioned format.

    The temporary file is created next to the destination and moved into
    place with :func:`os.replace`, so readers never observe a half-written
    file.  On platforms that support it, the file is restricted to the owner.

    :param path: Destination file path.
    :param items: Cookie entry mappings to serialize.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload_items = [_prepare_write_item(item) for item in items]
    payload = json.dumps(payload_items, ensure_ascii=False, indent=2)

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # Best effort: Windows does not implement POSIX permission bits.
        with contextlib.suppress(OSError):
            os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
