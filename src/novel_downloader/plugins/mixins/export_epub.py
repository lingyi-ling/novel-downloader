#!/usr/bin/env python3
"""
novel_downloader.plugins.mixins.export_epub
-------------------------------------------
"""

import base64
import hashlib
import logging
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from novel_downloader.infra.persistence.chapter_storage import ChapterStorage
from novel_downloader.libs.epub_builder import EpubBuilder, EpubChapter, EpubVolume
from novel_downloader.libs.filesystem import (
    font_filename,
    format_filename,
    image_filename,
    sanitize_filename,
)
from novel_downloader.schemas import (
    BookConfig,
    ChapterDict,
    ExporterConfig,
    MediaResource,
    VolumeInfoDict,
)

logger = logging.getLogger(__name__)

# Windows still caps most paths at 260 characters (unless long-path support is
# enabled). ``_build_epub`` writes into a ``.{name}.XXXXXXXX.tmp`` sibling, so
# leave a margin for that suffix.
_MAX_PATH_LEN = 240

# Chapters are stored as ``Text/<name>.xhtml`` inside the archive; keep the
# component short so extracting the EPUB on Windows cannot exceed the limit,
# whatever directory the reading app unpacks to.
_MAX_CHAPTER_NAME_LEN = 72


def _fit_name(name: str, parent: Path, limit: int = _MAX_PATH_LEN) -> str:
    """
    Shorten ``name`` so that ``parent / name`` fits within ``limit`` chars.

    Only the name shrinks; the extension is preserved and a short digest of
    the original name is appended so two long titles that share a prefix do
    not collapse onto the same output file. The digest is taken from the raw
    (pre-sanitization) name, because ``sanitize_filename`` truncates on its
    own and would otherwise erase the part that distinguishes the two.
    """
    total = len(str(parent)) + 1 + len(name)
    if total <= limit:
        return name

    budget = max(len(name) - (total - limit), 8)

    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""

    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]
    tail = f"~{digest}" + (f".{ext}" if ext else "")
    keep = max(budget - len(tail), 1)
    return f"{stem[:keep]}{tail}"


def _count_unique_cids(vols: list[VolumeInfoDict]) -> int:
    """Number of distinct chapter ids across the (already filtered) volumes."""
    cids = {
        cid
        for vol in vols
        for chap in vol.get("chapters", [])
        if (cid := chap.get("chapterId"))
    }
    return len(cids)


def _check_export_consistency(
    *,
    fmt: str,
    site: str,
    book_id: str,
    planned: int,
    rendered: int,
    skipped: int,
) -> None:
    """
    Warn when the archive does not account for every planned chapter.

    ``rendered + skipped`` must equal the number of distinct chapters the
    filtered book metadata promised. A mismatch means the builder dropped a
    chapter that was handed to it; the archive is still usable, so this is
    logged rather than raised (``CommonClient.export_book`` would otherwise
    discard the whole export and leave the user with nothing).
    """
    if planned == rendered + skipped:
        return

    logger.warning(
        "Export consistency mismatch (%s, site=%s, book=%s): planned=%d, "
        "rendered=%d, skipped=%d",
        fmt,
        site,
        book_id,
        planned,
        rendered,
        skipped,
    )


if TYPE_CHECKING:
    from novel_downloader.plugins.protocols import _ClientContext

    class ExportEpubClientContext(_ClientContext, Protocol):
        """"""

        _IMAGE_WRAPPER: str

        def _xp_epub_chapter(
            self,
            *,
            book: EpubBuilder,
            cid: str,
            chap_title: str | None,
            chap: ChapterDict,
            media_dir: Path,
            include_picture: bool = True,
        ) -> EpubChapter: ...

        def _xp_epub_missing_chapter(
            self,
            *,
            cid: str,
            chap_title: str | None,
        ) -> EpubChapter: ...

        def _xp_epub_extras(self, extras: dict[str, Any]) -> str: ...

        def _unique_output_path(self, path: Path, used: set[Path]) -> Path: ...

        def _xp_epub_chap_post(
            self, html_parts: list[str], chap: ChapterDict
        ) -> list[str]: ...


