#!/usr/bin/env python3
"""
novel_downloader.libs.html_builder.core
---------------------------------------
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import tempfile
import uuid
from pathlib import Path

from novel_downloader.infra.paths import (
    HTML_CSS_CHAPTER_PATH,
    HTML_CSS_INDEX_PATH,
    HTML_JS_MAIN_PATH,
)
from novel_downloader.libs.crypto.hash_utils import hash_bytes, hash_file
from novel_downloader.libs.filesystem import sanitize_filename
from novel_downloader.libs.media.font import detect_font_format
from novel_downloader.libs.media.image import detect_image_format

from .constants import (
    CHAPTER_DIR,
    CSS_DIR,
    FONT_DIR,
    IMAGE_MEDIA_EXTS,
    JS_DIR,
    MEDIA_DIR,
)
from .models import HtmlChapter, HtmlFont, HtmlImage, HtmlVolume, IndexDocument

logger = logging.getLogger(__name__)


def _discard_temp(path: str | Path) -> None:
    """Remove a staging file, warning instead of hiding a failed cleanup."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("Could not remove temporary file %s: %s", path, exc)


def _discard_tree(path: Path) -> None:
    """Remove a staging/backup tree, warning instead of hiding a failure."""
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("Could not remove temporary directory %s: %s", path, exc)


