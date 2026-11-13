#!/usr/bin/env python3
"""
novel_downloader.plugins.mixins.export_epub
-------------------------------------------
"""

import base64
import hashlib
import logging
import re
from dataclasses import replace
from html import escape
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

from novel_downloader.infra.persistence.chapter_storage import ChapterStorage
from novel_downloader.libs.epub_builder import EpubBuilder, EpubChapter, EpubVolume
from novel_downloader.libs.epub_builder.constants import IMAGE_MEDIA_TYPES
from novel_downloader.libs.filesystem import (
    font_filename,
    format_filename,
    image_filename,
    sanitize_filename,
)
from novel_downloader.libs.media.image import detect_image_format
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

#: Characters that are illegal in XML 1.0 (and cannot even be expressed as
#: character references). Metadata is escaped for markup, but a title made
#: only of these would still end up empty in ``<dc:title>``, which strict
#: EPUB validators reject.
_ILLEGAL_XML_CHARS = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")

#: Upper bound for a single embedded image. The media cache can contain
#: multi-hundred-megabyte scans; embedding one makes the whole export
#: unusable (memory and archive size) for very little benefit, so oversized
#: files are skipped instead of being written into every chapter.
_MAX_IMAGE_BYTES = 16 * 1024 * 1024

_UTF8_BOM = b"\xef\xbb\xbf"

#: Short ``schema:accessibilitySummary`` values, chosen by book language and
#: by whether the archive actually carries a navigable table of contents.
_ACCESSIBILITY_SUMMARIES: dict[str, dict[bool, str]] = {
    "zh": {
        True: "纯文本电子书，含结构化目录，无已知无障碍风险。",
        False: "纯文本电子书，无已知无障碍风险。",
    },
    "en": {
        True: (
            "Text-only ebook with structured navigation and no known "
            "accessibility hazards."
        ),
        False: "Text-only ebook with no known accessibility hazards.",
    },
}


def _accessibility_summary(language: str, *, has_toc: bool) -> str:
    """Return a short accessibility summary matching the book language."""
    lang = (language or "").strip().lower()
    return _ACCESSIBILITY_SUMMARIES["zh" if lang.startswith("zh") else "en"][has_toc]


def _apply_accessibility_metadata(builder: EpubBuilder) -> None:
    """
    Add EPUB 3 schema.org accessibility metadata to ``builder``'s OPF.

    ``structuralNavigation`` is only claimed when the book contains at least
    one navigable chapter document: intro/volume pages alone are not a table
    of contents. The values contain no timestamps or other run-dependent
    data, so repeated exports produce identical metadata.
    """
    has_toc = any(isinstance(item, EpubChapter) for item in builder.items)
    opf = builder.opf

    opf.add_meta("schema:accessMode", "textual")
    if has_toc:
        opf.add_meta("schema:accessibilityFeature", "structuralNavigation")
    opf.add_meta("schema:accessibilityHazard", "none")
    opf.add_meta(
        "schema:accessibilitySummary",
        _accessibility_summary(opf.language, has_toc=has_toc),
    )


def _clean_metadata_text(text: str) -> str:
    """Strip characters that cannot appear in the generated XML."""
    return _ILLEGAL_XML_CHARS.sub("", text or "").strip()


def _detect_embeddable_image(data: bytes) -> str | None:
    """
    Return the image format of ``data`` when it is safe to embed, else ``None``.

    Failed downloads leave empty files, truncated bodies or HTML error pages
    behind under an image name, and ``detect_image_format`` reports formats
    (BMP/TIFF/ICO) the EPUB manifest has no media type for. Both must be
    dropped: the builder would otherwise fall back to the file suffix and
    publish, say, an HTML page as ``image/png``.
    """
    head = data[:64].removeprefix(_UTF8_BOM)
    fmt = detect_image_format(head)
    if fmt is None or fmt not in IMAGE_MEDIA_TYPES:
        return None
    return fmt


