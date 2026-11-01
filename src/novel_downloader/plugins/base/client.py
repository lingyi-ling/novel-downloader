#!/usr/bin/env python3
"""
novel_downloader.plugins.base.client
------------------------------------

Abstract base class providing common
"""

import abc
import contextlib
import json
import logging
import types
from pathlib import Path
from typing import Any, Self, cast

from novel_downloader.libs.filesystem import image_filename, write_file
from novel_downloader.plugins.protocols import FetcherProtocol, ParserProtocol
from novel_downloader.plugins.protocols.ui import (
    DownloadUI,
    ExportUI,
    LoginUI,
    ProcessUI,
)
from novel_downloader.plugins.registry import registrar
from novel_downloader.schemas import (
    BookConfig,
    BookInfoDict,
    ChapterInfoDict,
    ClientConfig,
    ExporterConfig,
    FetcherConfig,
    ParserConfig,
    PipelineMeta,
    ProcessorConfig,
    VolumeInfoDict,
)
from novel_downloader.schemas.book import DownloadSummary

logger = logging.getLogger(__name__)


class AbstractClient(abc.ABC):
    """
    Abstract base class for site client.
    """

    def __init__(
        self,
        site: str,
        config: ClientConfig | None = None,
    ) -> None:
        """
        Initialize the downloader for a specific site.

        :param site: Identifier for the target website or source.
        :param config: Downloader configuration settings.
        """
        self._site = site
        cfg = config or ClientConfig()

        self._save_html = cfg.save_html
        self._cache_book_info = cfg.cache_book_info
        self._cache_chapter = cfg.cache_chapter
        self._fetch_inaccessible = cfg.fetch_inaccessible
        self._request_interval = cfg.request_interval
        self._retry_times = cfg.retry_times
        self._backoff_factor = cfg.backoff_factor
        self._workers = max(1, cfg.workers)
        self._storage_batch_size = max(1, cfg.storage_batch_size)

        self._fetcher_cfg = cfg.fetcher_cfg
        self._parser_cfg = cfg.parser_cfg

        self._fetcher: FetcherProtocol | None = None
        self._parser: ParserProtocol | None = None

        self._raw_data_dir = Path(cfg.raw_data_dir) / site
        self._cache_dir = Path(cfg.cache_dir) / site
        self._output_dir = Path(cfg.output_dir)
        self._debug_dir = Path.cwd() / "debug" / site

    async def init(self, fetcher_cfg: FetcherConfig, parser_cfg: ParserConfig) -> None:
        if self._fetcher or self._parser:
            await self.close()

        fetcher = registrar.get_fetcher(self._site, fetcher_cfg)
        try:
            parser = registrar.get_parser(self._site, parser_cfg)
        except Exception:
            # A fetcher owns a network session. If the parser cannot be built,
            # release that session instead of leaking it on a failed init().
            with contextlib.suppress(Exception):
                await fetcher.close()
            raise

        try:
            await fetcher.init()
        except Exception:
            with contextlib.suppress(Exception):
                await fetcher.close()
            raise

        # Publish the pair only once both are usable.
        self._fetcher = fetcher
        self._parser = parser

    async def close(self) -> None:
        # Detach first so a failed save/close cannot leave a half-closed client
        # behind or cause a double close on a later attempt.
        fetcher, self._fetcher = self._fetcher, None
        self._parser = None
        if fetcher is None:
            return

        try:
            if fetcher.is_logged_in:
                await fetcher.save_state()
        finally:
            await fetcher.close()

    @abc.abstractmethod
    async def login(
        self,
        *,
        ui: LoginUI,
        login_cfg: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> bool:
        """
        Attempt to log in asynchronously.

        :returns: True if login succeeded.
        """
        ...

    async def download(
        self,
        book: BookConfig,
        *,
        ui: DownloadUI | None = None,
        **kwargs: Any,
    ) -> None:
        """
        Download a single book.

        :param book: BookConfig with at least 'book_id'.
        :param ui: Optional DownloadUI to report progress or messages.
        """
        await self.download_book(book, ui=ui, **kwargs)

    @abc.abstractmethod
    async def download_book(
        self,
        book: BookConfig,
        *,
        ui: DownloadUI | None = None,
        **kwargs: Any,
    ) -> DownloadSummary:
        """
        Download a single book.

        :param book: BookConfig with at least ``book_id``.
        :param ui: Optional DownloadUI to report progress or messages.
        :returns: Structured summary of the run (chapter counts + failures).
        """
        ...

    @abc.abstractmethod
    def process_book(
        self,
        book: BookConfig,
        processors: list[ProcessorConfig],
        *,
        ui: ProcessUI | None = None,
        **kwargs: Any,
    ) -> None:
        """
        Run all processors for a single book.

        :param book: BookConfig to process.
        :param ui: Optional ProcessUI to report progress.
        """
        ...

    @abc.abstractmethod
    async def cache_media(
        self,
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
        ...

    def export(
        self,
        book: BookConfig,
        cfg: ExporterConfig | None = None,
        *,
        formats: list[str] | None = None,
        stage: str | None = None,
        ui: ExportUI | None = None,
        **kwargs: Any,
    ) -> dict[str, list[Path]]:
        """
        Persist the assembled book to disk.

        :param book: The book configuration to export.
        :param cfg: Optional ExporterConfig defining export parameters.
        :param formats: Optional list of format strings (e.g., ['epub', 'txt']).
        :param ui: Optional ExportUI for reporting export progress.
        :return: A mapping from format name to the resulting file path.
        """
        return self.export_book(
            book=book,
            cfg=cfg,
            formats=formats,
            stage=stage,
            ui=ui,
            **kwargs,
        )

    @abc.abstractmethod
    def export_book(
        self,
        book: BookConfig,
        cfg: ExporterConfig | None = None,
        *,
        formats: list[str] | None = None,
        stage: str | None = None,
        ui: ExportUI | None = None,
        **kwargs: Any,
    ) -> dict[str, list[Path]]:
        """
        Persist the assembled book to disk in one or more formats.

        :param book: The :class:`BookConfig` instance to export.
        :param cfg: Optional :class:`ExporterConfig` defining export parameters.
        :param formats: Optional list of format identifiers (e.g. ``['epub', 'txt']``).
        :param stage: Optional export stage name, used for multi-phase exports.
        :param ui: Optional :class:`ExportUI` for reporting progress.
        :return: Mapping from format name to generated file paths.
        """
        ...

    @abc.abstractmethod
    def export_chapter(
        self,
        book_id: str,
        chapter_id: str,
        cfg: ExporterConfig | None = None,
        *,
        formats: list[str] | None = None,
        stage: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Path | None]:
        """
        Persist a single chapter to disk in one or more formats.

        :param book_id: Identifier of the book to download.
        :param chapter_id: Identifier of the chapter to download.
        :param cfg: Optional :class:`ExporterConfig` defining export parameters.
        :param formats: Optional list of format identifiers (e.g. ``['epub', 'txt']``).
        :param stage: Optional export stage name.
        :return: Mapping from format name to generated file paths.
        """
        ...

    @property
    def fetcher(self) -> FetcherProtocol:
        """
        Return the active fetcher.

        :raises RuntimeError: If the fetcher is uninitialized.
        """
        if self._fetcher is None:
            raise RuntimeError("Fetcher is not initialized.")
        return self._fetcher

    @property
    def parser(self) -> ParserProtocol:
        """
        Return the active parser.

        :raises RuntimeError: If the parser is uninitialized.
        """
        if self._parser is None:
            raise RuntimeError("Parser is not initialized.")
        return self._parser

    @property
    def workers(self) -> int:
        return self._workers

    async def __aenter__(self) -> Self:
        await self.init(self._fetcher_cfg, self._parser_cfg)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        tb: types.TracebackType | None,
    ) -> None:
        await self.close()


class BaseClient(AbstractClient, abc.ABC):
    """
    Abstract intermediate client that provides reusable helper methods.
    """

    def _book_dir(self, book_id: str) -> Path:
        return self._raw_data_dir / book_id

    def _save_book_info(
        self, book_id: str, book_info: BookInfoDict, stage: str = "raw"
    ) -> None:
        """
        Serialize and save the book_info dict as json.

        :param book_id: identifier of the book
        :param book_info: dict containing metadata about the book
        """
        base = self._book_dir(book_id)
        payload = json.dumps(book_info, ensure_ascii=False, indent=2)
        # ``write_file`` writes to a temporary file and atomically replaces the
        # target, so a failed write never leaves a truncated metadata file.
        write_file(content=payload, filepath=base / f"book_info.{stage}.json")

    def _load_book_info(self, book_id: str, stage: str = "raw") -> BookInfoDict:
        """
        Load and return the book_info payload for the given book.

        :param book_id: Book identifier.
        :raises FileNotFoundError: if the metadata file does not exist.
        :raises ValueError: if the file is not valid UTF-8, the JSON is invalid,
            or it has an unexpected structure.
        :return: Parsed BookInfoDict.
        """
        base = self._book_dir(book_id)
        info_path = base / f"book_info.{stage}.json"

        if not info_path.is_file():
            raise FileNotFoundError(f"Missing metadata file: {info_path}")

        try:
            raw_text = info_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            raise ValueError(f"Cannot decode {info_path} as UTF-8: {e}") from e

        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as e:
            raise ValueError(f"Corrupt JSON in {info_path}: {e}") from e

        if not isinstance(data, dict):
            raise ValueError(
                f"Invalid JSON structure in {info_path}: expected an object at the top"
            )

        return cast(BookInfoDict, data)

    def _load_pipeline_meta(self, book_id: str) -> PipelineMeta:
        """
        Load and return the pipeline metadata for the given book.

        The method attempts to read and parse ``pipeline.json`` stored
        under the book's directory. If the file is missing, unreadable,
        or contains unexpected structures, a default empty metadata
        payload is returned.

        :param book_id: Book identifier.
        :return: A ``PipelineMeta`` dict with ``pipeline`` and ``executed`` keys.
        """
        base = self._book_dir(book_id)
        meta_path = base / "pipeline.json"

        if not meta_path.is_file():
            return {"pipeline": [], "executed": {}}

        try:
            raw = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            return {"pipeline": [], "executed": {}}

        pipeline = raw.get("pipeline", [])
        if not isinstance(pipeline, list):
            pipeline = []

        executed = raw.get("executed", {})
        if not isinstance(executed, dict):
            executed = {}

        return cast(
            PipelineMeta,
            {
                "pipeline": pipeline,
                "executed": executed,
            },
        )

    def _save_pipeline_meta(self, book_id: str, meta: PipelineMeta) -> None:
        """
        Serialize and write ``pipeline.json`` for the given book.

        :param book_id: Book identifier.
        :param meta: Pipeline metadata containing ``pipeline`` and ``executed`` records.
        """
        base = self._book_dir(book_id)
        payload = json.dumps(meta, ensure_ascii=False, indent=2)
        write_file(content=payload, filepath=base / "pipeline.json")

    def _save_raw_pages(
        self,
        book_id: str,
        filename: str,
        raw_pages: list[str],
        *,
        folder: str = "html",
    ) -> None:
        """
        If save_html is enabled, write each HTML snippet to a file.

        Filenames will be {book_id}_{filename}_{index}.html in html_dir.

        :param book_id: The book identifier
        :param filename: used as filename prefix
        :param raw_pages: list of HTML strings to save
        """
        if not self._save_html:
            return
        html_dir = self._debug_dir / folder
        html_dir.mkdir(parents=True, exist_ok=True)
        for i, html in enumerate(raw_pages):
            (html_dir / f"{book_id}_{filename}_{i}.html").write_text(
                html, encoding="utf-8"
            )

    @staticmethod
    def _resolve_image_path(
        img_dir: Path | None,
        url: str | None,
        *,
        name: str | None = None,
    ) -> Path | None:
        """
        Resolve the local path of an image if it exists.

        :param img_dir: The directory where images are stored.
        :param url: The source URL of the image.
        :param name: Optional explicit base name.
        :return: Path to the existing image, or None if not found or invalid.
        """
        if not img_dir or not url:
            return None

        path = img_dir / image_filename(url, name=name)
        return path if path.is_file() else None

    def _extract_chapter_ids(
        self,
        vols: list[VolumeInfoDict],
        start_id: str | None,
        end_id: str | None,
        ignore: frozenset[str],
    ) -> list[str]:
        seen_start = start_id is None
        out: list[str] = []
        # Some tables of contents list the same chapter twice (a volume header
        # repeats it, or a site renders both a "latest" and a full listing).
        # Keeping the duplicate would download it twice, inflate ``total`` and
        # report one gap as two, so every id is emitted at most once.
        seen: set[str] = set()
        for vol in vols:
            for chap in vol["chapters"]:
                cid = chap.get("chapterId")
                if not cid:
                    continue
                if not seen_start:
                    if cid == start_id:
                        seen_start = True
                    else:
                        continue
                if cid in seen:
                    continue
                if cid not in ignore:
                    accessible = chap.get("accessible", True)
                    if accessible or self._fetch_inaccessible:
                        seen.add(cid)
                        out.append(cid)
                if end_id is not None and cid == end_id:
                    return out
        return out

    @staticmethod
    def _filter_volumes(
        vols: list[VolumeInfoDict],
        start_id: str | None,
        end_id: str | None,
        ignore: frozenset[str],
    ) -> list[VolumeInfoDict]:
        """
        Rebuild volumes to include only chapters within
        the [start_id, end_id] range (inclusive),
        while excluding any chapter IDs in `ignore`.

        :param vols: List of volume dicts.
        :param start_id: Range start chapter ID (inclusive) or None to start.
        :param end_id: Range end chapter ID (inclusive) or None to go till the end.
        :param ignore: Set of chapter IDs to exclude (regardless of range).
        :return: New list of volumes with chapters filtered accordingly.
        """
        if start_id is None and end_id is None and not ignore:
            return vols

        started = start_id is None
        finished = False
        result: list[VolumeInfoDict] = []

        for vol in vols:
            if finished:
                break

            kept: list[ChapterInfoDict] = []

            for ch in vol.get("chapters", []):
                cid = ch.get("chapterId")
                if not cid:
                    continue

                # wait until hit the start_id
                if not started:
                    if cid == start_id:
                        started = True
                    else:
                        continue

                if cid not in ignore:
                    kept.append(ch)

                # check for end_id after keeping
                if end_id is not None and cid == end_id:
                    finished = True
                    break

            if kept:
                result.append(
                    {
                        **vol,
                        "chapters": kept,
                    }
                )

        return result

    def _detect_latest_stage(self, book_id: str) -> str:
        """
        Return the chosen stage name for export (e.g., 'raw', 'cleaner', 'corrector').

        Strategy:
          * If pipeline.json exists, walk pipeline in reverse and pick the last stage
            whose recorded sqlite file exists.
          * Fallback: any executed record with an existing sqlite file.
          * Else: 'raw'.
        """
        base = self._book_dir(book_id)
        meta = self._load_pipeline_meta(book_id)

        for stg in reversed(meta["pipeline"]):
            db_file = base / f"chapter.{stg}.sqlite"
            info_file = base / f"book_info.{stg}.json"
            if db_file.is_file() and info_file.is_file():
                return stg

        return "raw"
