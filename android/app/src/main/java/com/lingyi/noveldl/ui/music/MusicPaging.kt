package com.lingyi.noveldl.ui.music

import com.lingyi.noveldl.data.MusicTrack

/** Page size requested from `POST /api/v1/music/search` per "load more" step. */
const val MUSIC_PAGE_SIZE = 20

/**
 * Pure "load more" paging rules for the music search list, kept out of the
 * Composable so they can be exercised from plain JVM unit tests.
 *
 * Offset semantics (confirmed against the server sources): the provider layer
 * (`providers/base.py::search(keyword, limit, offset)`) treats `offset` as the
 * number of hits to skip — netease forwards it to the platform client as
 * `offset` (0 = first page) and reports `total` as the platform-side hit count
 * (0/unknown when unavailable). So the next page is requested with
 * `offset = number of results already loaded`.
 */
object MusicPaging {

    /**
     * Appends [page] to [current], dropping tracks whose [MusicTrack.id] is
     * already present (first occurrence wins, order preserved). Returns
     * [current] itself when the page adds nothing, so callers can compare
     * sizes to detect a page that contributed no new tracks.
     */
    fun merge(current: List<MusicTrack>, page: List<MusicTrack>): List<MusicTrack> {
        if (page.isEmpty()) return current
        val seen = current.mapTo(HashSet()) { it.id }
        val additions = page.filter { seen.add(it.id) }
        return if (additions.isEmpty()) current else current + additions
    }

    /**
     * Whether another page should be requested after [pageReceived] tracks
     * came back for a list that grew from [previousLoaded] to [loaded] items
     * against a reported [total]. Stops when:
     * - the page came back empty or shorter than [pageSize] (end of results);
     * - the loaded count reached the reported total (`offset >= total`);
     * - the page added nothing new — a server that ignores `offset` would
     *   repeat the first page forever, and platforms without pagination
     *   support (e.g. the musicdl backend returns an empty page for any
     *   non-zero offset) surface as empty or non-growing pages.
     */
    fun hasMore(
        loaded: Int,
        total: Int,
        pageReceived: Int,
        previousLoaded: Int,
        pageSize: Int = MUSIC_PAGE_SIZE,
    ): Boolean {
        if (pageReceived <= 0) return false
        if (pageReceived < pageSize) return false
        if (total > 0 && loaded >= total) return false
        if (loaded <= previousLoaded) return false
        return true
    }
}
