#!/usr/bin/env python3
"""Durability and size rules for the music settings file.

The desktop settings panel, the embedded engine and the REST music endpoints
all read this file (and the desktop writes it), so an oversized document must
not be loaded whole and a save must never truncate the previous file.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from novel_downloader.apps.music import settings as music_settings


@pytest.fixture()
def settings_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "music_settings.json"
    monkeypatch.setattr(music_settings, "settings_file", lambda: path)
    return path


def test_an_oversized_settings_file_is_ignored(settings_path: Path) -> None:
    settings_path.write_text(
        json.dumps({"level": "oversized", "padding": "x" * (300 * 1024)}),
        encoding="utf-8",
    )

    loaded = music_settings.load_settings()

    assert loaded["level"] == "exhigh"


def test_save_does_not_truncate_the_file_in_place(
    settings_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A save is a temp-file write followed by ``os.replace``."""
    settings_path.write_text(json.dumps({"level": "lossless"}), encoding="utf-8")

    def explode(*_args: object, **_kwargs: object) -> int:
        raise AssertionError("save_settings must not write the target in place")

    monkeypatch.setattr(Path, "write_text", explode)

    music_settings.save_settings({"level": "exhigh"})

    assert json.loads(settings_path.read_text(encoding="utf-8"))["level"] == "exhigh"


def test_concurrent_saves_do_not_lose_updates(settings_path: Path) -> None:
    workers = 4
    barrier = threading.Barrier(workers)

    def save(index: int) -> None:
        barrier.wait()
        for _ in range(10):
            music_settings.save_settings({f"key_{index}": index})

    threads = [threading.Thread(target=save, args=(index,)) for index in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    stored = json.loads(settings_path.read_text(encoding="utf-8"))
    assert {key: stored[key] for key in stored if key.startswith("key_")} == {
        f"key_{index}": index for index in range(workers)
    }
