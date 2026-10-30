#!/usr/bin/env python3
"""
Offline test harness for the exporter mixins (``plugins/mixins/export_*``).

The exporter mixins only need a tiny slice of the client surface, so we drive
them through a concrete :class:`CommonClient` subclass whose network-facing
abstract methods are no-ops.  Book metadata and chapter content are written to
a temporary directory with the same on-disk layout used in production
(``book_info.<stage>.json`` + ``chapter.<stage>.sqlite``), which keeps the tests
fully offline while still exercising the real dispatch and helper code.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from novel_downloader.infra.persistence.chapter_storage import ChapterStorage
from novel_downloader.plugins.common.client import CommonClient
from novel_downloader.schemas import (
    BookConfig,
    ChapterDict,
    ClientConfig,
    ExporterConfig,
)

# A minimal but valid PNG payload (magic header + padding) so that
# ``detect_image_format`` recognises it as ``png``.
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8


class HarnessClient(CommonClient):
    """A ``CommonClient`` with every network-facing abstract method stubbed."""

    async def login(
        self,
        *,
        ui: Any,
        login_cfg: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> bool:
        return True

    async def download_book(
        self,
        book: BookConfig,
        *,
        ui: Any = None,
        **kwargs: Any,
    ) -> None:
        return None

    def process_book(
        self,
        book: BookConfig,
        processors: list[Any],
        *,
        ui: Any = None,
        **kwargs: Any,
    ) -> None:
        return None

    async def cache_media(
        self,
        book: BookConfig,
        *,
        force_update: bool = False,
        concurrent: int = 10,
        **kwargs: Any,
    ) -> None:
        return None


@dataclass
class Scenario:
    """A book staged on disk together with the client that exports it."""

    client: HarnessClient
    raw_dir: Path
    output_dir: Path
    book_id: str = "b1"
    stage: str = "raw"
    start_id: str | None = None
    end_id: str | None = None
    ignore_ids: frozenset[str] = frozenset()

    @property
    def book(self) -> BookConfig:
        return BookConfig(
            book_id=self.book_id,
            start_id=self.start_id,
            end_id=self.end_id,
            ignore_ids=self.ignore_ids,
        )

    @property
    def book_dir(self) -> Path:
        return self.raw_dir / self.book_id

    def write_book_info(
        self, book_info: dict[str, Any], *, stage: str | None = None
    ) -> None:
        self.client._save_book_info(self.book_id, book_info, stage=stage or self.stage)

    def write_chapters(
        self, chapters: list[ChapterDict], *, stage: str | None = None
    ) -> None:
        base = self.book_dir
        base.mkdir(parents=True, exist_ok=True)
        with ChapterStorage(base, f"chapter.{stage or self.stage}.sqlite") as storage:
            storage.upsert_chapters(chapters)

    def write_media(self, filename: str, data: bytes = PNG_BYTES) -> Path:
        media = self.book_dir / "media"
        media.mkdir(parents=True, exist_ok=True)
        path = media / filename
        path.write_bytes(data)
        return path

    def export(
        self, fmt: str, cfg: ExporterConfig | None = None
    ) -> dict[str, list[Path]]:
        return self.client.export_book(self.book, cfg, formats=[fmt])

    def export_chapter(
        self,
        chapter_id: str,
        fmt: str,
        cfg: ExporterConfig | None = None,
    ) -> dict[str, Path | None]:
        return self.client.export_chapter(self.book_id, chapter_id, cfg, formats=[fmt])

    def make_client(self) -> HarnessClient:
        return self.client


def new_scenario(tmp_path: Path, *, site: str = "site") -> Scenario:
    """Create an isolated scenario rooted at ``tmp_path``."""
    raw = tmp_path / "raw_data"
    out = tmp_path / "downloads"
    out.mkdir(parents=True, exist_ok=True)
    cfg = ClientConfig(raw_data_dir=str(raw), output_dir=str(out))
    return Scenario(client=HarnessClient(site, cfg), raw_dir=raw / site, output_dir=out)


def make_book_info(
    name: str = "测试书",
    author: str = "作者",
    volumes: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    info: dict[str, Any] = {
        "book_name": name,
        "author": author,
        "cover_url": "",
        "update_time": "",
        "summary": "",
        "extra": {},
        "volumes": volumes if volumes is not None else [],
    }
    info.update(extra)
    return info


def make_volume(
    name: str,
    chapters: list[dict[str, Any]],
    **extra: Any,
) -> dict[str, Any]:
    vol: dict[str, Any] = {"volume_name": name, "chapters": chapters}
    vol.update(extra)
    return vol


def make_chapter_info(cid: str, title: str, **extra: Any) -> dict[str, Any]:
    info: dict[str, Any] = {
        "title": title,
        "url": f"https://example.com/{cid}",
        "chapterId": cid,
    }
    info.update(extra)
    return info


def make_chapter(
    cid: str,
    content: str,
    title: str | None = None,
    extra: dict[str, Any] | None = None,
) -> ChapterDict:
    return ChapterDict(
        id=cid,
        title=title if title is not None else f"第{cid}章",
        content=content,
        extra=extra if extra is not None else {},
    )
