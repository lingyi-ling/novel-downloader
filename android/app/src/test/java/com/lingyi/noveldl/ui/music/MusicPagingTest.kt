package com.lingyi.noveldl.ui.music

import com.lingyi.noveldl.data.MusicTrack
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Pure "load more" paging rules: append-with-dedup by `provider + id` and the
 * stop/continue decision for the next page request.
 */
class MusicPagingTest {

    private fun track(id: String, provider: String = "netease") =
        MusicTrack(provider = provider, id = id, name = "song-$id")

    // ---------- merge: append without duplicates ----------

    @Test
    fun mergeAppendsNewTracksInOrder() {
        val current = listOf(track("1"), track("2"))
        val page = listOf(track("3"), track("4"))

        val merged = MusicPaging.merge(current, page)

        assertEquals(listOf("1", "2", "3", "4"), merged.map { it.id })
    }

    @Test
    fun mergeDropsTracksAlreadyLoadedById() {
        // The next page overlaps the tail of the current list (id "3" twice,
        // id "2" from the head); neither may be appended twice.
        val current = listOf(track("1"), track("2"), track("3"))
        val page = listOf(track("3"), track("2"), track("5"), track("3"))

        val merged = MusicPaging.merge(current, page)

        assertEquals(listOf("1", "2", "3", "5"), merged.map { it.id })
    }

    @Test
    fun mergeKeepsSameNumericIdFromDifferentProviders() {
        // An aggregate search returns the same song id from two platforms.
        // They are different rows (different provider, source label and
        // download request), so neither may be dropped as a duplicate.
        val netease = track("100", provider = "netease")
        val qq = track("100", provider = "qq")

        val merged = MusicPaging.merge(listOf(netease), listOf(qq))

        assertEquals(2, merged.size)
        assertEquals(listOf("netease", "qq"), merged.map { it.provider })
        assertEquals(listOf("100", "100"), merged.map { it.id })
    }

    @Test
    fun mergeOnlyDedupesTheSameProviderAndIdPair() {
        val current = listOf(track("1", provider = "netease"), track("2", provider = "qq"))
        val page = listOf(
            // Exact repeats: dropped.
            track("1", provider = "netease"),
            track("2", provider = "qq"),
            // Same ids under the other provider: kept.
            track("2", provider = "netease"),
            track("1", provider = "qq"),
        )

        val merged = MusicPaging.merge(current, page)

        assertEquals(
            listOf("netease" to "1", "qq" to "2", "netease" to "2", "qq" to "1"),
            merged.map { it.provider to it.id },
        )
    }

    @Test
    fun mergeWithEmptyPageKeepsCurrentList() {
        val current = listOf(track("1"))

        assertSame(current, MusicPaging.merge(current, emptyList()))
    }

    @Test
    fun mergeWithFullyDuplicatedPageAddsNothing() {
        // A server that ignores `offset` repeats the first page verbatim.
        val current = listOf(track("1"), track("2"))
        val page = listOf(track("1"), track("2"))

        val merged = MusicPaging.merge(current, page)

        assertEquals(current, merged)
        assertEquals(current.size, merged.size)
    }

    // ---------- hasMore: stop / continue decision ----------

    @Test
    fun hasMoreContinuesWhileLoadedBelowTotalOnFullPages() {
        assertTrue(
            MusicPaging.hasMore(
                loaded = 40,
                total = 100,
                pageReceived = MUSIC_PAGE_SIZE,
                previousLoaded = 20,
            ),
        )
    }

    @Test
    fun hasMoreStopsWhenOffsetReachesTotal() {
        assertFalse(
            MusicPaging.hasMore(
                loaded = 45,
                total = 45,
                pageReceived = MUSIC_PAGE_SIZE,
                previousLoaded = 25,
            ),
        )
        // Overshooting (dedup collapsed the last page) also stops.
        assertFalse(
            MusicPaging.hasMore(
                loaded = 46,
                total = 45,
                pageReceived = MUSIC_PAGE_SIZE,
                previousLoaded = 26,
            ),
        )
    }

    @Test
    fun hasMoreStopsOnEmptyPage() {
        // Platforms without offset support (musicdl backend) return an empty
        // page for any non-zero offset.
        assertFalse(
            MusicPaging.hasMore(loaded = 30, total = 30, pageReceived = 0, previousLoaded = 30),
        )
    }

    @Test
    fun hasMoreStopsOnShortPage() {
        assertFalse(
            MusicPaging.hasMore(
                loaded = 42,
                total = 0,
                pageReceived = MUSIC_PAGE_SIZE - 1,
                previousLoaded = 22,
            ),
        )
    }

    @Test
    fun hasMoreStopsWhenPageAddedNothingNew() {
        // Server ignores `offset`: same first page again, merged size unchanged.
        assertFalse(
            MusicPaging.hasMore(
                loaded = 20,
                total = 100,
                pageReceived = MUSIC_PAGE_SIZE,
                previousLoaded = 20,
            ),
        )
    }

    @Test
    fun hasMoreStopsWhenTheProviderCannotPage() {
        // supports_offset = false (musicdl platforms, aggregate searches):
        // a non-zero offset silently returns the first page or nothing, so a
        // full, growing page must not unlock the next request.
        assertFalse(
            MusicPaging.hasMore(
                loaded = 20,
                total = 0,
                pageReceived = MUSIC_PAGE_SIZE,
                previousLoaded = 0,
                supportsOffset = false,
            ),
        )
        assertFalse(
            MusicPaging.hasMore(
                loaded = 40,
                total = 100,
                pageReceived = MUSIC_PAGE_SIZE,
                previousLoaded = 20,
                supportsOffset = false,
            ),
        )
    }

    @Test
    fun unknownOrTrueOffsetSupportKeepsTheLegacyRule() {
        // Older servers omit the flag: keep paging exactly as before.
        assertTrue(MusicPaging.hasMore(40, 0, MUSIC_PAGE_SIZE, 20, supportsOffset = null))
        assertTrue(MusicPaging.hasMore(40, 0, MUSIC_PAGE_SIZE, 20, supportsOffset = true))
        // An explicit `true` does not bypass the short-page rule.
        assertFalse(MusicPaging.hasMore(45, 0, 5, 40, supportsOffset = true))
    }

    @Test
    fun hasMoreStopsWithUnknownTotalOnlyAtShortOrNonGrowingPage() {
        // total unknown (0): a full, growing page keeps paging…
        assertTrue(
            MusicPaging.hasMore(
                loaded = 40,
                total = 0,
                pageReceived = MUSIC_PAGE_SIZE,
                previousLoaded = 20,
            ),
        )
        // …until a short page ends the list.
        assertFalse(
            MusicPaging.hasMore(
                loaded = 45,
                total = 0,
                pageReceived = 5,
                previousLoaded = 40,
            ),
        )
    }
}
