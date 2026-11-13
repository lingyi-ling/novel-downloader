package com.lingyi.noveldl.ui.music

import com.lingyi.noveldl.data.MusicJobResult
import com.lingyi.noveldl.data.MusicTrack
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Pure rules wiring a music download job back into the visible search
 * results: which results wrote a local path, how they are keyed, and what the
 * error retry button repeats. Per-page job splitting lives in
 * [MusicJobBatchingTest].
 */
class MusicJobResultsTest {

    private fun track(id: String, provider: String = "netease", path: String = "") =
        MusicTrack(provider = provider, id = id, name = "song-$id", path = path)

    private fun result(
        status: String,
        path: String = "",
        url: String = "",
        lyric: String? = null,
    ) = MusicJobResult(status = status, path = path, download_url = url, lyricPath = lyric)

    // ---------- per-page search ids ----------

    @Test
    fun rememberSearchIdsTagsEveryTrackOfThePage() {
        val ids = MusicJobResults.rememberSearchIds(
            emptyMap(),
            listOf(track("1"), track("2")),
            " sid-page-1 ",
        )

        assertEquals(
            mapOf(
                MusicJobResults.trackKey(track("1")) to "sid-page-1",
                MusicJobResults.trackKey(track("2")) to "sid-page-1",
            ),
            ids,
        )
    }

    @Test
    fun rememberSearchIdsKeepsEarlierPagesAndIgnoresBlankId() {
        val first = MusicJobResults.rememberSearchIds(emptyMap(), listOf(track("1")), "sid-1")

        val second = MusicJobResults.rememberSearchIds(first, listOf(track("2")), "sid-2")
        val blank = MusicJobResults.rememberSearchIds(second, listOf(track("3")), "  ")

        assertEquals("sid-1", blank[MusicJobResults.trackKey(track("1"))])
        assertEquals("sid-2", blank[MusicJobResults.trackKey(track("2"))])
        assertNull(blank[MusicJobResults.trackKey(track("3"))])
        assertSame(second, blank)
    }

    // ---------- path write-back ----------

    @Test
    fun pathsByTrackAlignsResultsWithTheSubmittedOrder() {
        val chosen = listOf(track("1"), track("2"), track("3"))

        val paths = MusicJobResults.pathsByTrack(
            chosen,
            listOf(
                result("ok", path = "/data/music/a.mp3"),
                result("fail", path = ""),
                result("ok", path = " /data/music/c.flac "),
            ),
        )

        assertEquals(
            mapOf(
                MusicJobResults.trackKey(track("1")) to "/data/music/a.mp3",
                MusicJobResults.trackKey(track("3")) to "/data/music/c.flac",
            ),
            paths,
        )
    }

    @Test
    fun pathsByTrackIgnoresSuccessfulResultsWithoutAPath() {
        val paths = MusicJobResults.pathsByTrack(
            listOf(track("1")),
            listOf(result("ok", path = "   ")),
        )

        assertEquals(emptyMap<String, String>(), paths)
    }

    @Test
    fun pathsByTrackToleratesShortResultLists() {
        // A job that failed early can report fewer results than submitted;
        // the missing tail must not shift paths onto other tracks.
        val chosen = listOf(track("1"), track("2"))

        val paths = MusicJobResults.pathsByTrack(chosen, listOf(result("ok", path = "/a")))

        assertEquals(mapOf(MusicJobResults.trackKey(track("1")) to "/a"), paths)
    }

    @Test
    fun applyPathsMatchesProviderAndIdNotIdAlone() {
        // Same numeric id on another platform must keep its own (empty) path.
        val tracks = listOf(
            track("1", provider = "netease"),
            track("1", provider = "qq"),
        )

        val updated = MusicJobResults.applyPaths(
            tracks,
            mapOf(MusicJobResults.trackKey(track("1", provider = "netease")) to "/a.mp3"),
        )

        assertEquals("/a.mp3", updated[0].path)
        assertEquals("", updated[1].path)
    }

    @Test
    fun applyPathsKeepsTheListWhenNothingMatches() {
        val tracks = listOf(track("1"), track("2"))

        assertSame(tracks, MusicJobResults.applyPaths(tracks, emptyMap()))
        assertSame(
            tracks,
            MusicJobResults.applyPaths(tracks, mapOf("unknown" to "/x.mp3")),
        )
    }

    @Test
    fun applyPathsKeepsAnAlreadyRecordedPathUntouched() {
        val tracks = listOf(track("1", path = "/old.mp3"))

        assertSame(
            tracks,
            MusicJobResults.applyPaths(
                tracks,
                mapOf(MusicJobResults.trackKey(track("1")) to "/old.mp3"),
            ),
        )
    }

    // ---------- lyric write-back ----------

    @Test
    fun lyricsByTrackAlignsResultsWithTheSubmittedOrder() {
        val chosen = listOf(track("1"), track("2"), track("3"))

        val lyrics = MusicJobResults.lyricsByTrack(
            chosen,
            listOf(
                result("ok", lyric = "/music/a.lrc"),
                result("fail", lyric = "/music/b.lrc"),
                result("skip", lyric = "/music/c.lrc"),
            ),
        )

        // A failed row never contributes, even when the engine echoed a lyric;
        // a skipped track keeps its lyric (the engine writes it on skip too).
        assertEquals(
            mapOf(
                MusicJobResults.trackKey(track("1")) to "/music/a.lrc",
                MusicJobResults.trackKey(track("3")) to "/music/c.lrc",
            ),
            lyrics,
        )
    }

