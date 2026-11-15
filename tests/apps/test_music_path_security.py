"""Confinement tests for the music download engine.

Every component of a written path (title, artist, album, provider extension,
track id) is attacker-influenced: it comes from a platform API response, a
search result, or a serialized track.  ``export_path`` already guards the
caller-supplied ``out_dir`` / playlist ``out_path``; this module covers the
engine side, which builds the per-track paths inside that directory.

The invariants pinned here:

* hostile metadata (separators, ``..``, drive prefixes, NUL/control bytes,
  Windows device names) never moves the audio or its ``.lrc`` sidecars out of
  ``out_dir``;
* ordinary Unicode is not over-sanitized;
* over-long names still produce a valid single path component;
* a duplicate stem never overwrites an unrelated pre-existing file;
* a symlinked directory cannot redirect the write outside ``out_dir`` (skipped
  where the OS refuses to create symlinks);
* a relative ``out_dir`` is resolved against the process directory, not the
  track metadata.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from novel_downloader.apps.music import downloader
from novel_downloader.apps.music.downloader import DownloadOptions, save_lyric
from novel_downloader.apps.music.providers.base import ResolveResult, Track

_AUDIO = b"AUDIO" * 64
_UNRELATED = b"not an audio file, do not replace me"

_WIN_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

_HOSTILE_VALUES = [
    pytest.param("../../outside", id="posix-parent"),
    pytest.param("..\\..\\outside", id="windows-parent"),
    pytest.param("/etc/passwd", id="posix-absolute"),
    pytest.param("C:\\Windows\\evil", id="windows-absolute"),
    pytest.param("..", id="dotdot"),
    pytest.param("....", id="dots"),
    pytest.param('a:b*c?d"e<f>g|h', id="windows-illegal"),
    pytest.param("bad\x00name\x01\x1f", id="nul-and-controls"),
]


class _Provider:
    """Offline provider: fixed direct link + fixed lyric text."""

    def __init__(self, ext: str = "mp3") -> None:
        self._ext = ext

    def resolve(self, track: Track, level: str | None = None) -> ResolveResult:
        return ResolveResult(url="http://fake.test/audio", ext=self._ext, level=level)

    def lyric(self, track: Track) -> tuple[str, str]:
        return "[00:01.00]Original", "[00:01.00]译文"


@pytest.fixture
def quiet_download(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the network download and tag writer with local fakes."""

    def fake_download(url, dest, **kwargs):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        Path(dest).write_bytes(_AUDIO)
        return len(_AUDIO), 0

    monkeypatch.setattr(downloader, "_download_file", fake_download)
    monkeypatch.setattr(downloader, "_write_tags", lambda *args, **kwargs: None)


def _opts(root: str | Path, **overrides) -> DownloadOptions:
    values = {
        "out_dir": str(root),
        "split_folder": False,
        "embed_tags": False,
        "embed_cover": False,
        "save_lyric": False,
        "skip_existing": False,
        "retries": 0,
    }
    values.update(overrides)
    return DownloadOptions(**values)


def _assert_confined(path: str | Path, root: Path) -> None:
    assert Path(path).resolve().is_relative_to(root.resolve())


def _device_name(component: str) -> str:
    return component.split(".", 1)[0].strip(" .").upper()


def _make_dir_link(link: Path, target: Path) -> None:
    """Create a directory symlink, or a Windows junction when symlinks are
    unavailable (no Developer Mode): both are followed by ``resolve()``."""
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            return
    pytest.skip("symlink creation is not permitted on this platform")


@pytest.mark.parametrize("field", ["name", "artists", "album"])
@pytest.mark.parametrize("value", _HOSTILE_VALUES)
def test_hostile_metadata_never_escapes_out_dir(
    tmp_path: Path,
    quiet_download: None,
    field: str,
    value: str,
) -> None:
    payload = {"name": "Song", "artists": "Artist", "album": "Album"}
    payload[field] = value
    track = Track(provider="fake", id="1", **payload)

    result = downloader.download_track(
        _Provider(),
        track,
        _opts(tmp_path, split_folder=True, save_lyric=True),
    )

    assert result.status == "ok", result.reason
    _assert_confined(result.path, tmp_path)
    audio = Path(result.path)
    assert audio.is_file()
    assert audio.read_bytes() == _AUDIO

    # Both sidecars follow the audio and stay under the same root.
    sidecars = list(tmp_path.rglob("*.lrc"))
    assert sidecars
    for sidecar in sidecars:
        _assert_confined(sidecar, tmp_path)
        assert sidecar.parent == audio.parent

    # No path component may resolve to the parent or an absolute drive root.
    for part in audio.relative_to(tmp_path).parts:
        assert part not in {"", ".", ".."}
        assert not Path(part).is_absolute()