def _probe_image(path: Path) -> bool:
    """Whether ``path`` is a readable image the EPUB builder can embed."""
    try:
        if not path.is_file():
            return False
        size = path.stat().st_size
        if size == 0 or size > _MAX_IMAGE_BYTES:
            logger.warning(
                "Skipping image %s (%d bytes): empty or over the %d-byte limit",
                path,
                size,
                _MAX_IMAGE_BYTES,
            )
            return False
        with path.open("rb") as fh:
            head = fh.read(64)
    except OSError as exc:
        logger.debug("Cannot read image %s: %s", path, exc)
        return False

    if _detect_embeddable_image(head) is None:
        logger.debug("Skipping unrecognised image data: %s", path)
        return False
    return True


def _usable_image_path(path: Path | None) -> Path | None:
    """Return ``path`` when it can be embedded, otherwise ``None``."""
    if path is None or not _probe_image(path):
        return None
    return path


def _image_cache_url(url: str | None) -> str | None:
    """
    Return the form of *url* the downloader hashes into the media cache.

    ``BaseFetcher._fetch_one_image`` upgrades a protocol-relative reference
    (``//host/x``) to ``https://host/x`` before computing the cache filename;
    every other reference is hashed verbatim. A lookup that skips the upgrade
    reports a downloaded illustration as missing.
    """
    if not url:
        return None
    return "https:" + url if url.startswith("//") else url


def _cached_image_path(media_dir: Path, url: object) -> Path | None:
    """
    Locate the cached file for an image resource URL, or return ``None``.

    The downloader saves each image under a hash of the reference as stored:
    protocol-relative URLs are upgraded to ``https:`` first
    (:meth:`BaseFetcher._fetch_one_image`), while root-relative
    (``/uploads/x.png``) and dotted-relative (``./x.png``, ``../x.png``)
    references are hashed untouched. The lookup must use exactly the same rule,
    otherwise the file is on disk but never embedded.

    Values that cannot name downloaded media are refused: an empty value, a
    non-HTTP scheme (``ftp:``, ``data:``, a Windows drive letter), a malformed
    URL, or a reference whose cache file does not exist. Callers then omit the
    image instead of emitting a dangling ``<img>``.
    """
    if not isinstance(url, str):
        return None

    candidate = url.strip()
    if not candidate:
        return None

    try:
        parsed = urlsplit(candidate)
    except ValueError:
        # ``urlsplit`` rejects e.g. an unterminated IPv6 host.
        return None

    scheme = parsed.scheme.lower()
    if scheme:
        if scheme not in ("http", "https") or not parsed.netloc:
            return None
    elif not parsed.path:
        return None

    try:
        filename = image_filename(_image_cache_url(candidate) or "")
    except ValueError:
        return None

    path = media_dir / filename
    return path if path.is_file() else None


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


