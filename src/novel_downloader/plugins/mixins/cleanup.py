#!/usr/bin/env python3
"""
novel_downloader.plugins.mixins.cleanup
---------------------------------------

Cleanup operations deliberately restrict themselves to one book directory and
never follow a link out of the raw-data root.  A book ID (and the ``stage``
suffix used in file names) comes from the CLI/API, so it is validated as a
single path component before it is joined onto the raw-data directory; values
such as ``..`` or ``C:\\Windows`` would otherwise turn a "delete this book"
request into a delete somewhere else.
"""

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from novel_downloader.infra.persistence.chapter_storage import ChapterStorage
from novel_downloader.libs.filesystem import (
    KIND_ROWS,
    RemovalItem,
    is_link_like,
    plan_removal,
    remove_path_safely,
)
from novel_downloader.schemas import BookConfig

logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from novel_downloader.plugins.protocols import _ClientContext


def _is_component(value: str) -> bool:
    """Return whether ``value`` is usable as one file/directory name."""
    text = str(value)
    path = Path(text)
    return not (
        # Windows strips trailing spaces/dots, so ".. " and "..." are parent
        # references there even though ``Path`` keeps them as literal names.
        not text.strip(" .")
        or text != text.rstrip(" .")
        or "/" in text
        or "\\" in text
        or ":" in text
        or path.is_absolute()
        or path.name != text
        or any(ord(char) < 32 for char in text)
    )


def _require_component(value: str, *, what: str) -> str:
    """Return ``value`` when it is one path component, else raise ``ValueError``."""
    text = str(value)
    if not _is_component(text):
        raise ValueError(f"Unsafe {what}: {text!r}")
    return text


def _safe_book_dir(
    raw_data_dir: Path, book_id: str, *, allow_link: bool = False
) -> Path | None:
    """
    Resolve the directory for ``book_id`` without leaving the raw-data root.

    A book directory that is itself a symlink/junction is refused for
    selective cleanup (reading or writing through it would operate on data
    outside the configured root).  ``remove_all`` passes ``allow_link`` to
    remove the link itself instead.

    :param raw_data_dir: The site's raw-data directory.
    :param book_id: Book identifier, validated as one path component.
    :param allow_link: Return a linked book directory for link-only removal.
    :return: The book directory, or ``None`` when a link must not be followed.
    :raise ValueError: When ``book_id`` is not a single path component.
    """
    safe_id = _require_component(book_id, what="book ID")
    book_dir = Path(raw_data_dir) / safe_id
    if not allow_link and is_link_like(book_dir):
        logger.warning(
            "Refusing to follow link at %s for book %s; skipping cleanup.",
            book_dir,
            safe_id,
        )
        return None
    return book_dir


def preview_book_cleanup(
    raw_data_dir: Path,
    book: BookConfig,
    *,
    remove_metadata: bool = True,
    remove_chapters: bool = True,
    remove_media: bool = False,
    remove_all: bool = False,
    stage: str = "raw",
) -> list[RemovalItem]:
    """
    Enumerate what :meth:`CleanupMixin.cleanup_book` would remove.

    Nothing is deleted or written: the book directory, metadata file, media
    directory and chapter database are inspected with the same component and
    link checks the real cleanup uses.  A linked book directory is skipped for
    selective cleanup (the real code refuses to follow it) and reported as a
    link for ``remove_all`` (only the link itself is removed).  The chapter
    database is reported with kind ``"rows"`` because the rows are deleted but
    the file stays.

    :param raw_data_dir: The site's raw-data directory.
    :param book: Book to inspect.
    :param remove_metadata: Include ``book_info.<stage>.json`` when present.
    :param remove_chapters: Include the chapter database when present.
    :param remove_media: Include the ``media`` directory when present.
    :param remove_all: Report the whole book directory instead of parts.
    :param stage: Data stage used in file names.
    :return: Planned removals in the same order the real cleanup uses.
    :raise ValueError: When the book ID or stage is not one path component.
    """
    if remove_all:
        book_dir = _safe_book_dir(raw_data_dir, book.book_id, allow_link=True)
        if book_dir is None:  # pragma: no cover - allow_link never returns None
            return []
        item = plan_removal(book_dir, root=Path(raw_data_dir))
        return [item] if item is not None else []

    book_dir = _safe_book_dir(raw_data_dir, book.book_id)
    if book_dir is None:
        return []
    _require_component(stage, what="stage")

    items: list[RemovalItem] = []

    if remove_chapters:
        db_path = book_dir / f"chapter.{stage}.sqlite"
        # A linked DB is skipped by the real cleanup: opening it would delete
        # rows through the link, possibly outside the raw-data root.
        if os.path.lexists(db_path) and not is_link_like(db_path):
            items.append(RemovalItem(path=db_path, kind=KIND_ROWS))

    if remove_metadata:
        metadata_path = book_dir / f"book_info.{stage}.json"
        if os.path.lexists(metadata_path):
            item = plan_removal(metadata_path, root=book_dir)
            if item is not None:
                items.append(item)

    if remove_media:
        media_dir = book_dir / "media"
        if os.path.lexists(media_dir):
            item = plan_removal(media_dir, root=book_dir)
            if item is not None:
                items.append(item)

    return items


