#!/usr/bin/env python3
"""
novel_downloader.plugins.mixins.export_html
-------------------------------------------
"""

import base64
import hashlib
import logging
from dataclasses import replace
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from novel_downloader.infra.persistence.chapter_storage import ChapterStorage
from novel_downloader.libs.filesystem import (
    font_filename,
    format_filename,
    image_filename,
    sanitize_filename,
)
from novel_downloader.libs.html_builder import HtmlBuilder, HtmlChapter, HtmlVolume
from novel_downloader.libs.html_builder.constants import CHAPTER_DIR as _CHAPTER_DIR
from novel_downloader.schemas import (
    BookConfig,
    ChapterDict,
    ExporterConfig,
    MediaResource,
    VolumeInfoDict,
)

logger = logging.getLogger(__name__)

# Windows still caps most paths at 260 characters (unless long-path support is
# enabled). Chapters live at ``<folder>/chapters/<name>``, so the folder gets a
# tighter budget: the extra directory level, the chapter filename and the
# temporary-file suffix used by the atomic writer must all fit under the
# limit. ``_chapter_name_limit`` shrinks the chapter filename further when the
# output directory itself is already deep.
_MAX_PATH_LEN = 240
_MAX_DIR_PATH_LEN = 150
_MAX_CHAPTER_NAME_LEN = 72

# Extra characters added by the writer while building the export:
#   * the staging directory name is ``.<folder>.XXXXXXXX.tmp`` (+14)
#   * each file is written through ``.<name>.XXXXXXXX.tmp`` (+13)
_STAGING_SUFFIX_LEN = 14
_TMP_SUFFIX_LEN = 13


