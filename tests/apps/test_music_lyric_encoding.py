"""Encoding and corrupted-input tests for music lyric parsing/writing.

Pinned behaviour of ``apps.music.downloader``:

* sidecars are written as UTF-8 **without a BOM**, LF-only, one trailing
  newline, with the text otherwise byte-faithful (time tags survive);
* sidecars are read with a fixed detection order -- BOM (UTF-32 / UTF-8 /
  UTF-16), then strict UTF-8, then strict GB18030 (GBK/GB2312 superset);
* unsupported or corrupted input (invalid bytes, mixed encodings, BOM-less
  UTF-16, NUL/control characters, files over 1 MiB) is skipped instead of
  being embedded as mojibake, and never raises into the download flow;
* ``.lrc`` / ``.zh.lrc`` selection follows the documented original versus
  translation rules;
* embedded lyrics (ID3 USLT / TXXX, Vorbis, MP4) decode the same texts and
  degrade to an empty string for corrupt containers.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from novel_downloader.apps.music import downloader
from novel_downloader.apps.music.downloader import (
    MAX_LYRIC_BYTES,
    DownloadOptions,
    save_lyric,
)
from novel_downloader.apps.music.providers.base import ResolveResult, Track

_UTF8_BOM = b"\xef\xbb\xbf"
_UTF16_LE_BOM = b"\xff\xfe"
_UTF16_BE_BOM = b"\xfe\xff"
_UTF32_LE_BOM = b"\xff\xfe\x00\x00"

_LYRIC = "[00:01.00]你好\n[00:02.345]世界"

#: Minimal MPEG-1 Layer III frames (128 kbps, 44.1 kHz) accepted by mutagen.
_MP3_FRAME = b"\xff\xfb\x90\x64" + b"\x00" * 413


def _mp3_bytes(frames: int = 4) -> bytes:
    return _MP3_FRAME * frames


def _sidecar(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _read(path: Path) -> str:
    return downloader._read_lyric_file(str(path))


# ---------------------------------------------------------------------------
# Sidecar reading: supported encodings and detection order
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(_LYRIC.encode("utf-8"), id="utf-8"),
        pytest.param(_UTF8_BOM + _LYRIC.encode("utf-8"), id="utf-8-bom"),
        pytest.param(_UTF16_LE_BOM + _LYRIC.encode("utf-16-le"), id="utf-16-le-bom"),
        pytest.param(_UTF16_BE_BOM + _LYRIC.encode("utf-16-be"), id="utf-16-be-bom"),
        pytest.param(_UTF32_LE_BOM + _LYRIC.encode("utf-32-le"), id="utf-32-le-bom"),
    ],
)
def test_read_supported_encodings(tmp_path: Path, data: bytes) -> None:
    path = _sidecar(tmp_path, "song.lrc", data)

    assert _read(path) == _LYRIC


@pytest.mark.parametrize("encoding", ["gbk", "gb18030"])
def test_read_legacy_chinese_sidecar(tmp_path: Path, encoding: str) -> None:
    text = "[00:03.00]歌词测试"
    path = _sidecar(tmp_path, "song.lrc", text.encode(encoding))

    assert _read(path) == text


def test_read_gb18030_four_byte_character(tmp_path: Path) -> None:
    """GB18030 (not plain GBK) is needed for supplementary-plane Han."""
    text = "[00:04.00]𠀀"
    path = _sidecar(tmp_path, "song.lrc", text.encode("gb18030"))

    assert _read(path) == text


@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be"])
def test_bomless_utf16_is_skipped(tmp_path: Path, encoding: str) -> None:
    """Without a BOM, ASCII UTF-16 is valid UTF-8 full of NULs.

    Accepting that byte string would embed NUL-laden garbage in the tag, so
    the reader must reject it rather than return it.
    """
    path = _sidecar(tmp_path, "song.lrc", _LYRIC.encode(encoding))

    assert _read(path) == ""


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"\xff\xff", id="invalid-0xff"),
        pytest.param(b"\xd2", id="truncated-gbk-lead"),
        pytest.param(b"\x80", id="lone-gb18030-byte"),
        pytest.param(b"\xed\xa0\x80", id="utf-8-surrogate"),
    ],
)
def test_invalid_byte_sequences_are_skipped(tmp_path: Path, data: bytes) -> None:
    path = _sidecar(tmp_path, "song.lrc", data)

    assert _read(path) == ""


def test_mixed_encoding_file_is_skipped_not_raised(tmp_path: Path) -> None:
    utf8 = "[00:01.00]你好\n".encode()
    gbk = "[00:02.00]世界\n".encode("gbk")
    path = _sidecar(tmp_path, "song.lrc", utf8 + gbk + b"\xff")

    assert _read(path) == ""


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"", id="empty"),
        pytest.param(_UTF8_BOM, id="utf-8-bom-only"),
        pytest.param(_UTF16_LE_BOM, id="utf-16-le-bom-only"),
        pytest.param(_UTF16_BE_BOM, id="utf-16-be-bom-only"),
        pytest.param(b"\n", id="lf-only"),
        pytest.param(b"\r\n\r\n", id="crlf-only"),
        pytest.param(b" \t\r\n", id="whitespace-only"),
    ],
)
def test_empty_bom_only_and_blank_files_read_as_empty(
    tmp_path: Path, data: bytes
) -> None:
    path = _sidecar(tmp_path, "song.lrc", data)

    assert _read(path) == ""


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"[00:01.00]ok\x00\n", id="nul-in-text"),
        pytest.param(b"\x00\x01\x02", id="only-controls"),
        pytest.param(b"[00:01.00]ok\x07\n", id="bell"),
    ],
)
def test_binary_control_characters_are_skipped(tmp_path: Path, data: bytes) -> None:
    path = _sidecar(tmp_path, "song.lrc", data)

    assert _read(path) == ""


def test_read_file_over_one_mib_is_skipped(tmp_path: Path) -> None:
    path = _sidecar(tmp_path, "big.lrc", b"a" * (MAX_LYRIC_BYTES + 1))

    assert _read(path) == ""


def test_read_file_at_exact_cap_is_accepted(tmp_path: Path) -> None:
    # The newline keeps the decoded text within the documented cap.
    data = b"a" * (MAX_LYRIC_BYTES - 1) + b"\n"
    assert len(data) == MAX_LYRIC_BYTES
    path = _sidecar(tmp_path, "cap.lrc", data)

    assert _read(path) == "a" * (MAX_LYRIC_BYTES - 1)


def test_missing_and_directory_paths_read_as_empty(tmp_path: Path) -> None:
    assert _read(tmp_path / "missing.lrc") == ""
    # Opening a directory raises OSError on every platform; it must be
    # treated like any other unreadable lyric file.
    assert _read(tmp_path) == ""


# ---------------------------------------------------------------------------
# Newlines, whitespace and LRC timestamps
# ---------------------------------------------------------------------------


def test_read_normalizes_crlf_lf_and_lone_cr(tmp_path: Path) -> None:
    raw = b"[00:01.00]a\r\n[00:02.00]b\r[00:03.00]c\n"
    path = _sidecar(tmp_path, "song.lrc", raw)

    assert _read(path) == "[00:01.00]a\n[00:02.00]b\n[00:03.00]c"


def test_read_keeps_inner_trailing_spaces_and_trims_the_tail(
    tmp_path: Path,
) -> None:
    raw = b"[00:01.00]a  \r\n[00:02.00]b\t\r\n[00:03.00]c\t\n\n"
    path = _sidecar(tmp_path, "song.lrc", raw)

    # Line-internal padding is content; only the file tail is stripped.
    assert _read(path) == "[00:01.00]a  \n[00:02.00]b\t\n[00:03.00]c"


_TIMELINE = (
    "[ti:Title]\n"
    "[ar:Artist]\n"
    "[00:01.00]centiseconds\n"
    "[00:02.345]milliseconds\n"
    "[00:03.00][00:04.50]multi tag line\n"
    "[01:02:03]hour form left untouched"
)


def test_timestamp_formats_survive_save_and_read(tmp_path: Path) -> None:
    audio = tmp_path / "song.mp3"

    written = save_lyric(str(audio), _TIMELINE)

    assert written == [str(tmp_path / "song.lrc")]
    raw = (tmp_path / "song.lrc").read_bytes()
    assert raw == (_TIMELINE + "\n").encode("utf-8")
    assert _read(tmp_path / "song.lrc") == _TIMELINE


def test_save_lyric_normalizes_crlf_and_lone_cr(tmp_path: Path) -> None:
    audio = tmp_path / "song.mp3"

    save_lyric(str(audio), "[00:01.00]a  \r\n[00:02.00]b\r[00:03.00]c\t")

    assert (tmp_path / "song.lrc").read_bytes() == (
        b"[00:01.00]a  \n[00:02.00]b\n[00:03.00]c\n"
    )


# ---------------------------------------------------------------------------
# save_lyric: output contract and .lrc / .zh.lrc selection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("lyric", "translated", "expected"),
    [
        pytest.param(
            "[00:01.00]orig",
            "[00:01.00]trans",
            {
                "song.lrc": "[00:01.00]orig\n",
                "song.zh.lrc": "[00:01.00]trans\n",
            },
            id="original-and-translation",
        ),
        pytest.param(
            "[00:01.00]orig",
            "",
            {"song.lrc": "[00:01.00]orig\n"},
            id="original-only",
        ),
        pytest.param(
            " \r\n",
            "[00:02.00]trans",
            {"song.lrc": "[00:02.00]trans\n"},
            id="translation-only-goes-to-main-lrc",
        ),
        pytest.param("", "", {}, id="both-blank-write-nothing"),
        pytest.param(
            "\ufeff[00:01.00]orig",
            "\ufeff[00:03.00]trans",
            {
                "song.lrc": "[00:01.00]orig\n",
                "song.zh.lrc": "[00:03.00]trans\n",
            },
            id="leading-ufeff-is-not-written-as-a-bom",
        ),
    ],
)
def test_save_lyric_sidecar_selection(
    tmp_path: Path,
    lyric: str,
    translated: str,
    expected: dict[str, str],
) -> None:
    audio = tmp_path / "song.flac"

    written = save_lyric(str(audio), lyric, translated)

    assert written == [str(tmp_path / name) for name in expected]
    actual = {
        path.name: path.read_text(encoding="utf-8") for path in tmp_path.glob("*.lrc")
    }
    assert actual == expected
    for path in tmp_path.glob("*.lrc"):
        assert not path.read_bytes().startswith(_UTF8_BOM)


def test_save_lyric_bom_only_text_writes_nothing(tmp_path: Path) -> None:
    written = save_lyric(str(tmp_path / "song.mp3"), "\ufeff\r\n")

    assert written == []
    assert list(tmp_path.glob("*.lrc")) == []


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_save_then_read_roundtrip(tmp_path: Path, newline: str) -> None:
    original = newline.join(["[00:01.00]第一行", "[00:02.345]second line"])
    translated = newline.join(["[00:01.00]译文一", "[00:02.345]译文二"])
    expected_original = original.replace("\r\n", "\n").replace("\r", "\n")
    expected_translated = translated.replace("\r\n", "\n").replace("\r", "\n")
    audio = tmp_path / "song.mp3"

    written = save_lyric(str(audio), original, translated)

    assert written == [str(tmp_path / "song.lrc"), str(tmp_path / "song.zh.lrc")]
    assert (tmp_path / "song.lrc").read_bytes() == (expected_original + "\n").encode(
        "utf-8"
    )
    assert (tmp_path / "song.zh.lrc").read_bytes() == (
        expected_translated + "\n"
    ).encode("utf-8")
    assert _read(tmp_path / "song.lrc") == expected_original
    assert _read(tmp_path / "song.zh.lrc") == expected_translated


def test_save_lyric_over_one_mib_writes_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    audio = tmp_path / "song.mp3"

    with caplog.at_level(logging.WARNING, logger=downloader.__name__):
        written = save_lyric(str(audio), "a" * MAX_LYRIC_BYTES)

    # ``_write_text`` appends a newline, pushing the body over the cap.
    assert written == []
    assert list(tmp_path.glob("*.lrc")) == []
    assert "歌词保存失败" in caplog.text


def test_save_lyric_lone_surrogate_is_skipped_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    audio = tmp_path / "song.mp3"

    with caplog.at_level(logging.WARNING, logger=downloader.__name__):
        written = save_lyric(str(audio), "[00:01.00]ok\ud800")

    assert written == []
    assert list(tmp_path.glob("*.lrc")) == []
    assert "歌词保存失败" in caplog.text


# ---------------------------------------------------------------------------
# Embedded lyrics: same inputs, corrupt containers
# ---------------------------------------------------------------------------


def test_embedded_lyric_id3_utf8_and_utf16_frames(tmp_path: Path) -> None:
    pytest.importorskip("mutagen")
    from mutagen.id3 import ID3, USLT

    audio = tmp_path / "song.mp3"
    audio.write_bytes(_mp3_bytes())
    for encoding in (3, 1):  # 3 = UTF-8, 1 = UTF-16 with BOM
        tags = ID3()
        tags.add(USLT(encoding=encoding, lang="eng", desc="Lyrics", text=_LYRIC))
        tags.save(str(audio), v2_version=3)

        assert downloader.embedded_lyric(str(audio)) == _LYRIC


def test_embedded_lyric_reads_txxx_lyrics_compat(tmp_path: Path) -> None:
    pytest.importorskip("mutagen")
    from mutagen.id3 import ID3, TXXX

    audio = tmp_path / "song.mp3"
    audio.write_bytes(_mp3_bytes())
    tags = ID3()
    tags.add(TXXX(encoding=3, desc="LYRICS", text=_LYRIC))
    tags.save(str(audio), v2_version=3)

    assert downloader.embedded_lyric(str(audio)) == _LYRIC


def test_write_embedded_lyric_preserves_timestamps_and_normalizes_bom(
    tmp_path: Path,
) -> None:
    pytest.importorskip("mutagen")
    audio = tmp_path / "song.mp3"
    audio.write_bytes(_mp3_bytes())

    assert downloader.write_embedded_lyric(str(audio), "\ufeff" + _TIMELINE)
    assert downloader.embedded_lyric(str(audio)) == _TIMELINE
    # Writing the same normalized text again is a no-op.
    assert downloader.write_embedded_lyric(str(audio), _TIMELINE) is False


@pytest.mark.parametrize(
    ("name", "data"),
    [
        pytest.param("bad.mp3", b"not an mp3 at all", id="mp3"),
        pytest.param("bad.flac", b"fLaC not really flac", id="flac"),
        pytest.param("bad.m4a", b"\x00\x00\x00\x08ftypM4A ", id="m4a"),
        pytest.param("bad.ogg", b"OggS not really ogg", id="unsupported"),
    ],
)
def test_embedded_lyric_corrupt_container_reads_empty(
    tmp_path: Path, name: str, data: bytes
) -> None:
    audio = tmp_path / name
    audio.write_bytes(data)

    assert downloader.embedded_lyric(str(audio)) == ""


def test_embedded_lyric_missing_and_directory_paths(tmp_path: Path) -> None:
    assert downloader.embedded_lyric(str(tmp_path / "missing.mp3")) == ""
    assert downloader.embedded_lyric(str(tmp_path)) == ""


def test_corrupt_flac_embedding_only_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    pytest.importorskip("mutagen")
    audio = tmp_path / "song.flac"
    audio.write_bytes(b"fLaC broken")
    before = audio.read_bytes()

    with caplog.at_level(logging.WARNING, logger=downloader.__name__):
        downloader._embed_lyric_or_warn(str(audio), "[00:01.00]x")

    assert "歌词写入标签失败" in caplog.text
    assert audio.read_bytes() == before


def test_oversized_embedded_lyric_only_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    pytest.importorskip("mutagen")
    audio = tmp_path / "song.mp3"
    audio.write_bytes(_mp3_bytes())
    before = audio.read_bytes()

    with caplog.at_level(logging.WARNING, logger=downloader.__name__):
        downloader._embed_lyric_or_warn(str(audio), "x" * (MAX_LYRIC_BYTES + 1))

    assert "歌词写入标签失败" in caplog.text
    assert audio.read_bytes() == before


# ---------------------------------------------------------------------------
# Corrupted sidecar must not fail the download flow
# ---------------------------------------------------------------------------


class _Provider:
    def __init__(self, lyric: tuple[str, str]) -> None:
        self._lyric = lyric

    def resolve(self, track: Track, level: str | None = None) -> ResolveResult:
        return ResolveResult(url="http://fake.test/audio.mp3", ext="mp3", level=level)

    def lyric(self, track: Track) -> tuple[str, str]:
        return self._lyric


def test_corrupt_sidecar_backfill_still_skips_and_embeds(tmp_path: Path) -> None:
    pytest.importorskip("mutagen")
    track = Track(provider="fake", id="1", name="Song", artists="Artist")
    audio = tmp_path / "Song - Artist.mp3"
    # Must exceed the 10 KiB skip threshold and carry a matching title, or
    # download_track would treat it as a collision and pick a new name.
    audio.write_bytes(_mp3_bytes(frames=30))
    downloader._write_tags(str(audio), track)
    sidecar = tmp_path / "Song - Artist.lrc"
    sidecar.write_bytes(b"\xff\xff\xff")
    opts = DownloadOptions(
        out_dir=str(tmp_path),
        split_folder=False,
        embed_tags=False,
        embed_cover=False,
        save_lyric=True,
        embed_lyric=True,
        skip_existing=True,
        retries=0,
    )

    result = downloader.download_track(_Provider(("[00:07.00]fresh", "")), track, opts)

    # The corrupt sidecar is not overwritten (existing lyrics stay untouched),
    # but it cannot be read, so the provider text is embedded instead. Most
    # importantly the track result is a skip, not a failure.
    assert result.status == "skip"
    assert sidecar.read_bytes() == b"\xff\xff\xff"
    assert downloader.embedded_lyric(str(audio)) == "[00:07.00]fresh"