def _unique_chapter_filename(filename: str, used: set[str]) -> str:
    """
    Return ``filename`` unless it was already used in this export.

    Different chapter ids can sanitize down to the same filename (``a/b`` and
    ``a_b`` both become ``ca_b``), so the later chapter gets a numeric suffix
    (``_2``, ``_3``, ...). Comparison is case-insensitive (``used`` holds
    case-folded keys): ``cA.xhtml`` and ``ca.xhtml`` are distinct ZIP entries
    but the same file when the archive is extracted on Windows or macOS.
    """
    key = filename.casefold()
    if key not in used:
        used.add(key)
        return filename

    stem, dot, ext = filename.rpartition(".")
    if not dot:
        stem, ext = filename, ""
    index = 2
    while True:
        candidate = f"{stem}_{index}.{ext}" if ext else f"{stem}_{index}"
        key = candidate.casefold()
        if key not in used:
            used.add(key)
            return candidate
        index += 1


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
        # Hand-written metadata may omit "book_name"; fall back to the id so
        # the EPUB still gets a non-empty dc:title.
        book_name = _clean_metadata_text(book_info.get("book_name") or "")
        name = book_name or _clean_metadata_text(book_id)
        author = book_info.get("author") or ""
        book_summary = book_info.get("summary", "")

        # --- Generate intro + cover ---
        # Downloads cache the book cover as ``cover.<ext>``; resolving it
        # without ``name="cover"`` silently dropped the cover of every split
        # volume.
        cover_path = _usable_image_path(
            self._resolve_image_path(
                media_dir, book_info.get("cover_url"), name="cover"
            )
        )

        # --- Compile columes ---
        outputs: list[Path] = []
        used_paths: set[Path] = set()
        with ChapterStorage(raw_base, filename=f"chapter.{stage}.sqlite") as storage:
            for v_idx, vol in enumerate(vols, start=1):
                vol_title = vol.get("volume_name") or f"卷 {v_idx}"

                vol_cover = _usable_image_path(
                    self._resolve_image_path(
                        media_dir, _image_cache_url(vol.get("volume_cover"))
                    )
                )
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
                used_chapter_files: set[str] = set()
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
                            builder.add_chapter(
                                replace(
                                    chapter_obj,
                                    filename=_unique_chapter_filename(
                                        chapter_obj.filename, used_chapter_files
                                    ),
                                )
                            )
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
                    builder.add_chapter(
                        replace(
                            chapter_obj,
                            filename=_unique_chapter_filename(
                                chapter_obj.filename, used_chapter_files
                            ),
                        )
                    )

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

                _apply_accessibility_metadata(builder)
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
        # Hand-written metadata may omit "book_name"; fall back to the id so
        # the EPUB still gets a non-empty dc:title.
        book_name = _clean_metadata_text(book_info.get("book_name") or "")
        name = book_name or _clean_metadata_text(book_id)
        author = book_info.get("author") or ""

        # --- Generate intro + cover ---
        cover_path = _usable_image_path(
            self._resolve_image_path(
                media_dir, book_info.get("cover_url"), name="cover"
            )
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
        # Chapter filenames are unique book-wide: the archive is extracted as
        # one tree, and case-insensitive filesystems would otherwise collapse
        # ``cA.xhtml`` and ``ca.xhtml`` onto a single chapter.
        used_chapter_files: set[str] = set()
        skipped = 0
        with ChapterStorage(raw_base, filename=f"chapter.{stage}.sqlite") as storage:
            for v_idx, vol in enumerate(vols, start=1):
                vol_title = vol.get("volume_name") or f"卷 {v_idx}"

                vol_cover = _usable_image_path(
                    self._resolve_image_path(
                        media_dir, _image_cache_url(vol.get("volume_cover"))
                    )
                )

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

                    chapter_obj = self._xp_epub_chapter(
                        book=builder,
                        cid=cid,
                        chap_title=ch_title,
                        chap=ch,
                        media_dir=media_dir,
                        include_picture=cfg.include_picture,
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

        _apply_accessibility_metadata(builder)
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

        # The chapter title becomes the EPUB's dc:title; strip characters that
        # would leave it empty in the generated XML.
        chap_title = _clean_metadata_text(chap.get("title") or "") or (
            f"chapter_{chapter_id}"
        )

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
        _apply_accessibility_metadata(builder)
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
                    local = _cached_image_path(media_dir, url)
                    if local is not None and _probe_image(local):
                        fname = book.add_image(local)

                # A resource may carry an inline copy as well; use it when the
                # download is missing or unusable instead of dropping the
                # illustration.
                if not fname and (b64 := res.get("base64")):
                    # Reject absurd payloads before decoding so a corrupt or
                    # hostile resource cannot balloon memory usage.
                    if len(b64) > (_MAX_IMAGE_BYTES * 4 // 3) + 4:
                        logger.warning("Skipping oversized inline image resource")
                    else:
                        raw = base64.b64decode(b64)
                        fmt = _detect_embeddable_image(raw)
                        if fmt is not None and len(raw) <= _MAX_IMAGE_BYTES:
                            fname = book.add_image_bytes(
                                raw, mime_type=IMAGE_MEDIA_TYPES[fmt]
                            )

                if fname:
                    # Parser-provided alts are not guaranteed to be strings;
                    # a non-string used to raise and silently drop the image.
                    alt = escape(str(res.get("alt") or "image"))
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
                # Hand-written metadata bypasses type checking, so a null or
                # string index must not abort the whole chapter.
                raw_idx = r.get("paragraph_index", max_i)
                try:
                    idx = int(raw_idx)
                except (TypeError, ValueError):
                    idx = max_i

                # Index 0 means "before the text"; negative indexes would
                # otherwise never match a line and silently disappear.
                if idx <= 0 or max_i == 0:
                    _append_image(r)
                    continue

                images_by_index.setdefault(min(idx, max_i), []).append(r)

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
