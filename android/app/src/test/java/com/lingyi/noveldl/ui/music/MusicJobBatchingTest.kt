package com.lingyi.noveldl.ui.music

import com.lingyi.noveldl.data.MusicTrack
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Splitting a cross-page selection into per-`search_id` jobs.
 *
 * The server caches the provider objects of one search request under one
 * `search_id`; reusing the first page's id for tracks of a later page makes
 * those tracks fail with "search result expired". One job per page is the
 * client-side fix that keeps the REST contract unchanged.
 */
class MusicJobBatchingTest {

    private fun track(id: String, provider: String = "netease") =
        MusicTrack(provider = provider, id = id, name = "song-$id")

    private fun idsByTrack(vararg pairs: Pair<MusicTrack, String>): Map<String, String> =
        pairs.associate { (track, id) -> MusicJobResults.trackKey(track) to id }

    @Test
    fun emptySelectionProducesNoJobs() {
        assertEquals(emptyList<MusicJobBatch>(), MusicJobBatching.batches(emptyList(), emptyMap()))
    }

    @Test
    fun selectionFromOnePageBecomesOneJobWithThatPageId() {
        val first = track("1")
        val second = track("2")

        val batches = MusicJobBatching.batches(
            listOf(first, second),
            idsByTrack(first to "sid-page-1", second to "sid-page-1"),
        )

        assertEquals(1, batches.size)
        assertEquals("sid-page-1", batches.single().searchId)
        assertEquals(listOf("1", "2"), batches.single().tracks.map { it.id })
    }

    @Test
    fun crossPageSelectionBecomesOneJobPerPage() {
        val pageOne = track("1")
        val pageOneB = track("2")
        val pageTwo = track("3")

        val batches = MusicJobBatching.batches(
            listOf(pageOne, pageOneB, pageTwo),
            idsByTrack(
                pageOne to "sid-1",
                pageOneB to "sid-1",
                pageTwo to "sid-2",
            ),
        )

        // This is the case that used to fall back to submitted dictionaries
        // for one half of the selection.
        assertEquals(listOf("sid-1", "sid-2"), batches.map { it.searchId })
        assertEquals(listOf("1", "2"), batches[0].tracks.map { it.id })
        assertEquals(listOf("3"), batches[1].tracks.map { it.id })
    }

    @Test
    fun tracksWithoutAPageIdGoIntoOneDictionaryJobLast() {
        val tagged = track("1")
        val untagged = track("2")

        val batches = MusicJobBatching.batches(
            listOf(untagged, tagged),
            idsByTrack(tagged to "sid-1"),
        )

        assertEquals(2, batches.size)
        // Id-resolvable tracks are submitted first; the dictionary batch keeps
        // the server fallback for responses that never had a search id.
        assertEquals("sid-1", batches[0].searchId)
        assertEquals(listOf("1"), batches[0].tracks.map { it.id })
        assertNull(batches[1].searchId)
        assertEquals(listOf("2"), batches[1].tracks.map { it.id })
    }

    @Test
    fun blankIdsAreTreatedAsMissing() {
        val blank = track("1")
        val padded = track("2")

        val batches = MusicJobBatching.batches(
            listOf(blank, padded),
            idsByTrack(blank to "  ", padded to " sid "),
        )

        assertEquals(listOf("sid", null), batches.map { it.searchId })
    }

    @Test
    fun sameNumericIdOnDifferentProvidersKeepsItsOwnPage() {
        val netease = track("1", provider = "netease")
        val qq = track("1", provider = "qq")

        val batches = MusicJobBatching.batches(
            listOf(netease, qq),
            idsByTrack(netease to "sid-netease", qq to "sid-qq"),
        )

        assertEquals(2, batches.size)
        assertEquals("sid-netease", batches[0].searchId)
        assertEquals("netease", batches[0].tracks.single().provider)
        assertEquals("sid-qq", batches[1].searchId)
        assertEquals("qq", batches[1].tracks.single().provider)
    }

    @Test
    fun everyChosenTrackAppearsInExactlyOneBatch() {
        val one = track("1")
        val two = track("2")
        val three = track("3", provider = "qq")
        val four = track("4")

        val batches = MusicJobBatching.batches(
            listOf(one, two, three, four),
            idsByTrack(one to "sid-a", two to "sid-a", three to "sid-b"),
        )

        val submitted = batches.flatMap { it.tracks }
        assertEquals(4, submitted.size)
        assertEquals(
            listOf("netease\u00001", "netease\u00002", "qq\u00003", "netease\u00004"),
            submitted.map { MusicJobResults.trackKey(it) },
        )
        // No batch is empty: the server would reject an empty track list.
        assertTrue(batches.all { it.tracks.isNotEmpty() })
    }
}
