#!/usr/bin/env python3
"""
novel_downloader.infra.sessions.response
----------------------------------------
"""

from __future__ import annotations

import codecs
import json
import re
from collections import defaultdict
from collections.abc import Iterator, Mapping, MutableMapping, Sequence
from typing import Any, Final

#: Charset parameter of a ``Content-Type`` header, e.g.
#: ``text/html; charset="GBK"``.  Quoted and unquoted values are accepted.
_CHARSET_RE: Final = re.compile(
    r"""charset\s*=\s*(?:"([^"]*)"|'([^']*)'|([^;\s]+))""",
    re.IGNORECASE,
)

#: Codecs tried after the declared one.  ``gb18030`` is a superset of both
#: ``gbk`` and ``gb2312``, so it comes first in the legacy-Chinese chain while
#: ``utf-8`` remains the preferred default for well-formed modern sites.
_FALLBACK_ENCODINGS: Final = ("utf-8", "gb18030", "gbk", "gb2312")

#: Byte-order marks mapped to the codec that both detects and strips them.
#: UTF-32 markers are checked first because they start with a UTF-16 marker.
_BOMS: Final = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


def _charset_from_headers(headers: Headers | Mapping[str, str]) -> str | None:
    """Return the charset declared by ``Content-Type``, if any."""
    content_type = headers.get("content-type")
    if not content_type:
        return None
    match = _CHARSET_RE.search(content_type)
    if match is None:
        return None
    charset = "".join(group for group in match.groups() if group).strip()
    return charset or None


def _normalise_encoding(encoding: str | None) -> str:
    """Strip whitespace and surrounding quotes from a charset name.

    Backends normally hand over a bare name, but a quoted value can leak
    through depending on how the header is scraped; accepting both keeps the
    three wrappers interchangeable.
    """
    value = encoding.strip() if encoding else ""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


def _bom_encoding(content: bytes) -> str | None:
    """Return the codec implied by a leading BOM, if the body has one."""
    for bom, encoding in _BOMS:
        if content.startswith(bom):
            return encoding
    return None


def _body_preview(text: str, limit: int = 120) -> str:
    """Collapse whitespace and truncate ``text`` for error messages."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit] + "..."


class Headers(MutableMapping[str, str]):
    __slots__ = ("_store",)

    def __init__(
        self,
        headers: Mapping[str, str] | Sequence[tuple[str, str]] | None = None,
    ) -> None:
        self._store: dict[str, list[str]] = defaultdict(list)
        if not headers:
            return

        if isinstance(headers, Mapping):
            for k, v in headers.items():
                self.add(k, v)
        else:
            for k, v in headers:
                self.add(k, v)

    def add(self, key: str, value: str | None) -> None:
        self._store[key.lower()].append(value or "")

    def get_all(self, key: str) -> list[str]:
        return self._store.get(key.lower(), [])

    def __getitem__(self, key: str) -> str:
        vals = self._store.get(key.lower())
        if not vals:
            raise KeyError(key)
        return vals[0]

    def __setitem__(self, key: str, value: str) -> None:
        self._store[key.lower()] = [value]

    def __delitem__(self, key: str) -> None:
        del self._store[key.lower()]

    def __iter__(self) -> Iterator[str]:
        return iter(self._store)

    def __len__(self) -> int:
        return len(self._store)

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        return key.lower() in self._store

    def __repr__(self) -> str:
        items_preview = ", ".join(f"{k}={len(v)}" for k, v in self._store.items())
        return f"<Headers ({items_preview})>"


class ResponseJSONError(ValueError):
    """Raised when a response body cannot be parsed as JSON.

    Subclasses :class:`ValueError` so existing ``except ValueError`` handlers
    keep working, while callers that need context can read :attr:`status`,
    :attr:`content_type`, :attr:`encoding` and :attr:`body_preview`.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int,
        content_type: str | None,
        encoding: str,
        body_preview: str,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.content_type = content_type
        self.encoding = encoding
        self.body_preview = body_preview


class BaseResponse:
    """Lightweight internal HTTP-like response used in fetcher."""

    __slots__ = ("content", "headers", "status", "encoding")

    def __init__(
        self,
        *,
        content: bytes,
        headers: Mapping[str, str] | Sequence[tuple[str, str]] | None = None,
        status: int = 200,
        encoding: str | None = None,
    ) -> None:
        self.content = content
        self.headers = Headers(headers)
        self.status = status
        # Backends pass the charset advertised by the server.  When the
        # response is built directly, fall back to the Content-Type header and
        # finally to UTF-8, so ``encoding`` is always a usable string.
        declared = _normalise_encoding(encoding)
        self.encoding = declared or _charset_from_headers(self.headers) or "utf-8"

    @property
    def text(self) -> str:
        """Decode the body, honouring BOMs and falling back safely.

        A leading BOM wins, then the charset declared by ``Content-Type``,
        then :attr:`encoding`, then the UTF-8/GB18030 chain.  Unknown codec
        names raise :class:`LookupError`, which is treated like any other
        failed attempt.  A final ``errors="replace"`` pass guarantees a string
        even for binary bodies, so this property never raises for arbitrary
        bytes.
        """
        candidates: list[str] = []
        bom_encoding = _bom_encoding(self.content)
        if bom_encoding is not None:
            candidates.append(bom_encoding)
        # The header is the most specific declaration and recovers quoted
        # charset values that a backend's extraction may have missed.
        header_charset = _charset_from_headers(self.headers)
        if header_charset:
            candidates.append(header_charset)
        if self.encoding:
            candidates.append(self.encoding)
        candidates.extend(_FALLBACK_ENCODINGS)

        seen: set[str] = set()
        for encoding in candidates:
            key = encoding.lower()
            if key in seen:
                continue
            seen.add(key)
            try:
                return self.content.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue

        # ``errors="replace"`` cannot raise, even when the body is binary or
        # the declared codec name is invalid.
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        """Parse the decoded body as JSON.

        :raises ResponseJSONError: If the body is not valid JSON.  The message
            carries the status, encoding, parser position and a body preview.
        """
        text = self.text
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            content_type = self.headers.get("content-type")
            preview = _body_preview(text)
            details = [
                f"status={self.status}",
                f"encoding={self.encoding!r}",
            ]
            if content_type:
                details.append(f"content-type={content_type!r}")
            body = f"body starts with {preview!r}" if preview else "body is empty"
            message = (
                "Failed to parse response body as JSON "
                f"({', '.join(details)}): {exc.msg} "
                f"at line {exc.lineno} column {exc.colno}; {body}"
            )
            raise ResponseJSONError(
                message,
                status=self.status,
                content_type=content_type,
                encoding=self.encoding,
                body_preview=preview,
            ) from exc

    @property
    def ok(self) -> bool:
        """Return True if HTTP status code is under 400."""
        return self.status < 400

    def __repr__(self) -> str:
        return f"<BaseResponse status={self.status} len={len(self.content)}>"
