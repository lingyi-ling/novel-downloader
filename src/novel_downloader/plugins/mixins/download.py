#!/usr/bin/env python3
"""
novel_downloader.plugins.mixins.download
----------------------------------------
"""

import asyncio
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, Protocol, final

from novel_downloader.infra.persistence.chapter_storage import ChapterStorage
from novel_downloader.infra.persistence.download_ledger import (
    DownloadLedger,
    LedgerEntry,
    utc_now_iso,
)
from novel_downloader.libs.time_utils import async_jitter_sleep
from novel_downloader.schemas import (
    BookConfig,
    BookInfoDict,
    ChapterDict,
    VolumeInfoDict,
)
from novel_downloader.schemas.book import ChapterFailureDict, DownloadSummary

__all__ = ["STOP", "DownloadMixin", "IncompleteDownloadError", "StopToken"]

ONE_DAY = 86400  # seconds
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _ChapterFailure:
    """Diagnostic detail for one failed ``get_chapter`` call."""

    reason: str
    attempts: int


# Task-local (each ``asyncio.Task`` owns a copy of the context), so concurrent
# producers never see each other's diagnostics.  ``get_chapter`` fills it in;
# ``download_book`` reads it right after the call.
_LAST_FAILURE: ContextVar[_ChapterFailure | None] = ContextVar(
    "novel_downloader_last_chapter_failure", default=None
)


class IncompleteDownloadError(RuntimeError):
    """
    Raised by :meth:`DownloadMixin.download_book` when ``strict=True`` and at
    least one planned chapter could not be downloaded.

    The carried :attr:`summary` lets the caller report the exact failures (and
    decide whether exporting is still acceptable).
    """

    __slots__ = ("summary",)

    def __init__(self, summary: DownloadSummary) -> None:
        super().__init__(
            f"Incomplete download for book {summary['book_id']} on "
            f"'{summary['site']}': {summary['failed']} of "
            f"{summary['total']} chapter(s) failed"
        )
        self.summary = summary


if TYPE_CHECKING:
    from novel_downloader.plugins.protocols import (
        DownloadUI,
        _ClientContext,
    )

    class DownloadClientContext(_ClientContext, Protocol):
        """"""

        async def get_book_info(
            self,
            book_id: str,
            **kwargs: Any,
        ) -> BookInfoDict: ...

        async def get_chapter(
            self,
            book_id: str,
            chapter_id: str,
            **kwargs: Any,
        ) -> ChapterDict | None: ...

        async def _dl_cache_info_images(
            self, book_id: str, book_info: BookInfoDict
        ) -> None: ...

        async def _dl_fix_chapter_ids(
            self,
            book_id: str,
            book_info: BookInfoDict,
            storage: ChapterStorage,
        ) -> BookInfoDict: ...

        def _dl_check_restricted(self, raw_pages: list[str]) -> bool: ...

        def _dl_check_empty(self, raw_pages: list[str]) -> bool: ...

        def _dl_check_refetch(self, chap: ChapterDict) -> bool: ...

        @staticmethod
        def _dl_chapter_meta(
            vols: list[VolumeInfoDict],
        ) -> dict[str, tuple[str, str]]: ...

        @staticmethod
        def _dl_chapter_is_empty(chap: ChapterDict) -> bool: ...


