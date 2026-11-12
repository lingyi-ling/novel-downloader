#!/usr/bin/env python3
"""
novel_downloader.plugins.base.fetcher
-------------------------------------

Abstract base class providing common HTTP session handling for fetchers.
"""

import abc
import asyncio
import contextlib
import hashlib
import logging
import random
import types
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Literal, Self
from urllib.parse import urljoin

from novel_downloader.infra.http_defaults import DEFAULT_USER_HEADERS, IMAGE_HEADERS
from novel_downloader.infra.i18n import t
from novel_downloader.infra.sessions import create_session
from novel_downloader.infra.sessions.base import (
    NonRetryableConnectionError,
    TooManyRedirectsError,
)
from novel_downloader.infra.sessions.response import BaseResponse
from novel_downloader.libs.filesystem import (
    font_filename,
    image_filename,
    sanitize_filename,
    write_file,
)
from novel_downloader.libs.time_utils import async_jitter_sleep
from novel_downloader.plugins.base.errors import (
    RETRYABLE_STATUS,
    HttpStatusError,
    RedirectLimitError,
    TransportError,
)
from novel_downloader.plugins.utils.rate_limiter import TokenBucketRateLimiter
from novel_downloader.schemas import FetcherConfig, LoginField, MediaResource

logger = logging.getLogger(__name__)

#: A server may ask us to wait an hour; downloads should not stall that long.
_MAX_RETRY_AFTER = 30.0

#: Upper bound for the locally computed exponential backoff.
_MAX_BACKOFF = 60.0

#: Extra wait added on top of a server-supplied ``Retry-After`` so concurrent
#: workers that received the same header do not retry in lockstep.  The jitter
#: is upward-only, so the server's minimum delay is still respected.
_RETRY_AFTER_JITTER_MAX = 1.0
_RETRY_AFTER_JITTER_FRACTION = 0.1

#: Statuses whose ``Retry-After`` describes site-wide pressure and therefore
#: pauses every worker sharing the rate limiter, not just this request.  A 5xx
#: without the header may be one broken route rather than a rate limit, so it
#: keeps the local exponential backoff only.
_SHARED_COOLDOWN_STATUS: frozenset[int] = frozenset({429, 503})

#: Ceiling for a single media payload.  The number is generous for an inline
#: illustration while still refusing a multi-gigabyte body that would fill the
#: disk (or a cache quota) from one buggy or hostile URL.
DEFAULT_MAX_MEDIA_BYTES = 64 * 1024 * 1024

#: Content types that are never a media body.  A login wall, an error document
#: and an expired-URL JSON response are served with 200 often enough that the
#: status code alone is not a completeness signal.
_REJECTED_MEDIA_CONTENT_TYPES: frozenset[str] = frozenset(
    {
        "application/json",
        "application/problem+json",
        "application/xhtml+xml",
        "application/xml",
        "text/xml",
    }
)

#: First bytes of an HTML document (BOM/whitespace already stripped).
_HTML_PREFIXES: tuple[bytes, ...] = (
    b"<!doctype html",
    b"<html",
    b"<head",
    b"<body",
)


def _media_content_type(headers: Mapping[str, str]) -> str:
    """Return the lower-cased ``Content-Type`` media type, if declared."""
    raw = headers.get("content-type") or headers.get("Content-Type") or ""
    return str(raw).split(";", 1)[0].strip().lower()


def _looks_like_html(content: bytes) -> bool:
    """Heuristic: does this body start with an HTML document?"""
    head = content[:256].lstrip().lower()
    return head.startswith(_HTML_PREFIXES)


def _unique_non_empty(urls: list[str]) -> list[str]:
    """Strip and de-duplicate URLs while preserving their first-seen order."""
    return list(dict.fromkeys(u.strip() for u in urls if u and u.strip()))


def _non_empty_file(path: Path) -> bool:
    """Return whether *path* exists and holds at least one byte."""
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


def _media_rejection_reason(
    content: bytes,
    headers: Mapping[str, str],
    max_bytes: int,
) -> str | None:
    """
    Return why *content* must not be stored as media, or ``None`` if it may.

    Missing or generic content types (``application/octet-stream``, no header)
    are accepted: many CDNs and mirror hosts do not label images.  Only a
    clearly textual/document type or an HTML body is refused, so a perfect
    server response is never dropped on a guess.
    """
    if len(content) > max_bytes:
        return f"payload too large ({len(content)} bytes > {max_bytes})"

    content_type = _media_content_type(headers)
    if (
        content_type.startswith("text/")
        or content_type in _REJECTED_MEDIA_CONTENT_TYPES
    ):
        return f"non-media content-type {content_type!r}"

    if _looks_like_html(content):
        return "HTML document instead of media"

    return None


