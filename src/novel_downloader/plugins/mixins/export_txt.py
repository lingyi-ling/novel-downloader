#!/usr/bin/env python3
"""
novel_downloader.plugins.mixins.export_txt
------------------------------------------
"""

import contextlib
import hashlib
import logging
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from novel_downloader.infra.persistence.chapter_storage import ChapterStorage
from novel_downloader.libs.filesystem import (
    format_filename,
    sanitize_filename,
)
from novel_downloader.schemas import (
    BookConfig,
    BookInfoDict,
    ChapterDict,
    ExporterConfig,
    VolumeInfoDict,
)

logger = logging.getLogger(__name__)

# Windows still caps most paths at 260 characters (unless long-path support is
# enabled). Keep a margin for the ``.{name}.XXXXXXXX.tmp`` file used by the
# atomic writer, which adds ~13 characters next to the destination.
_MAX_PATH_LEN = 240

#: POSIX caps a single path component at 255 **bytes**, while Windows caps the
#: whole path at 260 UTF-16 units.  A 400-character CJK title is 1,200 bytes,
#: so a character-only budget still produces ``ENAMETOOLONG`` on Linux.
_MAX_COMPONENT_BYTES = 200


def _truncate_utf8(text: str, limit_bytes: int) -> str:
    """Cut ``text`` to at most ``limit_bytes`` of UTF-8 without splitting a char."""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit_bytes:
        return text
    return encoded[:limit_bytes].decode("utf-8", "ignore")


def _fit_name(name: str, parent: Path, limit: int = _MAX_PATH_LEN) -> str:
    """
    Shorten ``name`` so that ``parent / name`` fits the platform limits.

    Two independent budgets apply and the stricter one wins:

    * ``limit`` characters for the whole path (the Windows 260-unit limit, with
      a margin);
    * ``_MAX_COMPONENT_BYTES`` bytes for the name alone (the POSIX per-entry
      limit, which counts bytes rather than characters).

    Only the name shrinks; the extension is preserved and a short digest of
    the original name is appended so two long titles that share a prefix do
    not collapse onto the same output file. The digest is taken from the raw
    (pre-sanitization) name, because ``sanitize_filename`` truncates on its
    own and would otherwise erase the part that distinguishes the two.
    """
    total = len(str(parent)) + 1 + len(name)
    too_long = total > limit
    too_many_bytes = len(name.encode("utf-8")) > _MAX_COMPONENT_BYTES
    if not too_long and not too_many_bytes:
        return name

    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""

    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]
    tail = f"~{digest}" + (f".{ext}" if ext else "")

    char_budget = len(stem)
    if too_long:
        char_budget = max(len(name) - (total - limit) - len(tail), 1)
    byte_budget = max(_MAX_COMPONENT_BYTES - len(tail.encode("utf-8")), 1)

    kept = _truncate_utf8(stem, byte_budget)[:char_budget]
    return f"{kept}{tail}"


def _discard_temp(path: str | Path) -> None:
    """Remove a staging file, warning instead of hiding a failed cleanup."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("Could not remove temporary file %s: %s", path, exc)


def _atomic_write_text(path: Path, text: str) -> Path:
    """
    Write ``text`` to ``path`` through a temporary file in the same directory.

    The temporary file is flushed and fsynced before ``os.replace`` moves it
    into place, so an interrupted export cannot leave a half-written TXT file
    and never destroys an already exported one.

    There is deliberately no "write directly instead" fallback: opening the
    destination for writing truncates it, so a failure halfway through the
    fallback would leave the user with neither the previous export nor a
    complete new one. When the atomic path cannot be completed the error is
    raised (with the destination path in the message) and the old file is
    left untouched -- fail, never corrupt.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

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
        handle = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
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
            handle.write(text)
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

    return path


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
    Warn when the artifact does not account for every planned chapter.

    ``rendered + skipped`` must equal the number of distinct chapters the
    filtered book metadata promised. A mismatch means a chapter was silently
    dropped (or duplicated) by the writer, which is worth a loud log line but
    not worth discarding an otherwise usable export: ``CommonClient.export_book``
    only reports a boolean outcome per format, so raising here would throw away
    the good chapters along with the missing ones.
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

    class ExportTxtClientContext(_ClientContext, Protocol):
        """"""

        def _xp_txt_header(
            self, book_info: BookInfoDict, name: str, author: str
        ) -> str: ...

        def _xp_txt_volume_heading(
            self, vol_title: str, volume: VolumeInfoDict
        ) -> str: ...

        def _xp_txt_chapter(self, chap_title: str | None, chap: ChapterDict) -> str: ...

        def _xp_txt_missing_chapter(
            self,
            *,
            cid: str,
            chap_title: str | None,
        ) -> str: ...

        def _xp_txt_extras(self, extras: dict[str, Any]) -> str: ...