class ExportEpubMixin:
    """"""

    _IMAGE_WRAPPER = '<div class="duokan-image-single illus">{img}</div>'

    def _export_volume_epub(
        self: "ExportEpubClientContext",
        book: BookConfig,
        cfg: ExporterConfig,
        *,
        stage: str | None = None,
        **kwargs: Any,
    ) -> list[Path]:
        """
        Export each volume of a novel as a separate EPUB file.
        """
        book_id = book.book_id
        start_id = book.start_id
        end_id = book.end_id
        ignore_set = book.ignore_ids

        # --- Load book data ---
        raw_base = self._raw_data_dir / book_id
        if not raw_base.is_dir():
            return []

        media_dir = raw_base / "media"

        stage = stage or self._detect_latest_stage(book_id)
        book_info = self._load_book_info(book_id, stage=stage)

        # --- Filter volumes & chapters ---
        orig_vols = book_info.get("volumes", [])
        vols = self._filter_volumes(orig_vols, start_id, end_id, ignore_set)
        if not vols:
            logger.info(
                "Nothing to do after filtering (site=%s, book=%s)", self._site, book_id
            )
            return []

        # --- Prepare header (book metadata) ---
        name = book_info["book_name"]
        author = book_info.get("author") or ""
        book_summary = book_info.get("summary", "")

        # --- Generate intro + cover ---
        cover_path = self._resolve_image_path(media_dir, book_info.get("cover_url"))

        # --- Compile columes ---
        outputs: list[Path] = []
        used_paths: set[Path] = set()
        with ChapterStorage(raw_base, filename=f"chapter.{stage}.sqlite") as storage:
            for v_idx, vol in enumerate(vols, start=1):
                vol_title = vol.get("volume_name") or f"卷 {v_idx}"

                vol_cover = self._resolve_image_path(media_dir, vol.get("volume_cover"))
                vol_cover = vol_cover or cover_path

                builder = EpubBuilder(
                    title=f"{name} - {vol_title}",
                    author=author,
                    description=vol.get("volume_intro") or book_summary,
                    cover_path=vol_cover,
                    subject=book_info.get("tags", []),
                    serial_status=book_info.get("serial_status", ""),
                    word_count=vol.get("word_count", ""),
                    uid=f"{self._site}_{book_id}_v{v_idx}",
                )

                # Collect chapter ids then batch fetch
                cids = [
                    c["chapterId"]
                    for c in vol.get("chapters", [])
                    if c.get("chapterId")
                ]
                if not cids:
                    continue
                chap_map = storage.get_chapters(cids)

                # Append each chapter
                seen_cids: set[str] = set()
                skipped = 0
                for ch_info in vol.get("chapters", []):
                    cid = ch_info.get("chapterId")
                    ch_title = ch_info.get("title")
                    if not cid or cid in seen_cids:
                        continue
                    seen_cids.add(cid)

                    ch = chap_map.get(cid)
                    if not ch:
                        if cfg.render_missing_chapter:
                            chapter_obj = self._xp_epub_missing_chapter(
                                cid=cid,
                                chap_title=ch_title,
                            )
                            builder.add_chapter(chapter_obj)
                        else:
                            skipped += 1
                        continue

                    chapter_obj = self._xp_epub_chapter(
                        book=builder,
                        cid=cid,
                        chap_title=ch_title,
                        chap=ch,
                        media_dir=media_dir,
                        include_picture=cfg.include_picture,
                    )
                    builder.add_chapter(chapter_obj)

                _check_export_consistency(
                    fmt="EPUB",
                    site=self._site,
                    book_id=book_id,
                    planned=_count_unique_cids([vol]),
                    rendered=sum(
                        1 for item in builder.items if isinstance(item, EpubChapter)
                    ),
                    skipped=skipped,
                )

                raw_name = format_filename(
                    cfg.filename_template,
                    title=vol_title,
                    author=author,
                    append_timestamp=cfg.append_timestamp,
                    ext="epub",
                )
                out_path = self._output_dir / sanitize_filename(
                    _fit_name(raw_name, self._output_dir)
                )
                out_path = self._unique_output_path(out_path, used_paths)

                try:
                    outputs.append(builder.export(out_path))
                    logger.info(
                        "Exported EPUB (site=%s, book=%s): %s",
                        self._site,
                        book_id,
                        out_path,
                    )
                except Exception as e:
                    logger.error(
                        "Failed to write EPUB (site=%s, book=%s) to %s: %s",
                        self._site,
                        book_id,
                        out_path,
                        e,
                    )
        return outputs

    def _export_book_epub(
        self: "ExportEpubClientContext",
        book: BookConfig,
        cfg: ExporterConfig,
        *,
        stage: str | None = None,
        **kwargs: Any,
    ) -> list[Path]:
        """
        Export a single novel (identified by `book_id`) to an EPUB file.
        """
        book_id = book.book_id
        start_id = book.start_id
        end_id = book.end_id
        ignore_set = book.ignore_ids

        # --- Load book data ---
        raw_base = self._raw_data_dir / book_id
        if not raw_base.is_dir():
            return []

        media_dir = raw_base / "media"

        stage = stage or self._detect_latest_stage(book_id)
        book_info = self._load_book_info(book_id, stage=stage)

        # --- Filter volumes & chapters ---
        orig_vols = book_info.get("volumes", [])
        vols = self._filter_volumes(orig_vols, start_id, end_id, ignore_set)
        if not vols:
            logger.info(
                "Nothing to do after filtering (site=%s, book=%s)", self._site, book_id
            )
            return []

        # --- Prepare header (book metadata) ---
        name = book_info["book_name"]
        author = book_info.get("author") or ""

        # --- Generate intro + cover ---
        cover_path = self._resolve_image_path(
            media_dir, book_info.get("cover_url"), name="cover"
        )

        # --- Initialize EPUB ---
        builder = EpubBuilder(
            title=name,
            author=author,
            description=book_info.get("summary", ""),
            cover_path=cover_path,
            subject=book_info.get("tags", []),
            serial_status=book_info.get("serial_status", ""),
            word_count=book_info.get("word_count", ""),
            uid=f"{self._site}_{book_id}",
        )

        # --- Compile columes ---
        seen_cids: set[str] = set()
        skipped = 0
        with ChapterStorage(raw_base, filename=f"chapter.{stage}.sqlite") as storage:
            for v_idx, vol in enumerate(vols, start=1):
                vol_title = vol.get("volume_name") or f"卷 {v_idx}"

                vol_cover = self._resolve_image_path(media_dir, vol.get("volume_cover"))

                curr_vol = EpubVolume(
                    id=f"vol_{v_idx}",
                    title=vol_title,
                    intro=vol.get("volume_intro", ""),
                    cover_path=vol_cover,
                )

                # Collect chapter ids then batch fetch
                cids = [
                    c["chapterId"]
                    for c in vol.get("chapters", [])
                    if c.get("chapterId")
                ]
                if not cids:
                    builder.add_volume(curr_vol)
                    continue
                chap_map = storage.get_chapters(cids)

                # Append each chapter
                for ch_info in vol.get("chapters", []):
                    cid = ch_info.get("chapterId")
                    ch_title = ch_info.get("title")
                    if not cid or cid in seen_cids:
                        continue
                    seen_cids.add(cid)

                    ch = chap_map.get(cid)
                    if not ch:
                        if cfg.render_missing_chapter:
                            chapter_obj = self._xp_epub_missing_chapter(
                                cid=cid,
                                chap_title=ch_title,
                            )
                            curr_vol.chapters.append(chapter_obj)
                        else:
                            skipped += 1
                        continue

                    chapter_obj = self._xp_epub_chapter(
                        book=builder,
                        cid=cid,
                        chap_title=ch_title,
                        chap=ch,
                        media_dir=media_dir,
                        include_picture=cfg.include_picture,
                    )

                    curr_vol.chapters.append(chapter_obj)

                if curr_vol.chapters:
                    builder.add_volume(curr_vol)

        _check_export_consistency(
            fmt="EPUB",
            site=self._site,
            book_id=book_id,
            planned=_count_unique_cids(vols),
            rendered=sum(1 for item in builder.items if isinstance(item, EpubChapter)),
            skipped=skipped,
        )

        # --- Finalize EPUB ---
        raw_name = format_filename(
            cfg.filename_template,
            title=name,
            author=author,
            append_timestamp=cfg.append_timestamp,
            ext="epub",
        )
        out_path = self._output_dir / sanitize_filename(
            _fit_name(raw_name, self._output_dir)
        )

        builder.export(out_path)
        logger.info(
            "Exported EPUB (site=%s, book=%s): %s", self._site, book_id, out_path
        )
        return [out_path]

    def _export_chapter_epub(
        self: "ExportEpubClientContext",
        book_id: str,
        chapter_id: str,
        cfg: ExporterConfig,
        *,
        stage: str | None = None,
        **kwargs: Any,
    ) -> Path | None:
        """Export a single chapter into a standalone EPUB file."""
        raw_base = self._raw_data_dir / book_id
        if not raw_base.is_dir():
            return None

        media_dir = raw_base / "media"

        # --- detect stage ---
        stage = stage or self._detect_latest_stage(book_id)

        # --- load chapter ---
        dbfile = raw_base / f"chapter.{stage}.sqlite"
        if not dbfile.exists():
            return None

        with ChapterStorage(raw_base, filename=f"chapter.{stage}.sqlite") as storage:
            chap = storage.get_chapter(chapter_id)

        if chap is None:
            return None

        chap_title = (chap.get("title") or f"chapter_{chapter_id}").strip()

        builder = EpubBuilder(
            title=chap_title,
            author="Unknown",
            description="",
            cover_path=None,
            subject=[],
            serial_status="",
            word_count="0",
            uid=f"{self._site}_{book_id}_{chapter_id}",
        )

        # --- build chapter XHTML ---
        chapter_obj = self._xp_epub_chapter(
            book=builder,
            cid=chapter_id,
            chap_title=chap_title,
            chap=chap,
            media_dir=media_dir,
            include_picture=cfg.include_picture,
        )

        builder.add_chapter(chapter_obj)

        # --- filename pattern ---
        raw_name = format_filename(
            cfg.filename_template,
            title=chap_title,
            author="Unknown",
            append_timestamp=cfg.append_timestamp,
            ext="epub",
        )
        out_path = self._output_dir / sanitize_filename(
            _fit_name(raw_name, self._output_dir)
        )

        # --- export epub ---
        builder.export(out_path)

        logger.info(
            "Exported EPUB chapter (site=%s, book=%s, chapter=%s): %s",
            self._site,
            book_id,
            chapter_id,
            out_path,
        )
        return out_path

    def _xp_epub_chapter(
        self: "ExportEpubClientContext",
        *,
        book: EpubBuilder,
        cid: str,
        chap_title: str | None,
        chap: ChapterDict,
        media_dir: Path,
        include_picture: bool = True,
    ) -> EpubChapter:
        """
        Build a Chapter object with XHTML content and optionally place images / font.
        """
        # Hand-written / legacy chapter records may store explicit nulls
        # ("title": null), so ``get``'s default never fires: coerce to "".
        title = chap_title or (chap.get("title") or "").strip()
        content = chap.get("content") or ""
        resources: list[MediaResource] = (chap.get("extra") or {}).get(
            "resources"
        ) or []

        lines = content.splitlines()
        max_i = len(lines)

        images_by_index: dict[int, list[MediaResource]] = {}
        added_fonts = []
        html_parts: list[str] = []

        def _append_image(res: MediaResource) -> None:
            fname: str | None = None
            try:
                if url := res.get("url"):
                    local = media_dir / image_filename(url)
                    if local and local.is_file():
                        fname = book.add_image(local)

                elif b64 := res.get("base64"):
                    mime = res.get("mime", "image/png")
                    raw = base64.b64decode(b64)
                    fname = book.add_image_bytes(raw, mime_type=mime)

                if fname:
                    alt = escape(res.get("alt") or "image")
                    tag = f'<img src="../Images/{fname}" alt="{alt}"/>'
                    html_parts.append(self._IMAGE_WRAPPER.format(img=tag))

            except Exception as e:
                logger.debug("EPUB image add failed: %s", e)

        for r in resources:
            typ = r.get("type")

            if typ == "font":
                try:
                    if url := r.get("url"):
                        local = media_dir / font_filename(url)
                        if local.is_file() and (f := book.add_font(local)):
                            added_fonts.append(f)

                    elif b64 := r.get("base64"):
                        raw = base64.b64decode(b64)
                        if f := book.add_font_bytes(raw):
                            added_fonts.append(f)

                except Exception as e:
                    logger.debug("EPUB font add failed: %s", e)

            elif typ == "image" and include_picture:
                idx = r.get("paragraph_index", max_i)
                if idx == 0:
                    _append_image(r)
                    continue

                idx = min(idx, max_i)
                images_by_index.setdefault(idx, []).append(r)

        for i, ln in enumerate(lines, start=1):
            if ln := ln.strip():
                html_parts.append(f"<p>{escape(ln)}</p>")

            if items := images_by_index.get(i):
                for res in items:
                    _append_image(res)

        html_parts = self._xp_epub_chap_post(html_parts, chap)
        xhtml_str = "\n".join(html_parts)
        extra_content = self._xp_epub_extras(chap.get("extra") or {})

        # Sanitize the *whole* filename (extension included): sanitizing only
        # the stem and then appending ".xhtml" can push the component past the
        # 255-character limit that Windows enforces on extraction.
        safe_name = sanitize_filename(f"c{cid}.xhtml", max_length=_MAX_CHAPTER_NAME_LEN)
        safe_id = safe_name.removesuffix(".xhtml")
        return EpubChapter(
            id=f"c_{safe_id}",
            filename=safe_name,
            title=title,
            fonts=added_fonts,
            content=xhtml_str,
            extra_content=extra_content,
        )

    def _xp_epub_missing_chapter(
        self,
        *,
        cid: str,
        chap_title: str | None,
    ) -> EpubChapter:
        """
        Build a placeholder Chapter when content is missing
        or the chapter is inaccessible.
        """
        title = chap_title or f"Chapter {cid}"

        xhtml_str = "<p>本章内容暂不可用</p>"

        safe_name = sanitize_filename(f"c{cid}.xhtml", max_length=_MAX_CHAPTER_NAME_LEN)
        safe_id = safe_name.removesuffix(".xhtml")
        return EpubChapter(
            id=f"c_{safe_id}",
            filename=safe_name,
            title=title,
            fonts=[],
            content=xhtml_str,
            extra_content="",
        )

    def _xp_epub_extras(self, extras: dict[str, Any]) -> str:
        """
        Format the extras dict into a string.

        Subclasses may override this method to render extra info.
        """
        return ""

    @staticmethod
    def _unique_output_path(path: Path, used: set[Path]) -> Path:
        """
        Return ``path`` unless it was already produced in this run.

        When two volumes share the same name (and therefore the same
        filename template output), appending a numeric suffix prevents the
        later export from silently overwriting the earlier one.
        """
        if path not in used:
            used.add(path)
            return path

        stem, suffix = path.stem, path.suffix
        index = 2
        while True:
            candidate = path.with_name(f"{stem}_{index}{suffix}")
            if candidate not in used:
                used.add(candidate)
                return candidate
            index += 1

    def _xp_epub_chap_post(self, html_parts: list[str], chap: ChapterDict) -> list[str]:
        """Allows subclasses to inject HTML or modify structure."""
        return html_parts
