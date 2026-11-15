"""Boundary and concurrency tests for the music byte budgets.

Covers the two hard ceilings in ``apps.music.downloader``:

* ``MAX_TRACK_BYTES`` / ``opts.max_track_bytes`` -- one file, checked per
  chunk as it streams (the declared ``Content-Length`` is only an early
  rejection).
* ``MAX_BATCH_BYTES`` / ``opts.max_batch_bytes`` -- one ``download_many`` or
  ``download_mixed`` invocation, shared atomically by every worker.

The tests pin the accounting semantics, which are the contract callers rely
on:

* a successful download keeps its bytes charged for the rest of the batch;
* a failed, cancelled, or over-limit attempt releases everything it charged
  (those bytes are *not* counted against the batch, even though a resumable
  ``.part`` may stay on disk);
* skipping an already-downloaded track charges nothing;
* a per-track rejection never leaves bytes charged to the batch.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from novel_downloader.apps.music import downloader
from novel_downloader.apps.music.downloader import DownloadOptions
from novel_downloader.apps.music.providers.base import ResolveResult, Track

_URL = "http://fake.test/audio.mp3"


class _StreamResponse:
    """Minimal streaming ``requests.Response`` double."""

    def __init__(
        self,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        chunks: tuple[bytes, ...] = (b"data",),
    ) -> None:
        self.status_code = status_code
        self.headers = dict(headers or {})
        self._chunks = list(chunks)
        self.closed = False

    def iter_content(self, chunk_size: int | None = None):
        return iter(self._chunks)

    def close(self) -> None:
        self.closed = True


class _FailingResponse(_StreamResponse):
    """Yields a few bytes, then breaks the connection mid-body."""

    def __init__(self, first_chunk: bytes = b"b" * 200) -> None:
        super().__init__()
        self._first_chunk = first_chunk

    def iter_content(self, chunk_size: int | None = None):
        yield self._first_chunk
        raise OSError("connection reset")


class _UrlProvider:
    def resolve(self, track: Track, level: str | None = None) -> ResolveResult:
        return ResolveResult(url=_URL, ext="mp3", level=level)

    def lyric(self, track: Track) -> tuple[str, str]:
        return "", ""


def _install_fake_get(
    monkeypatch: pytest.MonkeyPatch, responses: list[_StreamResponse]
) -> None:
    queue = list(responses)

    def fake_get(url: str, **kwargs: object) -> _StreamResponse:
        assert queue, f"unexpected request: {url}"
        return queue.pop(0)

    monkeypatch.setattr(downloader.requests, "get", fake_get)


def _opts(tmp_path: Path, **overrides: object) -> DownloadOptions:
    values: dict[str, object] = {
        "out_dir": str(tmp_path),
        "split_folder": False,
        "embed_tags": False,
        "embed_cover": False,
        "save_lyric": False,
        "skip_existing": False,
        "retries": 0,
    }
    values.update(overrides)
    return DownloadOptions(**values)  # type: ignore[arg-type]


def _track(track_id: str, name: str) -> Track:
    return Track(
        provider="fake", id=track_id, name=name, artists="Artist", album="Album"
    )


def _audio(length: int, headers: dict[str, str] | None = None) -> _StreamResponse:
    base = {"Content-Type": "audio/mpeg"}
    base.update(headers or {})
    return _StreamResponse(headers=base, chunks=(b"a" * length,))


# ---------------------------------------------------------------------------
# limit boundaries: exactly at the ceiling is accepted, one byte over is not
# ---------------------------------------------------------------------------


def test_budget_constants_match_documented_ceilings() -> None:
    # The boundary tests below scale the limit down to a few KiB: the same
    # ``used + amount > limit`` comparison is exercised without writing a
    # real gigabyte to disk.
    assert downloader.MAX_TRACK_BYTES == 1024**3  # 1 GiB per track
    assert downloader.MAX_BATCH_BYTES == 8 * 1024**3  # 8 GiB per batch


def test_track_limit_exact_size_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = b"a" * 1024
    _install_fake_get(
        monkeypatch,
        [_audio(1024, {"Content-Length": "1024"})],
    )
    dest = tmp_path / "song.mp3"
    budget = downloader._ByteBudget(10_000)

    written, resumed = downloader._download_file(
        _URL, str(dest), max_bytes=1024, budget=budget
    )

    assert (written, resumed) == (1024, 0)
    assert dest.read_bytes() == payload
    assert budget.used == 1024


def test_track_limit_one_byte_over_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No Content-Length on purpose: the streaming guard, not the early
    # declared-size rejection, has to catch this.
    response = _StreamResponse(
        headers={"Content-Type": "audio/mpeg"},
        chunks=(b"a" * 512, b"a" * 512, b"a"),
    )
    _install_fake_get(monkeypatch, [response])
    dest = tmp_path / "song.mp3"
    budget = downloader._ByteBudget(10_000)

    with pytest.raises(downloader._SizeBudgetExceeded, match="单曲超过大小上限"):
        downloader._download_file(_URL, str(dest), max_bytes=1024, budget=budget)

    assert not dest.exists()
    assert not (tmp_path / "song.mp3.part").exists()
    # The 1024 bytes written before the offending chunk were charged and must
    # be returned: a rejected track occupies no batch budget.
    assert budget.used == 0


def test_batch_limit_exact_size_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_get(
        monkeypatch,
        [
            _audio(512, {"Content-Length": "512"}),
            _audio(512, {"Content-Length": "512"}),
        ],
    )
    tracks = [_track("1", "One"), _track("2", "Two")]
    opts = _opts(tmp_path, max_batch_bytes=1024)
    budget = downloader._ByteBudget(1024)

    ok, skip, fail, pairs, failed = downloader.download_many(
        _UrlProvider(), tracks, opts, workers=1, budget=budget
    )

    assert (ok, skip, fail) == (2, 0, 0)
    assert pairs == [] and failed == []
    assert budget.used == 1024


def test_batch_limit_one_byte_over_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_get(
        monkeypatch,
        [
            _audio(512, {"Content-Length": "512"}),
            _audio(513, {"Content-Length": "513"}),
        ],
    )
    tracks = [_track("1", "One"), _track("2", "Two")]
    opts = _opts(tmp_path, max_batch_bytes=1024)
    budget = downloader._ByteBudget(1024)

    ok, skip, fail, pairs, failed = downloader.download_many(
        _UrlProvider(), tracks, opts, workers=1, budget=budget
    )

    assert (ok, skip, fail) == (1, 0, 1)
    assert failed == [tracks[1]]
    assert "整批下载超过大小上限" in pairs[0][1]
    assert (tmp_path / "One - Artist.mp3").is_file()
    assert not (tmp_path / "Two - Artist.mp3").exists()
    assert list(tmp_path.glob("*.part")) == []
    # Only the kept file is charged.
    assert budget.used == 512


# ---------------------------------------------------------------------------
# atomicity under concurrency
# ---------------------------------------------------------------------------


def test_byte_budget_reservations_are_atomic_under_asyncio_gather() -> None:
    """``asyncio.gather`` over concurrent workers must not over-reserve.

    Each coroutine offloads ``try_consume`` to a thread, so the reservations
    really do race instead of merely interleaving on one event loop.
    """
    budget = downloader._ByteBudget(100)

    async def attempt() -> bool:
        await asyncio.sleep(0)  # let every coroutine reach the barrier first
        return await asyncio.to_thread(budget.try_consume, 7)

    async def run() -> list[bool]:
        return await asyncio.gather(*(attempt() for _ in range(40)))

    results = asyncio.run(run())

    granted = results.count(True)
    assert granted == 100 // 7  # 14 x 7 = 98; the last two bytes are too few
    assert budget.used == granted * 7
    assert budget.used <= budget.limit


def test_byte_budget_release_after_concurrent_reservations() -> None:
    budget = downloader._ByteBudget(50)

    async def reserve(chunk: int) -> bool:
        await asyncio.sleep(0)
        return await asyncio.to_thread(budget.try_consume, chunk)

    async def run() -> list[bool]:
        return await asyncio.gather(*(reserve(20) for _ in range(5)))

    results = asyncio.run(run())
    assert results.count(True) == 2
    assert budget.used == 40

    budget.release(40)
    assert budget.used == 0
    # Released bytes are immediately usable again.
    assert budget.try_consume(50) is True
    assert budget.used == 50


# ---------------------------------------------------------------------------
# release semantics: failed / cancelled attempts are not counted
# ---------------------------------------------------------------------------


def test_failed_attempt_is_refunded_while_successful_bytes_stay_charged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_get(
        monkeypatch,
        [
            _audio(400, {"Content-Length": "400"}),
            _FailingResponse(b"b" * 200),
        ],
    )
    tracks = [_track("1", "One"), _track("2", "Two")]
    opts = _opts(tmp_path, max_batch_bytes=10_000)
    budget = downloader._ByteBudget(10_000)

    ok, skip, fail, pairs, failed = downloader.download_many(
        _UrlProvider(), tracks, opts, workers=1, budget=budget
    )

    assert (ok, skip, fail) == (1, 0, 1)
    assert failed == [tracks[1]]
    assert (tmp_path / "One - Artist.mp3").is_file()
    # A retryable failure keeps its .part for resume, but the bytes must not
    # stay in the batch counter.
    part = tmp_path / "Two - Artist.mp3.part"
    assert part.stat().st_size == 200
    assert budget.used == 400


def test_short_body_failure_releases_budget_and_keeps_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clean EOF before the declared size is a failure, not a charge."""
    _install_fake_get(monkeypatch, [_audio(300, {"Content-Length": "500"})])
    budget = downloader._ByteBudget(10_000)
    dest = tmp_path / "song.mp3"

    with pytest.raises(OSError, match="大小不符"):
        downloader._download_file(_URL, str(dest), max_bytes=10_000, budget=budget)

    part = tmp_path / "song.mp3.part"
    assert part.stat().st_size == 300
    assert not dest.exists()
    # The attempt failed and kept its .part for resume, so it must not hold
    # the batch budget.
    assert budget.used == 0