@pytest.mark.parametrize("reserved", sorted(_WIN_RESERVED))
def test_windows_device_names_are_neutralised(
    tmp_path: Path,
    quiet_download: None,
    reserved: str,
) -> None:
    track = Track(
        provider="fake",
        id="1",
        name=reserved,
        artists=reserved,
        album=reserved,
    )

    result = downloader.download_track(
        _Provider(), track, _opts(tmp_path, split_folder=True)
    )

    assert result.status == "ok", result.reason
    _assert_confined(result.path, tmp_path)
    if os.name == "nt":
        for part in Path(result.path).relative_to(tmp_path).parts:
            assert _device_name(part) not in _WIN_RESERVED
        assert Path(result.path).name.startswith(f"_{reserved}")


@pytest.mark.parametrize("field", ["name", "artists"])
def test_overlong_utf8_metadata_stays_one_valid_component(
    tmp_path: Path,
    quiet_download: None,
    field: str,
) -> None:
    # 400 CJK chars = 1200 UTF-8 bytes per component, well past NAME_MAX.
    long_value = "長" * 400
    payload = {"name": "Song", "artists": "Artist"}
    payload[field] = long_value
    track = Track(provider="fake", id="1", album="Album", **payload)

    result = downloader.download_track(
        _Provider(), track, _opts(tmp_path, save_lyric=True)
    )

    assert result.status == "ok", result.reason
    _assert_confined(result.path, tmp_path)
    name = Path(result.path).name
    assert len(name.encode("utf-8")) <= 255
    assert name.endswith(".mp3")
    assert Path(result.path).is_file()


def test_overlong_artist_and_album_folders_fit_component_limit(
    tmp_path: Path,
) -> None:
    track = Track(
        provider="fake",
        id="1",
        name="Song",
        artists="歌" * 400,
        album="專" * 400,
    )

    folder = downloader.build_dir(_opts(tmp_path, split_folder=True), track)

    _assert_confined(folder, tmp_path)
    for part in Path(folder).relative_to(tmp_path).parts:
        assert len(part.encode("utf-8")) <= 255


def test_ordinary_unicode_is_preserved(tmp_path: Path, quiet_download: None) -> None:
    track = Track(
        provider="fake",
        id="1",
        name="晴天 🎵",
        artists="周杰倫",
        album="葉惠美",
    )

    result = downloader.download_track(
        _Provider(), track, _opts(tmp_path, split_folder=True)
    )

    assert result.status == "ok", result.reason
    relative = Path(result.path).relative_to(tmp_path)
    assert "晴天 🎵" in relative.parts[-1]
    assert "周杰倫" in relative.parts[-1]
    assert "葉惠美" in relative.parts
    assert "周杰倫" in relative.parts


def test_same_stem_does_not_replace_unrelated_existing_file(
    tmp_path: Path, quiet_download: None
) -> None:
    existing = tmp_path / "Song - Artist.mp3"
    existing.write_bytes(_UNRELATED)

    first = downloader.download_track(
        _Provider(),
        Track(provider="fake", id="1", name="Song", artists="Artist", album="Album"),
        _opts(tmp_path),
    )
    second = downloader.download_track(
        _Provider(),
        Track(provider="fake", id="2", name="Song", artists="Artist", album="Album"),
        _opts(tmp_path),
    )

    assert first.status == second.status == "ok"
    # The unrelated file is untouched; both tracks got id-suffixed names.
    assert existing.read_bytes() == _UNRELATED
    assert Path(first.path) != existing
    assert Path(second.path) != existing
    assert Path(first.path) != Path(second.path)
    for result in (first, second):
        _assert_confined(result.path, tmp_path)
        assert Path(result.path).is_file()


def test_hostile_track_id_in_dedupe_suffix_stays_confined(
    tmp_path: Path, quiet_download: None
) -> None:
    existing = tmp_path / "Song - Artist.mp3"
    existing.write_bytes(_UNRELATED)

    track = Track(
        provider="fake",
        id="../../../../evil",
        name="Song",
        artists="Artist",
        album="Album",
    )

    result = downloader.download_track(_Provider(), track, _opts(tmp_path))

    assert result.status == "ok", result.reason
    _assert_confined(result.path, tmp_path)
    assert existing.read_bytes() == _UNRELATED
    assert Path(result.path).is_file()


def test_save_lyric_sidecars_stay_beside_the_audio(tmp_path: Path) -> None:
    audio = tmp_path / "album" / "Song - Artist.mp3"
    audio.parent.mkdir()
    audio.write_bytes(_AUDIO)

    written = save_lyric(str(audio), "[00:01.00]Original", "[00:01.00]译文")

    assert {Path(item).name for item in written} == {
        "Song - Artist.lrc",
        "Song - Artist.zh.lrc",
    }
    for item in written:
        _assert_confined(item, tmp_path)
        assert Path(item).parent == audio.parent