class CleanupMixin:
    """"""

    def cleanup_book(
        self: "_ClientContext",
        book: BookConfig,
        *,
        remove_metadata: bool = True,
        remove_chapters: bool = True,
        remove_media: bool = False,
        remove_all: bool = False,
        stage: str = "raw",
        **kwargs: Any,
    ) -> None:
        """
        Cleanup an entire book, or selectively remove chapter data + metadata.

        :param remove_all: If True, remove the entire book folder.
        :param stage: Which stage/version of raw data to remove.
        """
        if remove_all:
            # ``allow_link`` makes a book directory that is itself a junction
            # remove the junction only; its target is never walked into.
            book_dir = _safe_book_dir(self._raw_data_dir, book.book_id, allow_link=True)
            if book_dir is None:  # pragma: no cover - allow_link never returns None
                return
            if os.path.lexists(book_dir):
                logger.info(
                    "Removing all raw data for book %s at %s", book.book_id, book_dir
                )
                remove_path_safely(book_dir, root=Path(self._raw_data_dir))
            else:
                logger.info(
                    "Raw data directory for book %s does not exist, nothing to remove.",
                    book.book_id,
                )
            return

        # Selective cleanup
        if remove_chapters:
            self.cleanup_chapters(
                book.book_id,
                start_id=book.start_id,
                end_id=book.end_id,
                ignore_ids=book.ignore_ids,
                stage=stage,
            )

        if remove_metadata:
            self.cleanup_metadata(book.book_id, stage=stage)

        if remove_media:
            self.cleanup_media(book.book_id)

    def cleanup_metadata(
        self: "_ClientContext",
        book_id: str,
        *,
        stage: str = "raw",
        **kwargs: Any,
    ) -> None:
        """
        Delete metadata JSON for a book.
        """
        book_dir = _safe_book_dir(self._raw_data_dir, book_id)
        if book_dir is None:
            return
        _require_component(stage, what="stage")
        metadata_path = book_dir / f"book_info.{stage}.json"

        if os.path.lexists(metadata_path):
            try:
                metadata_path.unlink()
                logger.info("Removed metadata: %s", metadata_path)
            except Exception as e:
                logger.warning("Failed to remove metadata %s: %s", metadata_path, e)
        else:
            logger.debug(
                "Metadata file not found for book %s (stage=%s)", book_id, stage
            )

    def cleanup_chapters(
        self: "_ClientContext",
        book_id: str,
        start_id: str | None = None,
        end_id: str | None = None,
        ignore_ids: frozenset[str] = frozenset(),
        *,
        stage: str = "raw",
        **kwargs: Any,
    ) -> None:
        """
        Delete populated chapter entries (in SQLite) for a given range.
        """
        raw_base = _safe_book_dir(self._raw_data_dir, book_id)
        if raw_base is None:
            return
        _require_component(stage, what="stage")

        # Load chapter listing from metadata
        book_info = self._load_book_info(book_id, stage=stage)
        vols = book_info["volumes"]
        cids = self._extract_chapter_ids(vols, start_id, end_id, ignore_ids)

        db_path = raw_base / f"chapter.{stage}.sqlite"
        if is_link_like(db_path):
            # Opening it would delete rows through the link, i.e. in whatever
            # database it points at -- possibly outside the raw-data root.
            logger.warning(
                "Refusing to follow linked chapter DB for book %s: %s",
                book_id,
                db_path,
            )
            return
        if not db_path.exists():
            logger.debug("No chapter DB for book %s (stage=%s)", book_id, stage)
            return

        # Delete rows from SQLite
        with ChapterStorage(raw_base, filename=f"chapter.{stage}.sqlite") as storage:
            deleted = storage.delete_chapters(cids)
            if deleted > 0:
                storage.vacuum()
            logger.info("Deleted %d chapters (requested %d)", deleted, len(cids))

    def cleanup_media(
        self: "_ClientContext",
        book_id: str,
        **kwargs: Any,
    ) -> None:
        """
        Delete images/media folder for this book.
        """
        book_dir = _safe_book_dir(self._raw_data_dir, book_id)
        if book_dir is None:
            return
        media_dir = book_dir / "media"

        if os.path.lexists(media_dir):
            logger.info("Removing media directory: %s", media_dir)
            # A linked "media" entry is removed as a link; a real directory is
            # walked without descending into nested links.
            remove_path_safely(media_dir, root=book_dir)
        else:
            logger.debug("No media directory for book %s", book_id)