def test_rename_failure_releases_budget_and_keeps_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_get(monkeypatch, [_audio(300, {"Content-Length": "300"})])
    budget = downloader._ByteBudget(10_000)

    def fail_replace(src: object, dst: object) -> None:
        raise OSError("rename denied")

    monkeypatch.setattr(downloader.os, "replace", fail_replace)

    with pytest.raises(OSError, match="rename denied"):
        downloader._download_file(
            _URL, str(tmp_path / "song.mp3"), max_bytes=10_000, budget=budget
        )

    assert (tmp_path / "song.mp3.part").stat().st_size == 300
    assert not (tmp_path / "song.mp3").exists()
    assert budget.used == 0


def test_cancelled_download_refunds_budget_and_keeps_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    response = _StreamResponse(
        headers={"Content-Type": "audio/mpeg"},
        chunks=(b"a" * 100, b"b" * 100),
    )
    _install_fake_get(monkeypatch, [response])
    budget = downloader._ByteBudget(10_000)
    checks = {"n": 0}

    def stop_flag() -> bool:
        checks["n"] += 1
        return checks["n"] > 1  # stop before the second chunk

    track = _track("1", "Song")
    with pytest.raises(InterruptedError):
        downloader.download_track(
            _UrlProvider(),
            track,
            _opts(tmp_path),
            stop_flag=stop_flag,
            budget=budget,
        )

    part = tmp_path / "Song - Artist.mp3.part"
    assert part.exists() and part.stat().st_size == 100
    assert budget.used == 0