    @Test
    fun lyricsByTrackIsEmptyWithoutLyrics() {
        val chosen = listOf(track("1"), track("2"))

        assertEquals(
            emptyMap<String, String>(),
            MusicJobResults.lyricsByTrack(
                chosen,
                listOf(result("ok"), result("ok", lyric = "   ")),
            ),
        )
        assertEquals(emptyMap<String, String>(), MusicJobResults.lyricsByTrack(emptyList(), emptyList()))
    }

    @Test
    fun lyricsByTrackToleratesShortResultLists() {
        // Missing results must not shift another track's lyric onto a row.
        val chosen = listOf(track("1"), track("2"))

        val lyrics = MusicJobResults.lyricsByTrack(
            chosen,
            listOf(result("ok", lyric = "/music/a.lrc")),
        )

        assertEquals(mapOf(MusicJobResults.trackKey(track("1")) to "/music/a.lrc"), lyrics)
    }

    // ---------- selection keys ----------

    @Test
    fun isTrackKeyAcceptsOnlyProviderSeparatorId() {
        assertTrue(MusicJobResults.isTrackKey("netease\u00001"))
        assertTrue(MusicJobResults.isTrackKey("qq\u0000abc-def"))

        // Bare ids are what older builds saved; they must not be reinterpreted.
        assertFalse(MusicJobResults.isTrackKey("1"))
        assertFalse(MusicJobResults.isTrackKey(""))
        assertFalse(MusicJobResults.isTrackKey("\u0000"))
        assertFalse(MusicJobResults.isTrackKey("\u00001"))
        assertFalse(MusicJobResults.isTrackKey("netease\u0000"))
        assertFalse(MusicJobResults.isTrackKey("netease\u00001\u0000qq"))
    }

    @Test
    fun sanitizeSelectionDropsBareIdsAndKeepsValidKeys() {
        val kept = MusicJobResults.sanitizeSelection(
            listOf("7", "netease\u00007", "", "qq\u00009", "netease\u00007"),
        )

        assertEquals(setOf("netease\u00007", "qq\u00009"), kept)
        assertTrue(MusicJobResults.sanitizeSelection(null).isEmpty())
    }

    @Test
    fun selectedTracksKeepsOrderAndTreatsProvidersAsDistinct() {
        val netease = track("1", provider = "netease")
        val qq = track("1", provider = "qq")
        val tracks = listOf(netease, qq, track("2", provider = "netease"))

        val chosen = MusicJobResults.selectedTracks(
            tracks,
            setOf(MusicJobResults.trackKey(qq), MusicJobResults.trackKey(track("2"))),
        )

        assertEquals(listOf(qq, track("2")), chosen)
        assertTrue(MusicJobResults.selectedTracks(tracks, emptySet()).isEmpty())
    }

    // ---------- completion counts / DownloadManager handoff ----------

    @Test
    fun countsGroupResultsByStatus() {
        val counts = MusicJobResults.counts(
            listOf(
                result("ok"),
                result("ok"),
                result("skip"),
                result("fail"),
                result("weird"),
            ),
        )

        assertEquals(2, counts.ok)
        assertEquals(1, counts.skipped)
        assertEquals(1, counts.failed)
    }

    @Test
    fun downloadableOnlyReturnsSuccessfulLoopbackUrls() {
        val results = listOf(
            result("ok", url = "/downloads/music/a.mp3"),
            result("ok", url = ""),
            result("skip", url = "/downloads/music/b.mp3"),
            result("fail", url = "/downloads/music/c.mp3"),
        )

        assertEquals(listOf("a.mp3"), MusicJobResults.downloadable(results).map { it.download_url.substringAfterLast('/') })
        // Remote mode never produces download urls: nothing to hand off.
        assertEquals(0, MusicJobResults.downloadable(listOf(result("ok"))).size)
    }

    @Test
    fun firstFailureReasonNamesTheEngineProblem() {
        val results = listOf(
            MusicJobResult(status = "ok"),
            MusicJobResult(status = "fail", reason = ""),
            MusicJobResult(status = "fail", reason = "搜索结果已过期，请重新搜索"),
            MusicJobResult(status = "fail", reason = "第二个原因"),
        )

        assertEquals(
            "搜索结果已过期，请重新搜索",
            MusicJobResults.firstFailureReason(results),
        )
        assertNull(MusicJobResults.firstFailureReason(listOf(result("ok"), result("skip"))))
    }

    // ---------- retry semantics ----------

    @Test
    fun loadMoreFailureRetriesThePageNotTheWholeSearch() {
        assertEquals(
            MusicRequest.LoadMore,
            MusicRetry.target(MusicRequest.LoadMore, "晴天"),
        )
    }

    @Test
    fun platformFailureReloadsThePlatformList() {
        assertEquals(
            MusicRequest.Platforms,
            MusicRetry.target(MusicRequest.Platforms, ""),
        )
    }

    @Test
    fun searchAndLoadMoreRetryAreHiddenWithoutAKeyword() {
        // Nothing to repeat: showing a dead retry button was a silent no-op.
        assertNull(MusicRetry.target(MusicRequest.Search, "   "))
        assertNull(MusicRetry.target(MusicRequest.LoadMore, ""))
        assertEquals(MusicRequest.Search, MusicRetry.target(MusicRequest.Search, "晴天"))
    }
}
