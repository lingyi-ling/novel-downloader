package com.lingyi.noveldl.ui.music

import com.lingyi.noveldl.data.MusicJobResult
import com.lingyi.noveldl.data.MusicTrack

/**
 * Pure rules tying an asynchronous music download job back to the search
 * results it was started from, kept out of the Composable so plain JVM unit
 * tests can exercise them.
 *
 * Two invariants matter for the screen:
 * - a job's `search_id` only resolves the page it was returned with (the
 *   server caches one entry per search request), so a selection spanning
 *   pages is split into one job per id by [MusicJobBatching];
 * - job results are written back by [trackKey] (provider + id), never by
 *   position alone, so a newer search cannot receive another track's path.
 */
object MusicJobResults {

    /**
     * Stable track identity across JSON round-trips: the provider key plus the
     * provider-local song id. Two platforms can use the same numeric id, so
     * the id alone is not unique in aggregate searches.
     */
    fun trackKey(track: MusicTrack): String = "${track.provider}\u0000${track.id}"

    /**
     * Whether [value] has the [trackKey] shape (`provider\u0000id`, both parts
     * non-blank, exactly one separator). Used to reject selection entries
     * saved by older builds, which stored the bare id and would otherwise
     * match whichever provider happens to use that id.
     */
    fun isTrackKey(value: String): Boolean {
        val separator = value.indexOf('\u0000')
        if (separator <= 0 || separator >= value.lastIndex) return false
        return value.indexOf('\u0000', separator + 1) == -1
    }

    /**
     * Well-formed [trackKey] entries of a saved selection; older bare ids are
     * dropped instead of being reinterpreted as keys.
     */
    fun sanitizeSelection(saved: Iterable<String>?): Set<String> =
        saved?.filterTo(LinkedHashSet()) { isTrackKey(it) }.orEmpty()

    /**
     * Rows of [tracks] whose [trackKey] is in [selected], preserving the list
     * order. Selection sets are keyed by [trackKey], so the same numeric id
     * from two providers selects two independent rows.
     */
    fun selectedTracks(tracks: List<MusicTrack>, selected: Set<String>): List<MusicTrack> =
        if (selected.isEmpty()) emptyList() else tracks.filter { trackKey(it) in selected }

    /**
     * Records [searchId] for every track of [page], keeping the ids of pages
     * loaded earlier. A blank id (single-platform response without a cache) or
     * an empty page leaves [current] untouched.
     */
    fun rememberSearchIds(
        current: Map<String, String>,
        page: List<MusicTrack>,
        searchId: String,
    ): Map<String, String> {
        val id = searchId.trim()
        if (id.isEmpty() || page.isEmpty()) return current
        return current + page.associate { trackKey(it) to id }
    }

    /**
     * Local paths written by [results], keyed like [trackKey].
     *
     * The engine emits one result per submitted track, sorted by the original
     * index, so result `i` belongs to `chosen[i]`. Only successful results
     * with a non-blank path are kept; everything else leaves the track
     * untouched (and therefore not exportable).
     */
    fun pathsByTrack(
        chosen: List<MusicTrack>,
        results: List<MusicJobResult>,
    ): Map<String, String> {
        if (chosen.isEmpty() || results.isEmpty()) return emptyMap()
        val paths = LinkedHashMap<String, String>()
        chosen.forEachIndexed { index, track ->
            val result = results.getOrNull(index) ?: return@forEachIndexed
            if (result.status != "ok") return@forEachIndexed
            val path = result.path.trim()
            if (path.isEmpty()) return@forEachIndexed
            paths[trackKey(track)] = path
        }
        return paths
    }

    /**
     * Copies [paths] onto the matching rows of [tracks], by [trackKey]; rows
     * without a path keep theirs. Returns [tracks] itself when nothing
     * changes so callers can cheaply detect an unchanged list.
     */
    fun applyPaths(tracks: List<MusicTrack>, paths: Map<String, String>): List<MusicTrack> {
        if (paths.isEmpty() || tracks.isEmpty()) return tracks
        var changed = false
        val updated = tracks.map { track ->
            val path = paths[trackKey(track)]
            if (path == null || path == track.path) {
                track
            } else {
                changed = true
                track.copy(path = path)
            }
        }
        return if (changed) updated else tracks
    }

    /** Per-status counts of a finished job, as shown in the completion message. */
    data class Counts(val ok: Int, val skipped: Int, val failed: Int)

    fun counts(results: List<MusicJobResult>): Counts = Counts(
        ok = results.count { it.status == "ok" },
        skipped = results.count { it.status == "skip" },
        failed = results.count { it.status == "fail" },
    )

    /**
     * Results the app can hand to `DownloadManager`: only the ones the engine
     * exposes over loopback (`download_url`). The REST API leaves the field
     * empty because it writes files on the server machine instead, so a
     * non-empty list also tells the screen which mode produced the job.
     */
    fun downloadable(results: List<MusicJobResult>): List<MusicJobResult> =
        results.filter { it.status == "ok" && it.download_url.isNotBlank() }

    /**
     * First non-blank failure reason reported by the engine, or null.
     *
     * The completion line only carries counts; without this the user cannot
     * tell a DRM/account limitation from an expired search cache (the server
     * keeps one `search_id` entry for 30 minutes) and tends to retry the same
     * doomed download instead of searching again.
     */
    fun firstFailureReason(results: List<MusicJobResult>): String? =
        results.firstOrNull { it.status == "fail" && it.reason.isNotBlank() }?.reason
}

/** Request whose failure is currently shown in the music screen's error line. */
enum class MusicRequest { Platforms, Search, LoadMore }

/**
 * What the error line's retry button repeats. Repeating the *search* after a
 * failed "load more" throws away the pages already loaded (and the selection),
 * and repeating either without a keyword is a silent no-op, so both cases are
 * handled explicitly here instead of always calling `search()`.
 */
object MusicRetry {

    fun target(source: MusicRequest, keyword: String): MusicRequest? = when (source) {
        MusicRequest.Platforms -> MusicRequest.Platforms
        MusicRequest.Search, MusicRequest.LoadMore ->
            if (keyword.isBlank()) null else source
    }
}