def test_save_lyric_never_replaces_audio_named_lrc(tmp_path: Path) -> None:
    # ``lyric_paths`` strips the last suffix: for an audio path that already
    # ends in ``.lrc`` the sidecar target *is* the audio file.
    audio = tmp_path / "Song - Artist.lrc"
    audio.write_bytes(_AUDIO)

    written = save_lyric(str(audio), "[00:01.00]would destroy audio")

    assert written == []
    assert audio.read_bytes() == _AUDIO


def test_overlong_provider_extension_is_capped(
    tmp_path: Path, quiet_download: None
) -> None:
    result = downloader.download_track(
        _Provider(ext="x" * 300),
        Track(provider="fake", id="1", name="Song", artists="Artist", album="Album"),
        _opts(tmp_path),
    )

    assert result.status == "ok", result.reason
    _assert_confined(result.path, tmp_path)
    name = Path(result.path).name
    assert name.endswith(".xxxxxxxxxx")
    assert len(name.encode("utf-8")) <= 255


def test_provider_lrc_extension_cannot_overwrite_downloaded_audio(
    tmp_path: Path, quiet_download: None
) -> None:
    result = downloader.download_track(
        _Provider(ext="lrc"),
        Track(provider="fake", id="1", name="Song", artists="Artist", album="Album"),
        _opts(tmp_path, save_lyric=True),
    )

    assert result.status == "ok", result.reason
    audio = Path(result.path)
    assert audio.suffix.lower() == ".mp3"
    assert audio.read_bytes() == _AUDIO
    sidecar = audio.with_suffix(".lrc")
    assert sidecar.read_text(encoding="utf-8") == "[00:01.00]Original\n"


def test_embed_lyric_uses_the_confined_audio_path(
    tmp_path: Path,
    quiet_download: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        downloader,
        "_embed_lyric_or_warn",
        lambda path, text: seen.append((path, text)),
    )

    result = downloader.download_track(
        _Provider(),
        Track(provider="fake", id="1", name="./../Song", artists="Artist", album="A"),
        _opts(tmp_path, save_lyric=True, embed_lyric=True),
    )

    assert result.status == "ok", result.reason
    assert seen, "embedded lyric writer was never called"
    embedded_path, text = seen[0]
    assert text == "[00:01.00]Original"
    assert Path(embedded_path) == Path(result.path)
    _assert_confined(embedded_path, tmp_path)


def test_relative_out_dir_is_resolved_against_cwd(
    tmp_path: Path,
    quiet_download: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    track = Track(provider="fake", id="1", name="Song", artists="Artist", album="Album")

    result = downloader.download_track(_Provider(), track, _opts("library"))

    assert result.status == "ok", result.reason
    assert not Path(result.path).is_absolute()
    _assert_confined(result.path, tmp_path / "library")
    assert (tmp_path / "library" / "Song - Artist.mp3").is_file()


def test_symlinked_out_dir_is_still_the_write_root(
    tmp_path: Path, quiet_download: None
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    _make_dir_link(link, real)

    result = downloader.download_track(
        _Provider(),
        Track(provider="fake", id="1", name="Song", artists="Artist", album="Album"),
        _opts(link),
    )

    assert result.status == "ok", result.reason
    _assert_confined(result.path, real)
    assert (real / "Song - Artist.mp3").is_file()


def test_symlinked_artist_folder_cannot_redirect_the_write(
    tmp_path: Path, quiet_download: None
) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    _make_dir_link(out_dir / "Artist", outside)

    result = downloader.download_track(
        _Provider(),
        Track(provider="fake", id="1", name="Song", artists="Artist", album="Album"),
        _opts(out_dir, split_folder=True, save_lyric=True),
    )

    assert result.status == "fail"
    assert "越界" in result.reason
    assert [path for path in outside.rglob("*") if path.is_file()] == []
    assert [path for path in out_dir.rglob("*") if path.is_file()] == []


def test_symlinked_destination_file_cannot_redirect_the_write(
    tmp_path: Path, quiet_download: None
) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    target = tmp_path / "outside.txt"
    target.write_bytes(_UNRELATED)
    try:
        (out_dir / "Song - Artist.mp3").symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not permitted on this platform")

    result = downloader.download_track(
        _Provider(),
        Track(provider="fake", id="1", name="Song", artists="Artist", album="Album"),
        _opts(out_dir),
    )

    assert result.status == "fail"
    assert target.read_bytes() == _UNRELATED
    leftover = [
        path
        for path in tmp_path.rglob("*")
        if path.is_file() and not path.is_symlink() and path != target
    ]
    assert leftover == []


def test_confinement_helper_rejects_parent_paths(tmp_path: Path) -> None:
    root = tmp_path / "out"
    root.mkdir()

    assert downloader._escapes_out_dir(root, root / ".." / "evil.mp3")
    assert downloader._escapes_out_dir(root, tmp_path.parent / "elsewhere" / "evil.mp3")
    assert not downloader._escapes_out_dir(root, root / "Album" / "ok.mp3")
    assert not downloader._escapes_out_dir(root, root)