# ---------------------------------------------------------------------------
# over-limit cleanup and per-track / per-batch independence
# ---------------------------------------------------------------------------


def test_batch_limit_deletes_half_written_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_get(
        monkeypatch,
        [
            _audio(600, {"Content-Length": "600"}),
            # 400 bytes fit (600 + 400 = 1000), the second 400 does not, so a
            # real partial exists on disk when the limit trips.
            _StreamResponse(
                headers={"Content-Type": "audio/mpeg"},
                chunks=(b"b" * 400, b"b" * 400),
            ),
        ],
    )
    budget = downloader._ByteBudget(1000)

    downloader._download_file(
        _URL, str(tmp_path / "one.mp3"), max_bytes=10_000, budget=budget
    )
    with pytest.raises(downloader._SizeBudgetExceeded, match="整批下载超过大小上限"):
        downloader._download_file(
            _URL, str(tmp_path / "two.mp3"), max_bytes=10_000, budget=budget
        )

    assert (tmp_path / "one.mp3").is_file()
    assert not (tmp_path / "two.mp3").exists()
    assert not (tmp_path / "two.mp3.part").exists()
    # Only the kept file remains charged.
    assert budget.used == 600


def test_track_limit_failure_does_not_consume_batch_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_get(
        monkeypatch,
        [
            _StreamResponse(
                headers={"Content-Type": "audio/mpeg"},
                chunks=(b"a" * 300, b"a" * 300),
            ),
            _audio(200, {"Content-Length": "200"}),
        ],
    )
    tracks = [_track("1", "TooBig"), _track("2", "Fits")]
    opts = _opts(tmp_path, max_track_bytes=400, max_batch_bytes=1000)
    budget = downloader._ByteBudget(1000)

    ok, skip, fail, pairs, failed = downloader.download_many(
        _UrlProvider(), tracks, opts, workers=1, budget=budget
    )

    assert (ok, skip, fail) == (1, 0, 1)
    assert "单曲超过大小上限" in pairs[0][1]
    assert not (tmp_path / "TooBig - Artist.mp3").exists()
    assert (tmp_path / "Fits - Artist.mp3").is_file()
    # The rejected track wrote 300 bytes before tripping its own ceiling;
    # those must be refunded so the batch counter only reflects "Fits".
    assert budget.used == 200