def _retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    """
    Parse a ``Retry-After`` header into a bounded delay.

    Accepts both forms the RFC allows: a number of seconds, or an HTTP date.

    :param headers: Response headers (case-insensitive lookup).
    :return: Seconds to wait, capped at :data:`_MAX_RETRY_AFTER`; ``None``
        when the header is absent or unparseable.
    """
    raw = headers.get("Retry-After") or headers.get("retry-after")
    if not raw:
        return None
    text = str(raw).strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        # HTTP-dates are GMT; a parser that returns a naive datetime (some
        # malformed variants) must not be interpreted in the local timezone.
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - datetime.now(UTC)).total_seconds()
    if seconds != seconds:  # NaN
        return None
    return max(0.0, min(seconds, _MAX_RETRY_AFTER))


class BaseFetcher(abc.ABC):
    """
    BaseFetcher wraps basic HTTP operations.
    """

    site_name: str
    BASE_URL_MAP: dict[str, str] = {}
    DEFAULT_BASE_URL: str = ""
    IMAGE_HEADERS: dict[str, str] = IMAGE_HEADERS
    #: Safety valve for :meth:`_paginate`: a site that always confirms a next
    #: page must not grow the page list (and the request count) without bound.
    MAX_PAGES: int = 500
    #: Hard ceiling for one image/font payload.  Sites that legitimately host
    #: larger artwork can raise it on the subclass.
    MAX_MEDIA_BYTES: int = DEFAULT_MAX_MEDIA_BYTES

    def __init__(
        self,
        config: FetcherConfig,
        cookies: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        """
        Initialize the async session with configuration.

        :param config: Configuration object for session behavior
        :param cookies: Optional initial cookies to set on the session.
        """
        self._base_url = self._resolve_base_url(config.locale_style)
        self._backoff_factor = config.backoff_factor
        self._request_interval = config.request_interval
        self._retry_times = config.retry_times
        self._timeout = config.timeout
        self._is_logged_in = False

        self._cache_dir = Path(config.cache_dir) / self.site_name

        self.session = create_session(
            backend=config.backend,
            cfg=config,
            cookies=cookies,
            **kwargs,
        )

        self._rate_limiter: TokenBucketRateLimiter | None = (
            TokenBucketRateLimiter(config.max_rps) if config.max_rps > 0 else None
        )

        # Per-destination serialisation for media writes.  Concurrent chapter
        # workers (and duplicate URLs inside one chapter) otherwise download
        # the same image several times and race on the same path.  Entries are
        # reference-counted and removed when the last waiter leaves, so the
        # dict stays proportional to the in-flight set, not the book size.
        self._media_locks: dict[Path, tuple[asyncio.Lock, int]] = {}

    async def init(
        self,
        **kwargs: Any,
    ) -> None:
        """"""
        await self.session.init()

    async def close(self) -> None:
        """
        Shutdown and clean up any resources.
        """
        await self.session.close()

    async def login(
        self,
        username: str = "",
        password: str = "",
        cookies: dict[str, str] | None = None,
        attempt: int = 1,
        **kwargs: Any,
    ) -> bool:
        """
        Attempt to log in asynchronously.

        :returns: True if login succeeded.
        """
        if not cookies:
            return False
        self.session.update_cookies(cookies)

        self._is_logged_in = await self._check_login_status()
        return self._is_logged_in

    @abc.abstractmethod
    async def fetch_book_info(
        self,
        book_id: str,
        **kwargs: Any,
    ) -> list[str]:
        """
        Fetch the raw HTML (or JSON) of the book info page asynchronously.

        :param book_id: The book identifier.
        :return: The page content as string list.
        """
        ...

    @abc.abstractmethod
    async def fetch_chapter_content(
        self,
        book_id: str,
        chapter_id: str,
        **kwargs: Any,
    ) -> list[str]:
        """
        Fetch the raw HTML (or JSON) of a single chapter asynchronously.

        :param book_id: The book identifier.
        :param chapter_id: The chapter identifier.
        :return: The page content as string list.
        """
        ...

    async def fetch_data(self, url: str, **kwargs: Any) -> bytes | None:
        """
        Fetch arbitrary binary data from a remote URL.

        Media (covers, inline images, fonts) shares the same retry budget and
        rate limiter as page fetches: a burst of images used to bypass
        ``max_rps`` and give up on the first connection reset.

        :param url: The target URL.
        :return: raw content if success, else None
        """
        response = await self._fetch_data_response(url, **kwargs)
        return response.content if response is not None else None

    async def _fetch_data_response(
        self, url: str, **kwargs: Any
    ) -> BaseResponse | None:
        """
        Fetch binary data and keep the response headers.

        The media path needs ``Content-Type`` to tell an image from a login
        page, so the retry loop returns the whole response and
        :meth:`fetch_data` unwraps the body for existing callers.

        :param url: The target URL.
        :return: the successful response, or ``None`` when the fetcher's
            retries are exhausted.
        """
        for attempt in range(self._retry_times + 1):
            if self._rate_limiter:
                await self._rate_limiter.wait()
            try:
                resp = await self.session.get(url, **kwargs)
            except TooManyRedirectsError as exc:
                # Media is best-effort: an unusable redirect target must not
                # abort the chapter (or the whole book) that referenced it, and
                # repeating the same chain cannot change the answer.
                logger.warning(
                    "Too many redirects (site=%s) %s: %s",
                    self.site_name,
                    url,
                    exc,
                )
                return None
            except NonRetryableConnectionError as exc:
                # Bad certificate / proxy credentials: media is best-effort,
                # and every retry would fail the same way.
                logger.warning(
                    "Request failed (site=%s) %s: %s (not retrying)",
                    self.site_name,
                    url,
                    exc,
                )
                return None
            except ValueError as exc:
                # A malformed/unsupported URL is deterministic.  Media is
                # best-effort, so one broken reference must not abort the
                # chapter or the book.
                logger.warning(
                    "Invalid media URL (site=%s) %s: %s",
                    self.site_name,
                    url,
                    exc,
                )
                return None
            except (TimeoutError, ConnectionError) as exc:
                if attempt < self._retry_times:
                    logger.info(
                        "Retrying media (site=%s, attempt=%d/%d) %s: %s",
                        self.site_name,
                        attempt + 1,
                        self._retry_times,
                        url,
                        exc,
                    )
                    await self._retry_backoff(attempt)
                    continue
                logger.warning(
                    "Request failed (site=%s) %s: %s",
                    self.site_name,
                    url,
                    exc,
                )
                return None

            if not resp.ok:
                if resp.status in RETRYABLE_STATUS and attempt < self._retry_times:
                    delay = _retry_after_seconds(resp.headers)
                    if resp.status in _SHARED_COOLDOWN_STATUS and delay is not None:
                        covered = self._apply_shared_cooldown(delay)
                        if covered > 0 and covered >= delay:
                            # Covered by the next ``wait()``; no local sleep.
                            continue
                        if covered > 0:
                            delay -= covered
                    await self._retry_backoff(attempt, delay)
                    continue
                # Out of retries (or a status we cannot retry): still pass a
                # rate-limit header on to the peers sharing this limiter.
                self._register_shared_cooldown(resp.status, resp.headers)
                logger.warning(
                    "Request failed (site=%s) %s: HTTP %s",
                    self.site_name,
                    url,
                    resp.status,
                )
                return None

            if not resp.content:
                logger.warning(
                    "Empty response (site=%s): %s",
                    self.site_name,
                    url,
                )
                return None

            return resp

        raise AssertionError("unreachable: the retry loop always returns or raises")

    async def fetch_media(
        self,
        media_dir: Path,
        resources: list[MediaResource],
        *,
        on_exist: Literal["overwrite", "skip"] = "skip",
        concurrent: int = 5,
    ) -> None:
        """
        Process and persist a list of media resources asynchronously.

        :param media_dir: Destination directory.
        :param resources: List of :class:`MediaResource` items.
        :param on_exist: Behavior when existing files are found.
        :param concurrent: Number of concurrent tasks per batch.
        """
        if not resources:
            return

        img_urls: list[str] = []
        font_urls: list[str] = []

        for res in resources:
            url = res.get("url")
            if not url:
                continue

            r_type = res.get("type")
            if r_type == "image":
                img_urls.append(url)
            elif r_type == "font":
                font_urls.append(url)

        if img_urls:
            await self.fetch_images(
                media_dir,
                img_urls,
                on_exist=on_exist,
                concurrent=concurrent,
            )

        if font_urls:
            await self.fetch_fonts(
                media_dir,
                font_urls,
                on_exist=on_exist,
                concurrent=concurrent,
            )

    async def fetch_image(
        self,
        url: str,
        img_dir: Path,
        *,
        name: str | None = None,
        on_exist: Literal["overwrite", "skip"] = "skip",
    ) -> Path | None:
        """
        Download a single image and return its saved path.

        :param url: Image URL.
        :param img_dir: Destination folder.
        :param name: Optional explicit filename (without suffix).
        :param on_exist: What to do when file exists.
        :return: Path of saved image, or None if failed/skipped.
        """
        return await self._fetch_one_image(url, img_dir, name=name, on_exist=on_exist)

    async def fetch_images(
        self,
        img_dir: Path,
        urls: list[str],
        *,
        on_exist: Literal["overwrite", "skip"] = "skip",
        concurrent: int = 5,
    ) -> None:
        """
        Download images to `img_dir` in batches.

        Any HTTP error (raise_for_status) is logged and skipped.  Repeated
        URLs collapse to one download: a chapter often references the same
        decoration or watermark image several times.

        :param img_dir: Destination folder.
        :param urls: List of image URLs (http/https).
        :param on_exist: What to do when file exists.
        :param concurrent: Concurrency per batch.
        """
        unique_urls = _unique_non_empty(urls)
        if not unique_urls:
            return

        batch_size = max(1, concurrent)
        for i in range(0, len(unique_urls), batch_size):
            batch = unique_urls[i : i + batch_size]
            tasks = [
                self._fetch_one_image(url, img_dir, on_exist=on_exist) for url in batch
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    logger.warning("Image download error: %s", r)

    async def fetch_fonts(
        self,
        font_dir: Path,
        urls: list[str],
        *,
        on_exist: Literal["overwrite", "skip"] = "skip",
        concurrent: int = 5,
    ) -> None:
        """Download fonts to `font_dir` in batches."""
        unique_urls = _unique_non_empty(urls)
        if not unique_urls:
            return

        batch_size = max(1, concurrent)
        for i in range(0, len(unique_urls), batch_size):
            batch = unique_urls[i : i + batch_size]
            tasks = [
                self._fetch_one_font(url, font_dir, on_exist=on_exist) for url in batch
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    logger.warning("Font download error: %s", r)

    async def load_state(self) -> bool:
        """
        Load session cookies from a file to restore previous login state.

        :return: True if the session state was loaded, False otherwise.
        """
        self.session.load_cookies(self._cache_dir)

        self._is_logged_in = await self._check_login_status()
        return self._is_logged_in

    async def save_state(self) -> bool:
        """
        Save the current session cookies to a file for future reuse.

        :return: True if the session state was saved, False otherwise.
        """
        return self.session.save_cookies(self._cache_dir)

    @property
    def is_logged_in(self) -> bool:
        """
        Indicates whether the requester is currently authenticated.
        """
        return self._is_logged_in

    @property
    def login_fields(self) -> list[LoginField]:
        return [
            LoginField(
                name="cookies",
                label="Cookie",
                type="cookie",
                required=True,
                placeholder="Paste your login cookies here",
                description=t(
                    "Copy the cookies from your browser's developer tools "
                    "while logged in."
                ),
            ),
        ]

    async def fetch(
        self,
        url: str,
        encoding: str = "utf-8",
        **kwargs: Any,
    ) -> str:
        """
        Fetch the content from the given URL asynchronously, with retry support.

        :param url: The target URL to fetch.
        :param kwargs: Additional keyword arguments to pass to `session.get`.
        :return: The response body as text.
        """
        for attempt in range(self._retry_times + 1):
            if self._rate_limiter:
                await self._rate_limiter.wait()
            try:
                resp = await self.session.get(url, encoding=encoding, **kwargs)
            except TooManyRedirectsError as exc:
                # A redirect loop is deterministic: the next attempt walks the
                # same chain.  Raise the typed, non-retryable failure the
                # chapter loop understands instead of leaving a bare backend
                # exception for its generic ``except Exception`` retry.
                logger.warning(
                    "Too many redirects (site=%s) %s: %s",
                    self.site_name,
                    url,
                    exc,
                )
                raise RedirectLimitError(url, exc) from exc
            except NonRetryableConnectionError as exc:
                # Deterministic transport failure (untrusted certificate,
                # rejected proxy credentials).  Treated like a spent budget:
                # the typed TransportError tells the chapter loop not to
                # retry, so the same request is not sent three more times.
                logger.warning(
                    "Request failed (site=%s) %s: %s (not retrying)",
                    self.site_name,
                    url,
                    exc,
                )
                raise TransportError(url, exc) from exc
            except (TimeoutError, ConnectionError) as exc:
                # Transport trouble is exactly what the retry budget is for;
                # the previous version only retried HTTP statuses, so one
                # reset killed a book-info fetch outright.
                if attempt < self._retry_times:
                    logger.info(
                        "Retrying (site=%s, attempt=%d/%d) %s: %s",
                        self.site_name,
                        attempt + 1,
                        self._retry_times,
                        url,
                        exc,
                    )
                    await self._retry_backoff(attempt)
                    continue
                raise TransportError(url, exc) from exc

            if resp.ok:
                return resp.text

            if resp.status not in RETRYABLE_STATUS:
                # Deterministic failure (404/401/403...): another attempt
                # returns the same answer and only amplifies traffic.  A
                # ``Retry-After`` is still surfaced for reporting callers.
                raise HttpStatusError(
                    url,
                    resp.status,
                    retry_after=_retry_after_seconds(resp.headers),
                )

            if attempt >= self._retry_times:
                # This request gives up, but the header is still evidence for
                # the peers that are about to hit the same rate limit.
                delay = _retry_after_seconds(resp.headers)
                self._register_shared_cooldown(resp.status, resp.headers)
                raise HttpStatusError(url, resp.status, retry_after=delay)

            logger.info(
                "Retrying (site=%s, attempt=%d/%d) %s: HTTP %s",
                self.site_name,
                attempt + 1,
                self._retry_times,
                url,
                resp.status,
            )
            delay = _retry_after_seconds(resp.headers)
            if resp.status in _SHARED_COOLDOWN_STATUS and delay is not None:
                covered = self._apply_shared_cooldown(delay)
                if covered > 0 and covered >= delay:
                    # The limiter now holds every worker until the cooldown
                    # expires, and the next ``wait()`` blocks for exactly that
                    # long.  Sleeping here as well would wait twice for one
                    # 429 and silently multiply the requested pause.
                    continue
                if covered > 0:
                    delay -= covered
            await self._retry_backoff(attempt, delay)

        raise AssertionError("unreachable: the retry loop always returns or raises")

    def _register_shared_cooldown(
        self,
        status: int,
        headers: Mapping[str, str],
    ) -> float | None:
        """
        Register a shared cooldown from a rate-limit response, when applicable.

        Subclasses that override :meth:`fetch` do not pass through the base
        retry loop, so they call this to keep the same contract: a 429/503
        carrying a parseable ``Retry-After`` pauses every worker sharing the
        limiter, while other statuses and a limiter without ``penalize``
        change nothing.

        :param status: HTTP status of the response.
        :param headers: Response headers.
        :return: The parsed delay when a cooldown was registered; ``None``
            otherwise.
        """
        if status not in _SHARED_COOLDOWN_STATUS:
            return None
        delay = _retry_after_seconds(headers)
        if delay is None:
            return None
        if self._apply_shared_cooldown(delay) <= 0:
            return None
        return delay

    def _apply_shared_cooldown(self, seconds: float) -> float:
        """
        Pause every worker sharing the limiter, when the limiter supports it.

        ``penalize`` is optional (the limiter is duck-typed, and callers may
        install their own), so its absence is normal and must not change the
        retry path.

        :param seconds: Server-requested delay.
        :return: Seconds covered by the shared cooldown; ``0.0`` when the
            limiter has no such capability.
        """
        penalize = getattr(self._rate_limiter, "penalize", None)
        if penalize is None:
            return 0.0
        try:
            covered = float(penalize(seconds))
        except Exception:  # pragma: no cover - defensive: a foreign limiter
            logger.debug(
                "Rate limiter penalize() failed (site=%s)",
                self.site_name,
                exc_info=True,
            )
            return 0.0
        if not covered > 0:  # NaN-safe
            return 0.0
        return min(covered, seconds)

    async def _retry_backoff(self, attempt: int, delay: float | None = None) -> None:
        """
        Sleep before the next attempt.

        A server-supplied ``Retry-After`` wins over the local jitter schedule;
        otherwise the delay is an exponential backoff (``backoff_factor``,
        then doubling per attempt) with jitter, capped at
        :data:`_MAX_BACKOFF`.

        The retry counter used to be accepted but ignored, so every retry
        slept the same jittered ``backoff_factor`` however many failures had
        already happened.

        :param attempt: Zero-based index of the attempt that just failed.
        :param delay: Delay requested by the server, if any.
        """
        if delay is not None:
            # Wait at least as long as the server asked, plus upward-only
            # jitter: a fleet of workers that all received the same
            # ``Retry-After`` must not retry in lockstep and earn another 429.
            jitter = random.uniform(
                0.0,
                min(_RETRY_AFTER_JITTER_MAX, delay * _RETRY_AFTER_JITTER_FRACTION),
            )
            await asyncio.sleep(min(delay + jitter, _MAX_RETRY_AFTER))
            return
        base = min(
            self._backoff_factor * (2 ** min(attempt, 16)),
            _MAX_BACKOFF,
        )
        await async_jitter_sleep(
            base,
            mul_spread=1.1,
            max_sleep=min(base + 2.0, _MAX_BACKOFF),
        )

    async def _check_login_status(self) -> bool:
        """
        Check whether the user is currently logged in

        :return: True if the user is logged in, False otherwise.
        """
        return True

    async def _sleep(self) -> None:
        if self._request_interval > 0:
            await async_jitter_sleep(
                self._request_interval,
                mul_spread=1.1,
                max_sleep=self._request_interval + 2,
            )

    @property
    def headers(self) -> dict[str, str]:
        """
        Get a copy of the current session headers for temporary use.

        :return: A dict mapping header names to their values.
        """
        return self.session.headers

    async def _fetch_one_image(
        self,
        url: str,
        folder: Path,
        *,
        name: str | None = None,
        on_exist: Literal["overwrite", "skip"],
    ) -> Path | None:
        """Download a single image and save with a hashed filename."""
        # Exporters prepend ``https:`` to a protocol-relative resource URL
        # before hashing it; use the same form so the file they look for is
        # the one saved here.
        filename_url = url.strip()
        if filename_url.startswith("//"):
            filename_url = "https:" + filename_url
        try:
            filename = image_filename(filename_url, name=name)
        except ValueError as exc:
            logger.warning(
                "Invalid image URL (site=%s) %s: %s", self.site_name, url, exc
            )
            return None
        save_path = self._media_path(folder, filename)

        async with self._media_guard(save_path):
            if save_path.exists() and on_exist == "skip":
                if _non_empty_file(save_path):
                    logger.debug("Skip existing image: %s", save_path)
                    return save_path
                # A zero-byte leftover (interrupted write, pre-atomic version)
                # would otherwise stay broken forever under "skip".
                logger.warning("Empty cached image, re-downloading: %s", save_path)

            content = await self._fetch_media_bytes(
                url, headers=self.IMAGE_HEADERS, kind="image"
            )
            if content is None:
                return None

            if not await self._save_media(save_path, content, kind="image", url=url):
                return None

            logger.debug("Saved image: %s <- %s", save_path, url)
            return save_path

    async def _fetch_one_font(
        self,
        url: str,
        folder: Path,
        *,
        name: str | None = None,
        on_exist: Literal["overwrite", "skip"],
    ) -> Path | None:
        """Download a single font and save with a hashed filename."""
        try:
            filename = font_filename(url, name=name)
        except ValueError as exc:
            logger.warning(
                "Invalid font URL (site=%s) %s: %s", self.site_name, url, exc
            )
            return None
        save_path = self._media_path(folder, filename)

        async with self._media_guard(save_path):
            if save_path.exists() and on_exist == "skip":
                if _non_empty_file(save_path):
                    logger.debug("Skip existing font: %s", save_path)
                    return save_path
                logger.warning("Empty cached font, re-downloading: %s", save_path)

            content = await self._fetch_media_bytes(
                url, headers=DEFAULT_USER_HEADERS, kind="font"
            )
            if content is None:
                return None

            if not await self._save_media(save_path, content, kind="font", url=url):
                return None

            logger.debug("Saved font: %s <- %s", save_path, url)
            return save_path

    def _media_path(self, folder: Path, filename: str) -> Path:
        """
        Build a media destination path that cannot leave *folder*.

        ``image_filename``/``font_filename`` hash the URL by default, but an
        explicit ``name=`` is caller-controlled and may contain separators.
        Sanitising here (rather than only inside :func:`write_file`) keeps the
        existence check and the actual write on the same path.
        """
        return folder / sanitize_filename(filename)

    def _resolve_media_url(self, url: str) -> str:
        """
        Turn a resource URL into something the HTTP session can request.

        Protocol-relative (``//host/x``) and root-relative (``/uploads/x``)
        image URLs appear in several site parsers.  Both used to reach the
        session untouched, where the backend rejected them and the image was
        silently missing.  Relative URLs are resolved against the site origin;
        the filename stays derived from the original URL so exporters, which
        only know the stored value, find the file.
        """
        candidate = url.strip()
        if candidate.startswith("//"):
            return "https:" + candidate
        if candidate.startswith(("http://", "https://")):
            return candidate
        if self._base_url:
            try:
                return urljoin(self._base_url, candidate)
            except ValueError:
                # A malformed base/host must not fail before the session gets
                # a chance to classify the URL.
                return candidate
        return candidate

    @contextlib.asynccontextmanager
    async def _media_guard(self, save_path: Path) -> AsyncIterator[None]:
        """
        Serialise downloads that target the same file.

        Two chapter workers may reference the same illustration, and a single
        chapter may repeat it.  Without this guard both requests are in flight
        at once and the URL is downloaded twice; with it the second waiter
        wakes after the first write and honours ``on_exist="skip"``.
        """
        lock, refs = self._media_locks.get(save_path, (asyncio.Lock(), 0))
        self._media_locks[save_path] = (lock, refs + 1)
        try:
            async with lock:
                yield
        finally:
            lock, refs = self._media_locks[save_path]
            if refs <= 1:
                del self._media_locks[save_path]
            else:
                self._media_locks[save_path] = (lock, refs - 1)

    async def _fetch_media_bytes(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        kind: str,
    ) -> bytes | None:
        """
        Fetch a media body and refuse anything that is clearly not media.

        The check is intentionally conservative: unknown or generic content
        types are accepted, while an HTML login/error page (with 200 OK) or a
        body above :attr:`MAX_MEDIA_BYTES` is rejected before it can become a
        corrupt image on disk.
        """
        request_url = self._resolve_media_url(url)
        response = await self._fetch_data_response(request_url, headers=headers)
        if response is None:
            return None

        reason = _media_rejection_reason(
            response.content, response.headers, self.MAX_MEDIA_BYTES
        )
        if reason is not None:
            logger.warning(
                "Rejected %s (site=%s) %s: %s",
                kind,
                self.site_name,
                url,
                reason,
            )
            return None

        return response.content

    async def _save_media(
        self,
        save_path: Path,
        content: bytes,
        *,
        kind: str,
        url: str,
    ) -> bool:
        """
        Write *content* atomically, turning disk errors into a skipped item.

        A full disk or a read-only media directory must not abort the book:
        the chapter text is still valuable, and the failure is already logged
        per URL.  Cancellation is not caught (it is a ``BaseException``), so a
        stopped download still propagates.
        """
        try:
            await asyncio.to_thread(
                write_file,
                content=content,
                filepath=save_path,
                on_exist="overwrite",
            )
        except OSError as exc:
            logger.warning(
                "Failed to save %s (site=%s) %s -> %s: %s",
                kind,
                self.site_name,
                url,
                save_path,
                exc,
            )
            return False
        return True

    def _resolve_base_url(self, locale_style: str) -> str:
        key = locale_style.strip().lower()
        return self.BASE_URL_MAP.get(key, self.DEFAULT_BASE_URL)

    async def __aenter__(self) -> Self:
        await self.init()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        tb: types.TracebackType | None,
    ) -> None:
        await self.close()


class GenericFetcher(BaseFetcher):
    """
    Generic mid-layer for novel sites.

    Children override constants like:
      * site_name
      * BOOK_INFO_URL / CHAPTER_URL
      * BOOK_CATALOG_URL (optional)
      * BASE_URL (for sites that use relative page suffixes)
      * BASE_URL_MAP / DEFAULT_BASE_URL (when URL depends on variant)
      * BOOK_ID_REPLACEMENTS (e.g. [("-", "/")])

    For paginated info/chapters, set:
      * USE_PAGINATED_INFO = True and implement relative_info_url
      * USE_PAGINATED_CATALOG = True and implement relative_catalog_url
      * USE_PAGINATED_CHAPTER = True and implement relative_chapter_url

    For catalogs:
      * HAS_SEPARATE_CATALOG = True if site has a dedicated catalog page
    """

    BOOK_ID_REPLACEMENTS: list[tuple[str, str]] = []
    CHAP_ID_REPLACEMENTS: list[tuple[str, str]] = []

    # Simple template-style URLs (single page)
    BOOK_INFO_URL: str | None = None
    CHAPTER_URL: str | None = None

    # Optional extra info page ([info, catalog])
    HAS_SEPARATE_CATALOG: bool = False
    BOOK_CATALOG_URL: str | None = None

    # For sites that build full URLs with a BASE_URL + relative suffixes
    BASE_URL: str | None = None

    # Pagination toggles
    USE_PAGINATED_INFO: bool = False
    USE_PAGINATED_CATALOG: bool = False
    USE_PAGINATED_CHAPTER: bool = False

    async def fetch_book_info(self, book_id: str, **kwargs: Any) -> list[str]:
        book_id = self._transform_book_id(book_id)
        pages: list[str] = []

        # --- 1) Info ---
        if self.USE_PAGINATED_INFO:
            pages = await self._paginate(
                make_suffix=lambda idx: self.relative_info_url(book_id, idx),
                page_type="info",
                book_id=book_id,
                **kwargs,
            )
            if not pages:
                return []
        else:
            info_url = self.book_info_url(base_url=self._base_url, book_id=book_id)
            pages = [await self.fetch(info_url, **kwargs)]

        # --- 2) Catalog ---
        if self.HAS_SEPARATE_CATALOG:
            if self.USE_PAGINATED_CATALOG:
                catalog_pages = await self._paginate(
                    make_suffix=lambda idx: self.relative_catalog_url(book_id, idx),
                    page_type="catalog",
                    book_id=book_id,
                    **kwargs,
                )
                if not catalog_pages:
                    return []
                pages.extend(catalog_pages)

            elif self.BOOK_CATALOG_URL:
                catalog_url = self.book_catalog_url(
                    base_url=self._base_url, book_id=book_id
                )
                pages.append(await self.fetch(catalog_url, **kwargs))

        return pages

    async def fetch_chapter_content(
        self, book_id: str, chapter_id: str, **kwargs: Any
    ) -> list[str]:
        book_id = self._transform_book_id(book_id)
        chapter_id = self._transform_chap_id(chapter_id)

        if self.USE_PAGINATED_CHAPTER:
            return await self._paginate(
                make_suffix=lambda idx: self.relative_chapter_url(
                    book_id, chapter_id, idx
                ),
                page_type="chapter",
                book_id=book_id,
                chapter_id=chapter_id,
                **kwargs,
            )

        # Single chapter page
        url = self.chapter_url(
            base_url=self._base_url, book_id=book_id, chapter_id=chapter_id
        )
        return [await self.fetch(url, **kwargs)]

    @classmethod
    def relative_info_url(cls, book_id: str, idx: int) -> str:
        """
        Return the *relative* URL suffix (prefixed by "/") for the info page #idx.
        """
        raise NotImplementedError(f"{cls.__name__} must implement relative_info_url")

    @classmethod
    def relative_catalog_url(cls, book_id: str, idx: int) -> str:
        """
        Return the *relative* URL suffix (prefixed by "/") for the catalog page #idx.
        """
        raise NotImplementedError(f"{cls.__name__} must implement relative_catalog_url")

    @classmethod
    def relative_chapter_url(cls, book_id: str, chapter_id: str, idx: int) -> str:
        """
        Return the *relative* URL suffix (prefixed by "/") for the chapter page #idx.
        """
        raise NotImplementedError(f"{cls.__name__} must implement relative_chapter_url")

    def should_continue_pagination(
        self,
        current_html: str,
        next_suffix: str,
        next_idx: int,
        page_type: Literal["info", "catalog", "chapter"],
        book_id: str,
        chapter_id: str | None = None,
    ) -> bool:
        return next_suffix in current_html

    def _transform_book_id(self, book_id: str) -> str:
        for old, new in self.BOOK_ID_REPLACEMENTS:
            book_id = book_id.replace(old, new)
        return book_id

    def _transform_chap_id(self, chap_id: str) -> str:
        for old, new in self.CHAP_ID_REPLACEMENTS:
            chap_id = chap_id.replace(old, new)
        return chap_id

    async def _paginate(
        self,
        *,
        make_suffix: Callable[[int], str],
        page_type: Literal["info", "catalog", "chapter"],
        book_id: str,
        chapter_id: str | None = None,
        **fetch_kwargs: Any,
    ) -> list[str]:
        """
        Generic pagination loop for info/catalog/chapter.

        Starts at idx=1 and continues while should_continue_pagination(...) is True.
        """
        if not self.BASE_URL:
            raise RuntimeError(
                f"{self.site_name}: BASE_URL is required for {page_type}"
            )
        origin = self.BASE_URL.rstrip("/")

        pages: list[str] = []
        seen: set[bytes] = set()
        idx = 1
        suffix = make_suffix(idx)

        while True:
            if len(pages) >= self.MAX_PAGES:
                # A site that keeps answering "there is a next page" (login
                # wall, error page echoing the suffix) used to loop forever
                # while growing this list.
                logger.warning(
                    "%s: %s pagination stopped at MAX_PAGES=%d",
                    self.site_name,
                    page_type,
                    self.MAX_PAGES,
                )
                break

            html = await self.fetch(origin + suffix, **fetch_kwargs)
            fingerprint = hashlib.blake2b(
                html.encode("utf-8", "replace"), digest_size=8
            ).digest()
            if fingerprint in seen:
                logger.warning(
                    "%s: %s pagination repeated page %d; stopping",
                    self.site_name,
                    page_type,
                    idx,
                )
                break
            seen.add(fingerprint)
            pages.append(html)
            idx += 1
            suffix = make_suffix(idx)
            if not self.should_continue_pagination(
                current_html=html,
                next_suffix=suffix,
                next_idx=idx,
                page_type=page_type,
                book_id=book_id,
                chapter_id=chapter_id,
            ):
                break
            await self._sleep()

        return pages

    @classmethod
    def book_info_url(cls, **kwargs: Any) -> str:
        if not cls.BOOK_INFO_URL:
            raise NotImplementedError(f"{cls.__name__}.BOOK_INFO_URL not set")
        return cls.BOOK_INFO_URL.format(**kwargs)

    @classmethod
    def book_catalog_url(cls, **kwargs: Any) -> str:
        if not cls.BOOK_CATALOG_URL:
            raise NotImplementedError(f"{cls.__name__}.BOOK_CATALOG_URL not set")
        return cls.BOOK_CATALOG_URL.format(**kwargs)

    @classmethod
    def chapter_url(cls, **kwargs: Any) -> str:
        if not cls.CHAPTER_URL:
            raise NotImplementedError(f"{cls.__name__}.CHAPTER_URL not set")
        return cls.CHAPTER_URL.format(**kwargs)