def _atomic_write(path: Path, data: str | bytes) -> None:
    """
    Write ``data`` to ``path`` through a sibling temporary file.

    The temporary file is flushed and fsynced before ``os.replace`` moves it
    into place, so an interrupted export never leaves a half-written chapter
    or stylesheet behind.

    There is deliberately no "write directly instead" fallback: opening the
    destination for writing truncates it, so failing halfway through the
    fallback would destroy the previous export as well. Failures are raised
    (with the destination path in the message) and the old file is preserved.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    binary = isinstance(data, bytes)
    mode = "wb" if binary else "w"
    encoding = None if binary else "utf-8"

    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
    except OSError as exc:
        raise OSError(
            f"Cannot create a temporary file next to {path} ({exc}); "
            "refusing to write directly because that would truncate an "
            "existing export"
        ) from exc

    try:
        handle = os.fdopen(
            fd,
            mode,
            encoding=encoding,
            newline=None if binary else "\n",
        )
    except BaseException:
        # ``fdopen`` may fail before it owns the descriptor; close it here so
        # the temporary file can really be removed (Windows refuses to unlink
        # a file that still has an open handle).
        with contextlib.suppress(OSError):
            os.close(fd)
        _discard_temp(tmp_name)
        raise

    try:
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        _discard_temp(tmp_name)
        raise

    try:
        os.replace(tmp_name, path)
    except OSError as exc:
        _discard_temp(tmp_name)
        raise OSError(
            f"Atomic replace failed for {path} ({exc}); the previous export "
            "was left untouched"
        ) from exc


class HtmlBuilder:
    def __init__(
        self,
        title: str,
        author: str = "",
        description: str = "",
        cover: bytes | None = None,
        subject: list[str] | None = None,
        serial_status: str = "",
        word_count: str = "0",
        language: str = "zh-Hans",
    ) -> None:
        # metadata
        self.title = title
        self.lang = language
        self.cover = cover
        self._cover_filename: str | None = None
        if cover:
            fmt = detect_image_format(cover)
            ext = fmt.lower() if fmt else "jpg"
            self._cover_filename = f"cover.{ext}"

        self._index = IndexDocument(
            title=title,
            author=author,
            description=description,
            subject=subject or [],
            serial_status=serial_status,
            word_count=word_count,
            lang=language,
            cover_filename=self._cover_filename,
        )

        # image state
        self.images: list[HtmlImage] = []
        self._img_map: dict[str, str] = {}
        self._img_idx = 0

        # font state
        self.fonts: list[HtmlFont] = []
        self._font_map: dict[str, HtmlFont] = {}
        self._font_idx = 0

        self._vol_idx = 0

        self._chapters: list[HtmlChapter] = []  # flattened reading order

    def add_image(self, image_path: Path) -> str:
        """
        Add an image resource (deduped by hash) and register it.
        """
        if not (image_path.exists() and image_path.is_file()):
            return ""
        h = hash_file(image_path)
        if h in self._img_map:
            return self._img_map[h]

        data = image_path.read_bytes()
        # Try detecting from magic bytes
        fmt = detect_image_format(data)
        ext = fmt.lower() if fmt else image_path.suffix.lower().lstrip(".") or "bin"

        filename = f"img_{self._img_idx}.{ext}"

        img = HtmlImage(data=data, filename=filename)
        self.images.append(img)
        self._img_map[h] = filename
        self._img_idx += 1
        return filename

    def add_image_bytes(self, data: bytes, mime_type: str = "") -> str:
        """
        Add an image resource from bytes (deduped by hash) and register it.
        """
        if not data:
            return ""

        h = hash_bytes(data)
        if h in self._img_map:
            return self._img_map[h]

        fmt = detect_image_format(data)
        ext = fmt.lower() if fmt else IMAGE_MEDIA_EXTS.get(mime_type, "bin")

        filename = f"img_{self._img_idx}.{ext}"

        img = HtmlImage(data=data, filename=filename)
        self.images.append(img)
        self._img_map[h] = filename
        self._img_idx += 1
        return filename

    def add_font(
        self,
        font_path: Path,
        *,
        family: str | None = None,
        selectors: tuple[str, ...] = (),
    ) -> HtmlFont | None:
        """
        Add a font from a file (deduped by hash) and return a HtmlFont.

        :param font_path: Path to the font file.
        :param family: CSS font-family name to use.
        :param selectors: CSS selectors this font should apply to.
        :return: HtmlFont instance, or None if the path is invalid.
        """
        if not (font_path.exists() and font_path.is_file()):
            return None

        h = hash_file(font_path)
        if h in self._font_map:
            return self._font_map[h]

        data = font_path.read_bytes()
        fmt = detect_font_format(data)
        ext = fmt.lower() if fmt else font_path.suffix.lower().lstrip(".") or "bin"

        family_name = family or f"FontFamily_{self._font_idx}"
        filename = f"font_{self._font_idx}.{ext}"

        font = HtmlFont(
            data=data,
            filename=filename,
            family=family_name,
            selectors=selectors,
        )
        self.fonts.append(font)
        self._font_map[h] = font
        self._font_idx += 1
        return font

    def add_font_bytes(
        self,
        data: bytes,
        *,
        family: str | None = None,
        selectors: tuple[str, ...] = (),
    ) -> HtmlFont | None:
        """
        Add a font from raw bytes (deduped by hash) and return a HtmlFont.

        :param data: Raw font bytes.
        :param family: CSS font-family name to use.
        :param selectors: CSS selectors this font should apply to.
        :return: HtmlFont instance, or None if data is empty.
        """
        if not data:
            return None

        h = hash_bytes(data)
        if h in self._font_map:
            return self._font_map[h]

        fmt = detect_font_format(data)
        ext = fmt.lower() if fmt else "bin"

        family_name = family or f"FontFamily_{self._font_idx}"
        filename = f"font_{self._font_idx}.{ext}"

        font = HtmlFont(
            data=data,
            filename=filename,
            family=family_name,
            selectors=selectors,
        )
        self.fonts.append(font)
        self._font_map[h] = font
        self._font_idx += 1
        return font

    def add_chapter(self, chap: HtmlChapter) -> None:
        """
        Register a single chapter in the global reading order.
        """
        self._chapters.append(chap)
        self._index.add_chapter(chap)

    @property
    def chapter_count(self) -> int:
        """Number of chapters registered for export, in reading order."""
        return len(self._chapters)

    def add_volume(self, volume: HtmlVolume) -> None:
        """Add a volume and all its chapters to the HTML."""
        self._vol_idx += 1
        self._index.add_volume(volume)

        for chap in volume.chapters:
            self._chapters.append(chap)

    def export(
        self,
        output_path: str | Path,
        *,
        folder: str | None = None,
    ) -> Path:
        """
        Build and export the current book as an HTML folder.

        :param output_path: Path to the parent directory where folder will be created
        :param folder: Optional folder name. If None, use the book title
        """
        return self._build_html(
            output_path=Path(output_path),
            folder=folder,
        )

    def _prepare_output_dir(self, html_dir: Path) -> None:
        css_dir = html_dir / CSS_DIR
        css_dir.mkdir(parents=True, exist_ok=True)
        index_css = HTML_CSS_INDEX_PATH.read_bytes()
        _atomic_write(css_dir / "index.css", index_css)
        chapter_css = HTML_CSS_CHAPTER_PATH.read_bytes()
        _atomic_write(css_dir / "chapter.css", chapter_css)
        js_dir = html_dir / JS_DIR
        js_dir.mkdir(parents=True, exist_ok=True)
        main_js = HTML_JS_MAIN_PATH.read_bytes()
        _atomic_write(js_dir / "main.js", main_js)

    def _write_media(self, html_dir: Path) -> None:
        """
        Write all referenced images and the cover image (if any) into /media.
        """
        media_dir = html_dir / MEDIA_DIR
        # Write chapter images
        if self.images:
            media_dir.mkdir(parents=True, exist_ok=True)
            for img in self.images:
                _atomic_write(media_dir / img.filename, img.data)

        # Write cover image as media/cover.xxx
        if self.cover and self._cover_filename:
            media_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write(media_dir / self._cover_filename, self.cover)

    def _write_fonts(self, html_dir: Path) -> None:
        """
        Write all referenced fonts into /fonts.
        """
        if not self.fonts:
            return

        font_dir = html_dir / FONT_DIR
        font_dir.mkdir(parents=True, exist_ok=True)

        for font in self.fonts:
            _atomic_write(font_dir / font.filename, font.data)

    def _build_index(self, html_dir: Path) -> None:
        index_path = html_dir / "index.html"

        _atomic_write(index_path, self._index.to_html())

    def _build_chapters(self, html_dir: Path) -> None:
        chap_dir = html_dir / CHAPTER_DIR
        chap_dir.mkdir(parents=True, exist_ok=True)

        if not self._chapters:
            return

        n = len(self._chapters)
        for idx, chap in enumerate(self._chapters):
            prev_link = self._chapters[idx - 1].filename if idx > 0 else ""
            next_link = self._chapters[idx + 1].filename if idx < n - 1 else ""

            chap_html = chap.to_html(
                lang=self.lang,
                prev_link=prev_link,
                next_link=next_link,
            )
            _atomic_write(chap_dir / chap.filename, chap_html)

    def _build_html(self, output_path: Path, folder: str | None) -> Path:
        """
        Assemble the folder in a staging directory and swap it into place.

        Writing straight into ``output_path/folder`` would leave files from a
        previous export (chapters removed in the new data, old media) behind:
        the index would no longer reference them, but they would still be on
        disk. Building in a sibling staging directory keeps the previous
        export intact until the new one is complete, and lets the swap remove
        every stale file. If the swap fails, the previous export is restored.
        """
        folder_name = (
            sanitize_filename(folder) if folder else sanitize_filename(self.title)
        )
        output_path.mkdir(parents=True, exist_ok=True)
        final_dir = output_path / folder_name

        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{folder_name}.",
                suffix=".tmp",
                dir=str(output_path),
            )
        )
        completed = False
        try:
            self._prepare_output_dir(staging)
            self._write_media(staging)
            self._write_fonts(staging)
            self._build_index(staging)
            self._build_chapters(staging)
            _replace_directory(staging, final_dir)
            completed = True
        finally:
            if not completed:
                _discard_tree(staging)
        return final_dir


def _replace_directory(new_dir: Path, final_dir: Path) -> None:
    """
    Replace ``final_dir`` with ``new_dir``, keeping the old tree until the
    swap has succeeded.

    ``os.replace`` cannot overwrite a non-empty directory on Windows, so the
    existing export is first renamed aside. If moving the new tree into place
    fails, the old one is renamed back; nothing is deleted before the new
    export is fully in place.
    """
    backup: Path | None = None
    if final_dir.exists() or final_dir.is_symlink():
        backup = final_dir.with_name(f".{final_dir.name}.{uuid.uuid4().hex[:8]}.old")
        os.replace(final_dir, backup)

    try:
        os.replace(new_dir, final_dir)
    except BaseException:
        if backup is not None:
            try:
                os.replace(backup, final_dir)
            except OSError:
                logger.warning(
                    "Could not restore the previous HTML export at %s; it is "
                    "kept at %s",
                    final_dir,
                    backup,
                )
        raise

    if backup is not None:
        _discard_tree(backup)