class ExportTxtMixin:
    """"""

    def _export_book_txt(
        self: "ExportTxtClientContext",
        book: BookConfig,
        cfg: ExporterConfig,
        *,
        stage: str | None = None,
        **kwargs: Any,
    ) -> list[Path]:
        """
        Export a novel as a single text file by merging all chapter data.
        """
        book_id = book.book_id
        start_id = book.start_id
        end_id = book.end_id
        ignore_set = book.ignore_ids

        # --- Load book data ---
        raw_base = self._raw_data_dir / book_id
        if not raw_base.is_dir():
            return []

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
        header_txt = self._xp_txt_header(book_info, name, author)

        # --- Build body by volumes & chapters ---
        parts: list[str] = [header_txt]
        seen_cids: set[str] = set()
        planned = _count_unique_cids(vols)
        rendered = 0
        skipped = 0
        with ChapterStorage(raw_base, filename=f"chapter.{stage}.sqlite") as storage:
            for v_idx, volume in enumerate(vols, start=1):
                vol_title = volume.get("volume_name") or f"卷 {v_idx}"
                parts.append(self._xp_txt_volume_heading(vol_title, volume))

                # Collect chapter ids then batch fetch
                cids = [
                    c["chapterId"]
                    for c in volume.get("chapters", [])
                    if c.get("chapterId")
                ]
                if not cids:
                    continue
                chap_map = storage.get_chapters(cids)
                for ch_info in volume.get("chapters", []):
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
                            parts.append(
                                self._xp_txt_missing_chapter(
                                    cid=cid,
                                    chap_title=ch_title,
                                )
                            )
                            rendered += 1
                        else:
                            skipped += 1
                        continue

                    parts.append(self._xp_txt_chapter(ch_title, ch))
                    rendered += 1

        _check_export_consistency(
            fmt="TXT",
            site=self._site,
            book_id=book_id,
            planned=planned,
            rendered=rendered,
            skipped=skipped,
        )

        final_text = "\n".join(parts)

        # --- Determine output file path ---
        # Fit the raw name first: sanitize_filename has its own 255-char cap
        # that would chop off the distinguishing hash suffix before the path
        # length is considered.
        raw_name = format_filename(
            cfg.filename_template,
            title=name,
            author=author,
            append_timestamp=cfg.append_timestamp,
            ext="txt",
        )
        out_path = self._output_dir / sanitize_filename(
            _fit_name(raw_name, self._output_dir)
        )

        # --- Save final text ---
        result = _atomic_write_text(out_path, final_text)
        logger.info(
            "Exported TXT (site=%s, book=%s): %s", self._site, book_id, out_path
        )
        return [result]

    def _export_chapter_txt(
        self: "ExportTxtClientContext",
        book_id: str,
        chapter_id: str,
        cfg: ExporterConfig,
        *,
        stage: str | None = None,
        **kwargs: Any,
    ) -> Path | None:
        """Export a single chapter into a TXT file."""
        raw_base = self._raw_data_dir / book_id
        if not raw_base.is_dir():
            return None

        # --- stage ---
        stage = stage or self._detect_latest_stage(book_id)

        # --- Load chapter ---
        dbfile = raw_base / f"chapter.{stage}.sqlite"
        if not dbfile.exists():
            return None

        with ChapterStorage(raw_base, filename=f"chapter.{stage}.sqlite") as storage:
            chap = storage.get_chapter(chapter_id)

        if chap is None:
            return None

        chap_title = (chap.get("title") or f"chapter_{chapter_id}").strip()
        final_text = self._xp_txt_chapter(chap_title, chap)

        # --- Output file name ---
        raw_name = format_filename(
            cfg.filename_template,
            title=chap_title,
            author="Unknown",
            append_timestamp=cfg.append_timestamp,
            ext="txt",
        )
        out_path = self._output_dir / sanitize_filename(
            _fit_name(raw_name, self._output_dir)
        )

        # --- Save file ---
        result = _atomic_write_text(out_path, final_text)

        logger.info(
            "Exported TXT chapter (site=%s, book=%s, chapter=%s): %s",
            self._site,
            book_id,
            chapter_id,
            out_path,
        )
        return result

    def _xp_txt_header(self, book_info: BookInfoDict, name: str, author: str) -> str:
        """
        Top-of-file metadata block.
        """
        lines: list[str] = [name.strip()]

        if author:
            lines.append(f"作者：{author.strip()}")

        if serial_status := book_info.get("serial_status"):
            lines.append(f"状态：{serial_status.strip()}")

        if word_count := book_info.get("word_count"):
            lines.append(f"字数：{word_count.strip()}")

        if tags_list := book_info.get("tags"):
            tags = "、".join(t.strip() for t in tags_list if t)
            if tags:
                lines.append(f"标签：{tags}")

        if update_time := (book_info.get("update_time") or "").strip():
            lines.append(f"更新：{update_time}")

        if summary := (book_info.get("summary") or "").strip():
            lines.extend(["", summary])

        return "\n".join(lines).strip() + "\n\n"

    def _xp_txt_volume_heading(self, vol_title: str, volume: VolumeInfoDict) -> str:
        """
        Render a volume heading. Include optional info if present.
        """
        meta_bits: list[str] = []

        if v_update_time := volume.get("update_time"):
            meta_bits.append(f"更新时间：{v_update_time}")

        if v_word_count := volume.get("word_count"):
            meta_bits.append(f"字数：{v_word_count}")

        if v_intro := (volume.get("volume_intro") or "").strip():
            meta_bits.append(f"简介：{v_intro}")

        line = f"=== {vol_title.strip()} ==="
        return f"{line}\n" + ("\n".join(meta_bits) + "\n\n" if meta_bits else "\n\n")

    def _xp_txt_chapter(self, chap_title: str | None, chap: ChapterDict) -> str:
        """
        Render one chapter to text
        """
        # Hand-written / legacy chapter records may store explicit nulls
        # ("title": null), so ``get``'s default never fires: coerce to "".
        title_line = chap_title or (chap.get("title") or "").strip()

        cleaned = (chap.get("content") or "").strip()
        body = "\n".join(s for line in cleaned.splitlines() if (s := line.strip()))

        # Extras
        extras_txt = self._xp_txt_extras(chap.get("extra", {}) or {})

        return (
            f"{title_line}\n\n{body}\n\n{extras_txt}\n\n"
            if extras_txt
            else f"{title_line}\n\n{body}\n\n"
        )

    def _xp_txt_missing_chapter(
        self,
        *,
        cid: str,
        chap_title: str | None,
    ) -> str:
        """
        Render a placeholder text block for missing or inaccessible chapters.
        """
        title = chap_title or f"Chapter {cid}"
        return f"{title}\n\n本章内容暂不可用\n\n"

    def _xp_txt_extras(self, extras: dict[str, Any]) -> str:
        """
        Format the extras dict into a string.

        Subclasses may override this method to render extra info.
        """
        return ""