@final
class StopToken:
    """Typed sentinel used to end queues."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "STOP"


STOP: Final[StopToken] = StopToken()


class DownloadMixin:
    """"""

    async def download_book(
        self: "DownloadClientContext",
        book: BookConfig,
        *,
        ui: "DownloadUI | None" = None,
        strict: bool = False,
        **kwargs: Any,
    ) -> DownloadSummary:
        """
        Download all chapters and metadata for a single book.

        A chapter that cannot be fetched (retries exhausted, access limited,
        empty content, storage rejected it) is written to the book's failure
        ledger (``chapter.failures.jsonl``) and counted in the returned summary
        instead of being skipped silently.

        The ledger is *per run*: :meth:`DownloadLedger.begin_run` wipes the
        previous run's records before the first chapter is attempted, so after
        this call the file lists exactly the chapters this run could not fetch.
        A run without failures leaves no ledger file at all -- reading a stale
        file after a successful re-run must not report phantom missing
        chapters.

        Progress still advances for failed chapters so the progress bar does
        not stall at ``< total``; the returned summary -- not the percentage --
        is therefore the authoritative completeness signal.  A run in which
        every chapter failed still ends at ``(total, total)``: the bar reports
        "all chapters processed", ``failed == total`` reports "none usable".

        :param book: :class:`BookConfig` with at least ``book_id`` defined.
        :param ui: Optional :class:`DownloadUI` for progress reporting.
        :param strict: When ``True``, raise :class:`IncompleteDownloadError`
            if any planned chapter failed.  When ``False`` (the default) the
            failures are only reported in the returned summary.
        :return: Structured summary of the run.
        :raises IncompleteDownloadError: if ``strict`` is set and a chapter
            failed.
        """
        book_id = book.book_id
        start_id = book.start_id
        end_id = book.end_id
        ignore_set = book.ignore_ids

        raw_base = self._raw_data_dir / book_id
        raw_base.mkdir(parents=True, exist_ok=True)
        ledger = DownloadLedger(raw_base)
        # The ledger describes this run, never the previous one.  Wiped before
        # any work so a crash mid-run cannot leave yesterday's failures looking
        # like today's result.
        try:
            ledger.begin_run()
        except OSError as exc:
            logger.warning(
                "Failed to reset download ledger (site=%s, book=%s): %s",
                self._site,
                book_id,
                exc,
            )

        if ui:
            await ui.on_start(book)

        # ---- metadata ---
        book_info = await self.get_book_info(book_id=book_id)
        with ChapterStorage(raw_base, filename="chapter.raw.sqlite") as storage:
            book_info = await self._dl_fix_chapter_ids(
                book_id,
                book_info,
                storage,
            )

        await self._dl_cache_info_images(book_id, book_info)

        vols = book_info["volumes"]
        plan = self._extract_chapter_ids(vols, start_id, end_id, ignore_set)
        if not plan:
            logger.info(
                "Nothing to do after filtering (site=%s, book=%s)", self._site, book_id
            )
            return DownloadSummary(
                book_id=book_id,
                site=self._site,
                total=0,
                succeeded=0,
                failed=0,
                skipped=0,
                failures=[],
                ledger_path=str(ledger.path),
            )

        total = len(plan)
        chapter_meta = self._dl_chapter_meta(vols)
        done = 0
        skipped = 0
        failures: list[ChapterFailureDict] = []

        async def bump(n: int = 1) -> None:
            nonlocal done
            done += n
            if ui:
                await ui.on_progress(done, total)

        def record_failure(chapter_id: str, reason: str, attempts: int) -> None:
            """Record a failed chapter in the summary and in the ledger."""
            title, url = chapter_meta.get(chapter_id, ("", ""))
            failed_at = utc_now_iso()
            failures.append(
                ChapterFailureDict(
                    chapter_id=chapter_id,
                    chapter_title=title,
                    url=url,
                    reason=reason,
                    attempts=attempts,
                    failed_at=failed_at,
                )
            )
            # A ledger problem must never break (or mask) a download.
            try:
                ledger.append(
                    LedgerEntry(
                        book_id=book_id,
                        chapter_id=chapter_id,
                        chapter_title=title,
                        url=url,
                        reason=reason,
                        attempts=attempts,
                        failed_at=failed_at,
                        site=self._site,
                    )
                )
            except Exception as exc:
                logger.warning(
                    "Failed to append to download ledger (site=%s, book=%s, "
                    "chapter=%s): %s",
                    self._site,
                    book_id,
                    chapter_id,
                    exc,
                )

        # ---- queues & batching ---
        save_q: asyncio.Queue[ChapterDict | StopToken] = asyncio.Queue(maxsize=10)
        batches: dict[bool, list[ChapterDict]] = {False: [], True: []}
        sem = asyncio.Semaphore(self.workers)

        def _batch(need_refetch: bool) -> list[ChapterDict]:
            return batches[need_refetch]

        async def flush_batch(need_refetch: bool) -> None:
            batch = _batch(need_refetch)
            if not batch:
                return
            try:
                # need_refetch=True for encrypted, False for plain
                storage.upsert_chapters(batch, need_refetch=need_refetch)
            except Exception as e:
                logger.error(
                    "Storage batch upsert failed (site=%s, book=%s, size=%d, need_refetch=%s): %s",  # noqa: E501
                    self._site,
                    book_id,
                    len(batch),
                    need_refetch,
                    e,
                )
                # The chapters were fetched but are not on disk: they must not
                # be reported as succeeded.
                for chap in batch:
                    record_failure(chap["id"], f"storage_error: {e}", 1)
            finally:
                size = len(batch)
                batch.clear()
                # Progress covers processed chapters, written or not.
                await bump(size)

        async def flush_all() -> None:
            await flush_batch(False)
            await flush_batch(True)

        # ---- workers ---
        async def storage_worker() -> None:
            while True:
                item = await save_q.get()
                if isinstance(item, StopToken):
                    break

                need = self._dl_check_refetch(item)
                bucket = _batch(need)
                bucket.append(item)
                if len(bucket) >= self._storage_batch_size:
                    await flush_batch(need)
            await flush_all()

        async def producer(cid: str) -> None:
            nonlocal skipped
            async with sem:
                if self._cache_chapter and not storage.need_refetch(cid):
                    skipped += 1
                    await bump(1)
                    return

                # Reset the task-local diagnostic first: site overrides of
                # ``get_chapter`` (e.g. qidian) do not fill it in.
                _LAST_FAILURE.set(None)
                chap = await self.get_chapter(book_id, cid)
                detail = _LAST_FAILURE.get()

                if chap is None:
                    record_failure(
                        cid,
                        detail.reason if detail else "no_content",
                        detail.attempts if detail else self._retry_times + 1,
                    )
                    await bump(1)
                elif self._dl_chapter_is_empty(chap):
                    record_failure(
                        cid,
                        "empty_content",
                        detail.attempts if detail else 1,
                    )
                    await bump(1)
                else:
                    await save_q.put(chap)

                await async_jitter_sleep(
                    base=self._request_interval,
                    mul_spread=1.1,
                    max_sleep=self._request_interval + 2,
                )

        # ---- run tasks ---
        with ChapterStorage(raw_base, filename="chapter.raw.sqlite") as storage:
            storage_task = asyncio.create_task(storage_worker())

            try:
                tasks = [asyncio.create_task(producer(cid)) for cid in plan]
                await asyncio.gather(*tasks)

                # signal storage to finish and wait for flush
                await save_q.put(STOP)
                await storage_task
            except asyncio.CancelledError:
                logger.info("Download cancelled, stopping storage worker...")
                await save_q.put(STOP)

                try:
                    await asyncio.wait_for(storage_task, timeout=10)
                except TimeoutError:
                    logger.warning("Storage worker did not exit, cancelling.")
                    storage_task.cancel()
                    await asyncio.gather(storage_task, return_exceptions=True)

                raise
            finally:
                if not storage_task.done():
                    storage_task.cancel()
                    await asyncio.gather(storage_task, return_exceptions=True)

        # ---- done ---
        failed = len(failures)
        summary = DownloadSummary(
            book_id=book_id,
            site=self._site,
            total=total,
            succeeded=total - failed,
            failed=failed,
            skipped=skipped,
            failures=failures,
            ledger_path=str(ledger.path),
        )

        if ui:
            await ui.on_complete(book, failed=failed)

        if failed:
            logger.warning(
                "Download incomplete (site=%s, book=%s): %d of %d chapter(s) "
                "failed; see %s",
                self._site,
                book_id,
                failed,
                total,
                ledger.path,
            )
        else:
            logger.info(
                "Download completed for site=%s book=%s",
                self._site,
                book_id,
            )

        if strict and failed:
            raise IncompleteDownloadError(summary)

        return summary

    async def cache_media(
        self: "DownloadClientContext",
        book: BookConfig,
        *,
        force_update: bool = False,
        concurrent: int = 10,
        **kwargs: Any,
    ) -> None:
        """
        Asynchronously pre-cache all images associated with a book.

        :param book: The BookConfig instance representing the book.
        :param force_update: If True, re-download even if images are already cached.
        :param concurrent: Maximum number of concurrent download tasks.
        """
        book_id = book.book_id
        start_id = book.start_id
        end_id = book.end_id
        ignore_set = book.ignore_ids

        raw_base = self._raw_data_dir / book_id
        raw_base.mkdir(parents=True, exist_ok=True)
        media_dir = raw_base / "media"

        # ---- metadata ---
        book_info = self._load_book_info(book_id=book_id)
        await self._dl_cache_info_images(book_id, book_info)

        vols = book_info["volumes"]
        plan = self._extract_chapter_ids(vols, start_id, end_id, ignore_set)
        if not plan:
            logger.info(
                "Nothing to do after filtering (site=%s, book=%s)", self._site, book_id
            )
            return

        with ChapterStorage(raw_base, filename="chapter.raw.sqlite") as storage:
            chapters = storage.get_chapters(plan)
            for chap in chapters.values():
                if chap is None:
                    continue

                resources = chap["extra"].get("resources")
                if resources:
                    await self.fetcher.fetch_media(
                        media_dir,
                        resources,
                        on_exist="overwrite" if force_update else "skip",
                        concurrent=concurrent,
                    )

    async def get_book_info(
        self: "DownloadClientContext",
        book_id: str,
        **kwargs: Any,
    ) -> BookInfoDict:
        """
        Attempt to fetch and parse the book_info for a given book_id.

        :param book_id: identifier of the book
        :return: parsed BookInfoDict
        """
        book_info: BookInfoDict | None = None
        if self._cache_book_info:
            try:
                book_info = self._load_book_info(book_id)
                age = time.time() - book_info.get("last_checked", 0.0)

                if age < ONE_DAY:
                    logger.debug(
                        "Using cached metadata for %s (age=%.1f sec)", book_id, age
                    )
                    return book_info

                logger.debug(
                    "Cached metadata for %s expired (age=%.1f sec), refetching",
                    book_id,
                    age,
                )

            except FileNotFoundError:
                logger.debug("No cached metadata for %s", book_id)
            except Exception as exc:
                logger.info("Failed to load cached metadata for %s: %s", book_id, exc)

        else:
            logger.debug("Skipping cached metadata for book %s", book_id)

        try:
            info_html = await self.fetcher.fetch_book_info(book_id)
            self._save_raw_pages(book_id, "info", info_html)

            book_info = self.parser.parse_book_info(info_html)
            if book_info:
                book_info["last_checked"] = time.time()
                self._save_book_info(book_id, book_info)
                return book_info

        except Exception as exc:
            logger.warning("Failed to fetch/parse book_info for %s: %s", book_id, exc)

        raise LookupError(f"Unable to load book_info for {book_id}")

    async def get_chapter(
        self: "DownloadClientContext",
        book_id: str,
        chapter_id: str,
        **kwargs: Any,
    ) -> "ChapterDict | None":
        """
        Fetch, parse, and return a single chapter, retrying on transient errors.

        Terminal failures (``None``) also publish their reason and attempt count
        to the task-local :data:`_LAST_FAILURE`, which
        :meth:`download_book` reads to fill the failure ledger.

        :param book_id: Book identifier.
        :param chapter_id: Chapter identifier.
        :return: Parsed :class:`ChapterDict`, or ``None`` if failed.
        """
        _LAST_FAILURE.set(None)
        for attempt in range(self._retry_times + 1):
            try:
                raw_pages = await self.fetcher.fetch_chapter_content(
                    book_id, chapter_id
                )
                self._save_raw_pages(book_id, chapter_id, raw_pages)

                if self._dl_check_restricted(raw_pages):
                    logger.warning(
                        "Access limited (site=%s, book=%s, chapter=%s)",
                        self._site,
                        book_id,
                        chapter_id,
                    )
                    _LAST_FAILURE.set(_ChapterFailure("access_restricted", attempt + 1))
                    return None

                chap = await asyncio.to_thread(
                    self.parser.parse_chapter_content, raw_pages, chapter_id
                )
                if not chap:
                    if self._dl_check_empty(raw_pages):
                        logger.warning(
                            "Empty parse result (site=%s, book=%s, chapter=%s)",
                            self._site,
                            book_id,
                            chapter_id,
                        )
                        _LAST_FAILURE.set(_ChapterFailure("empty_parse", attempt + 1))
                        return None
                    raise ValueError("Empty parse result")

                resources = chap["extra"].get("resources")
                if resources:
                    media_dir = self._raw_data_dir / book_id / "media"
                    await self.fetcher.fetch_media(media_dir, resources)

                return chap
            except Exception as e:
                if attempt < self._retry_times:
                    logger.info(
                        "Retrying (site=%s, book=%s, chapter=%s, attempt=%d): %s",
                        self._site,
                        book_id,
                        chapter_id,
                        attempt + 1,
                        e,
                    )
                    backoff = self._backoff_factor * (2**attempt)
                    await async_jitter_sleep(
                        base=backoff, mul_spread=1.2, max_sleep=backoff + 3
                    )
                else:
                    logger.warning(
                        "Failed chapter (site=%s, book=%s, chapter=%s): %s",
                        self._site,
                        book_id,
                        chapter_id,
                        e,
                    )
                    _LAST_FAILURE.set(
                        _ChapterFailure(
                            f"retry_exhausted: {type(e).__name__}: {e}",
                            attempt + 1,
                        )
                    )
        return None

    async def _dl_fix_chapter_ids(
        self: "DownloadClientContext",
        book_id: str,
        book_info: BookInfoDict,
        storage: ChapterStorage,
    ) -> BookInfoDict:
        """
        Repair missing ``chapterId`` fields in the given book's metadata.

        This method attempts to infer missing chapter IDs by loading the
        previous chapter (from local storage or by refetching) and reading
        its ``extra.next_cid`` field.

        Refetched chapters are cached via the provided storage instance.
        """
        prev_cid: str = ""
        for vol in book_info["volumes"]:
            for chap in vol["chapters"]:
                cid = chap.get("chapterId")
                if cid:
                    prev_cid = cid
                    continue

                if not prev_cid:
                    continue

                # missing id: try storage
                data = storage.get_chapter(prev_cid)
                if not data:
                    # fetch+parse previous to discover next
                    data = await self.get_chapter(book_id, prev_cid)
                    if not data:
                        logger.warning(
                            "Failed to fetch chapter (site=%s, book=%s, prev=%s) during repair",  # noqa: E501
                            self._site,
                            book_id,
                            prev_cid,
                        )
                        continue
                    storage.upsert_chapter(data)
                    await async_jitter_sleep(
                        self._request_interval,
                        mul_spread=1.1,
                        max_sleep=self._request_interval + 2,
                    )

                next_cid = data.get("extra", {}).get("next_cid")
                if not next_cid:
                    logger.warning(
                        "No next_cid (site=%s, book=%s, prev=%s)",
                        self._site,
                        book_id,
                        prev_cid,
                    )
                    continue

                logger.info(
                    "Repaired chapterId (site=%s, book=%s): %s <- %s",
                    self._site,
                    book_id,
                    next_cid,
                    prev_cid,
                )
                chap["chapterId"] = next_cid
                prev_cid = next_cid

        self._save_book_info(book_id, book_info)
        return book_info

    async def _dl_cache_info_images(
        self: "DownloadClientContext",
        book_id: str,
        book_info: BookInfoDict,
    ) -> None:
        """
        Download cover and volume images for a book.

        :param book_id: Unique ID of the book.
        :param book_info: Metadata dictionary containing cover URLs.
        """
        img_dir = self._raw_data_dir / book_id / "media"
        img_dir.mkdir(parents=True, exist_ok=True)

        # --- cover image ---
        if cover_url := book_info.get("cover_url"):
            await self.fetcher.fetch_image(cover_url, img_dir, name="cover")

        # --- volume covers ---
        vol_covers = [
            cover
            for v in book_info.get("volumes", [])
            if (cover := v.get("volume_cover"))
        ]
        if vol_covers:
            await self.fetcher.fetch_images(img_dir, vol_covers)

    @staticmethod
    def _dl_chapter_meta(vols: list[VolumeInfoDict]) -> dict[str, tuple[str, str]]:
        """
        Map ``chapter_id -> (title, url)`` for failure reporting.

        The first occurrence of an id wins, matching
        :meth:`_extract_chapter_ids`, which also emits a chapter at its first
        appearance.

        :param vols: Volumes from the book metadata.
        """
        meta: dict[str, tuple[str, str]] = {}
        for vol in vols:
            for chap in vol["chapters"]:
                cid = chap.get("chapterId")
                if cid and cid not in meta:
                    meta[cid] = (chap.get("title", ""), chap.get("url", ""))
        return meta

    @staticmethod
    def _dl_chapter_is_empty(chap: ChapterDict) -> bool:
        """
        Return True if a downloaded chapter carries neither text nor media.

        An empty text body alone is not a failure: image-only chapters
        legitimately have no text but do reference ``extra["resources"]``.

        :param chap: Parsed chapter.
        """
        if chap["content"].strip():
            return False
        return not chap["extra"].get("resources")

    def _dl_check_restricted(self, raw_pages: list[str]) -> bool:
        """
        Return True if page content indicates access restriction
        (e.g. login required, paywall, VIP, subscription, etc.)

        :param raw_pages: List of raw HTML strings.
        """
        return False

    def _dl_check_empty(self, raw_pages: list[str]) -> bool:
        """
        Return True if parse_chapter returns empty but should be skipped.
        """
        return False

    def _dl_check_refetch(self, chap: ChapterDict) -> bool:
        """Override this hook to decide if a chapter needs refetch."""
        return False