def test_oversized_resumed_part_is_deleted_without_charging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    part = tmp_path / "song.mp3.part"
    part.write_bytes(b"A" * 400)

    def never_called(*args: object, **kwargs: object) -> None:
        pytest.fail("an oversized resume must be rejected before any request")

    monkeypatch.setattr(downloader.requests, "get", never_called)
    budget = downloader._ByteBudget(10_000)

    with pytest.raises(downloader._SizeBudgetExceeded, match="单曲超过大小上限"):
        downloader._download_file(
            _URL, str(tmp_path / "song.mp3"), max_bytes=300, budget=budget
        )

    assert not part.exists()
    assert budget.used == 0


# ---------------------------------------------------------------------------
# degenerate amounts and non-integer configuration
# ---------------------------------------------------------------------------


def test_byte_budget_zero_and_negative_amounts_are_noops() -> None:
    budget = downloader._ByteBudget(10)

    # A non-positive amount can never exhaust the budget, so it is granted
    # without touching the counter.
    assert budget.try_consume(0) is True
    assert budget.try_consume(-5) is True
    assert budget.used == 0

    # Releasing more than was used clamps at zero instead of going negative.
    assert budget.release(5) is None
    assert budget.used == 0


def test_byte_budget_rejects_unusable_amounts() -> None:
    budget = downloader._ByteBudget(10)

    assert budget.try_consume("not-a-number") is False
    assert budget.try_consume(None) is False
    assert budget.used == 0

    # ``int()`` coerces; a float amount is truncated rather than rounded.
    # Production callers always pass ``len(chunk)``, so this only documents
    # the internal helper's behaviour.
    assert budget.try_consume(3.9) is True
    assert budget.used == 3

    budget.release("x")
    budget.release(-1)
    assert budget.used == 3


@pytest.mark.parametrize("field", ["max_track_bytes", "max_batch_bytes"])
@pytest.mark.parametrize("value", [0, -1, 1.5, "1024", True])
def test_download_options_reject_non_positive_or_non_integer_limits(
    tmp_path: Path, field: str, value: object
) -> None:
    with pytest.raises(ValueError, match="必须是正整数"):
        _opts(tmp_path, **{field: value})


@pytest.mark.parametrize("limit", [0, -1, "not-a-number", None])
def test_byte_budget_constructor_rejects_invalid_limits(limit: object) -> None:
    with pytest.raises(ValueError, match="必须是正整数"):
        downloader._ByteBudget(limit)  # type: ignore[arg-type]


def test_byte_budget_constructor_coerces_int_like_limits() -> None:
    # Unlike ``_positive_limit`` (which guards the public options and rejects
    # bool/float/str), the internal budget class funnels through ``int()``.
    # Production only ever constructs it from already-validated ints, so this
    # documents the helper rather than a supported input path.
    assert downloader._ByteBudget("1024").limit == 1024
    assert downloader._ByteBudget(1.9).limit == 1  # type: ignore[arg-type]