def _fit_name(name: str, parent: Path, limit: int = _MAX_PATH_LEN) -> str:
    """
    Shorten ``name`` so that ``parent / name`` fits within ``limit`` chars.

    Only the name shrinks; the extension is preserved and a short digest of
    the original name is appended so two long titles that share a prefix do
    not collapse onto the same output folder. The digest is taken from the raw
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


def _chapter_name_limit(output_dir: Path, folder: str) -> int:
    """
    Longest chapter filename that keeps ``<output>/<folder>/chapters/<name>``
    within :data:`_MAX_PATH_LEN`, even before the atomic writer's ``.tmp``
    suffix.

    The fixed :data:`_MAX_CHAPTER_NAME_LEN` is only safe while the output
    folder is shallow. With a deep output directory the chapter files (not
    just the folder) can cross the Windows limit, so the remaining budget is
    computed from the real destination.
    """
    prefix = len(str(output_dir)) + 1 + len(folder) + 1 + len(_CHAPTER_DIR) + 1
    reserve = _STAGING_SUFFIX_LEN + _TMP_SUFFIX_LEN
    return max(8, min(_MAX_CHAPTER_NAME_LEN, _MAX_PATH_LEN - prefix - reserve))


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
    Warn when the folder does not account for every planned chapter.

    ``rendered + skipped`` must equal the number of distinct chapters the
    filtered book metadata promised. A mismatch means the builder dropped a
    chapter that was handed to it; the folder is still usable, so this is
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


def _unique_chapter_filename(filename: str, used: set[str]) -> str:
    """
    Return ``filename`` unless it was already used in this export.

    Two chapter ids can sanitize down to the same filename (``a/b`` and
    ``a_b`` both become ``ca_b``); appending a numeric suffix (``_2``,
    ``_3``, ...) then keeps the later chapter from silently overwriting
    the earlier one and keeps the index pointing at both files.
    """
    if filename not in used:
        used.add(filename)
        return filename

    stem, dot, ext = filename.rpartition(".")
    if not dot:
        stem, ext = filename, ""
    index = 2
    while True:
        candidate = f"{stem}_{index}.{ext}" if ext else f"{stem}_{index}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        index += 1


if TYPE_CHECKING:
    from novel_downloader.plugins.protocols import _ClientContext

    class ExportHtmlClientContext(_ClientContext, Protocol):
        """"""

        def _xp_html_chapter(
            self,
            *,
            builder: HtmlBuilder,
            cid: str,
            chap_title: str | None,
            chap: ChapterDict,
            media_dir: Path | None = None,
            include_picture: bool = True,
            max_name_length: int = _MAX_CHAPTER_NAME_LEN,
        ) -> HtmlChapter: ...

        def _xp_html_missing_chapter(
            self,
            *,
            cid: str,
            chap_title: str | None,
            max_name_length: int = _MAX_CHAPTER_NAME_LEN,
        ) -> HtmlChapter: ...

        def _xp_html_extras(self, extras: dict[str, Any]) -> str: ...

        def _xp_html_chap_post(
            self, html_parts: list[str], chap: ChapterDict
        ) -> list[str]: ...


class ExportHtmlMixin:
    """"""

    def _export_book_html(
        self: "ExportHtmlClientContext",
        book: BookConfig,
        cfg: ExporterConfig,
        *,
        stage: str | None = None,
        **kwargs: Any,
    ) -> list[Path]:
        """
        Export a novel as HTML files.
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
        cover_path = self._resolve_image_path(
            media_dir, book_info.get("cover_url"), name="cover"
        )
        cover = cover_path.read_bytes() if cover_path else None

        # --- Initialize EPUB ---
        builder = HtmlBuilder(
            title=name,
            author=author,
            description=book_info.get("summary", ""),
            cover=cover,
            subject=book_info.get("tags", []),
            serial_status=book_info.get("serial_status", ""),
            word_count=book_info.get("word_count", ""),
        )

        # --- Compile columes ---
        seen_cids: set[str] = set()
        skipped = 0
        # The output folder is known before the chapters are rendered, so the
        # per-chapter filename budget can be derived from the real destination
        # (a deep output directory leaves less room under the Windows limit).
        raw_name = format_filename(
            cfg.filename_template,
            title=name,
            author=author,
            append_timestamp=cfg.append_timestamp,
        )
        folder = sanitize_filename(
            _fit_name(raw_name, self._output_dir, _MAX_DIR_PATH_LEN)
        )
        chapter_name_limit = _chapter_name_limit(self._output_dir, folder)
        # Chapter filenames are derived from cids; distinct cids can collide
        # after sanitization ("a/b" vs "a_b"), so reserve them book-wide.
        used_chapter_files: set[str] = set()
        with ChapterStorage(raw_base, filename=f"chapter.{stage}.sqlite") as storage:
            for v_idx, vol in enumerate(vols, start=1):
                vol_title = vol.get("volume_name") or f"卷 {v_idx}"

                curr_vol = HtmlVolume(
                    title=vol_title,
                    intro=vol.get("volume_intro", ""),
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
                for ch_info in vol.get("chapters", []):
                    cid = ch_info.get("chapterId")
                    ch_title = ch_info.get("title")
                    # A chapter may be linked more than once in the same
                    # volume (or across volumes); keep only the first hit.
                    if not cid or cid in seen_cids:
                        continue
                    seen_cids.add(cid)

                    ch = chap_map.get(cid)
                    if not ch:
                        if cfg.render_missing_chapter:
                            chapter_obj = self._xp_html_missing_chapter(
                                cid=cid,
                                chap_title=ch_title,
                                max_name_length=chapter_name_limit,
                            )
                            chapter_obj = replace(
                                chapter_obj,
                                filename=_unique_chapter_filename(
                                    chapter_obj.filename, used_chapter_files
                                ),
                            )
                            curr_vol.chapters.append(chapter_obj)
                        else:
                            skipped += 1
                        continue

                    chapter_obj = self._xp_html_chapter(
                        builder=builder,
                        cid=cid,
                        chap_title=ch_title,
                        chap=ch,
                        media_dir=media_dir,
                        include_picture=cfg.include_picture,
                        max_name_length=chapter_name_limit,
                    )
                    chapter_obj = replace(
                        chapter_obj,
                        filename=_unique_chapter_filename(
                            chapter_obj.filename, used_chapter_files
                        ),
                    )

                    curr_vol.chapters.append(chapter_obj)

                if curr_vol.chapters:
                    builder.add_volume(curr_vol)

        _check_export_consistency(
            fmt="HTML",
            site=self._site,
            book_id=book_id,
            planned=_count_unique_cids(vols),
            rendered=builder.chapter_count,
            skipped=skipped,
        )

        out_path = builder.export(self._output_dir, folder=folder)
        logger.info(
            "Exported HTML (site=%s, book=%s): %s", self._site, book_id, out_path
        )
        return [out_path]

    def _export_chapter_html(
        self: "ExportHtmlClientContext",
        book_id: str,
        chapter_id: str,
        cfg: ExporterConfig,
        *,
        stage: str | None = None,
        **kwargs: Any,
    ) -> Path | None:
        """Export a single chapter into a HTML file."""
        raw_base = self._raw_data_dir / book_id
        if not raw_base.is_dir():
            return None

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

        # ---- basic fields ----
        chap_title = (chap.get("title") or f"chapter_{chapter_id}").strip()
        media_dir = raw_base / "media"

        # ---- builder (no metadata known) ----
        builder = HtmlBuilder(
            title=chap_title,
            author="Unknown",
            description="",
            cover=None,
            subject=[],
            serial_status="",
            word_count="0",
        )

        # ---- build a chapter ----
        raw_name = format_filename(
            cfg.filename_template,
            title=chap_title,
            author="Unknown",
            append_timestamp=cfg.append_timestamp,
        )
        folder = sanitize_filename(
            _fit_name(raw_name, self._output_dir, _MAX_DIR_PATH_LEN)
        )
        chapter_obj = self._xp_html_chapter(
            builder=builder,
            cid=chapter_id,
            chap_title=chap_title,
            chap=chap,
            media_dir=media_dir,
            include_picture=cfg.include_picture,
            max_name_length=_chapter_name_limit(self._output_dir, folder),
        )

        builder.add_chapter(chapter_obj)

        # ---- output file/folder name ----
        out_path = builder.export(self._output_dir, folder=folder)

        logger.info(
            "Exported HTML chapter (site=%s, book=%s, chapter=%s): %s",
            self._site,
            book_id,
            chapter_id,
            out_path,
        )

        return out_path

    def _xp_html_chapter(
        self: "ExportHtmlClientContext",
        *,
        builder: HtmlBuilder,
        cid: str,
        chap_title: str | None,
        chap: ChapterDict,
        media_dir: Path,
        include_picture: bool = True,
        max_name_length: int = _MAX_CHAPTER_NAME_LEN,
    ) -> HtmlChapter:
        """
        Build a Chapter object with HTML content and optionally place images / font.
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
                    if url.startswith("//"):
                        url = "https:" + url
                    if url.startswith(("http://", "https://")):
                        local = media_dir / image_filename(url)
                        if local and local.is_file():
                            fname = builder.add_image(local)

                elif b64 := res.get("base64"):
                    mime = res.get("mime", "image/png")
                    raw = base64.b64decode(b64)
                    fname = builder.add_image_bytes(raw, mime_type=mime)

                if fname:
                    alt = escape(res.get("alt") or "image")
                    html_parts.append(
                        f'<img src="../media/{fname}" alt="{alt}" class="chapter-image"/>'  # noqa: E501
                    )

            except Exception as e:
                logger.debug("HTML image add failed: %s", e)

        for r in resources:
            typ = r.get("type")

            if typ == "font":
                try:
                    if url := r.get("url"):
                        local = media_dir / font_filename(url)
                        if local.is_file() and (f := builder.add_font(local)):
                            added_fonts.append(f)

                    elif b64 := r.get("base64"):
                        raw = base64.b64decode(b64)
                        if f := builder.add_font_bytes(raw):
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

        html_parts = self._xp_html_chap_post(html_parts, chap)
        html_str = "\n".join(html_parts)
        extras_part = self._xp_html_extras(chap.get("extra") or {})

        return HtmlChapter(
            filename=sanitize_filename(f"c{cid}.html", max_length=max_name_length),
            title=title,
            content=html_str,
            extra_content=extras_part,
            fonts=added_fonts,
        )

    def _xp_html_missing_chapter(
        self,
        *,
        cid: str,
        chap_title: str | None,
        max_name_length: int = _MAX_CHAPTER_NAME_LEN,
    ) -> HtmlChapter:
        """
        Build a placeholder HTML chapter when content is missing
        or inaccessible.
        """
        title = chap_title or f"Chapter {cid}"

        html_str = "<p>本章内容暂不可用</p>"

        return HtmlChapter(
            filename=sanitize_filename(f"c{cid}.html", max_length=max_name_length),
            title=title,
            content=html_str,
            extra_content="",
            fonts=[],
        )

    def _xp_html_extras(self, extras: dict[str, Any]) -> str:
        """
        Format the extras dict into a string.

        Subclasses may override this method to render extra info.
        """
        return ""

    def _xp_html_chap_post(self, html_parts: list[str], chap: ChapterDict) -> list[str]:
        """Allows subclasses to inject HTML or modify structure."""
        return html_parts
