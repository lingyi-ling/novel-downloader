package com.lingyi.noveldl.ui.music

import com.lingyi.noveldl.data.MusicTrack

/**
 * Tracks that one `POST /api/v1/music/jobs` request can resolve.
 *
 * A search response caches its provider objects under its own `search_id`, and
 * that id cannot resolve tracks from another page. A cross-page selection
 * therefore has to be split: one job per page id, plus one dictionary-only job
 * for tracks that came without an id (non-aggregate responses may omit it).
 *
 * [searchId] is null for the dictionary-only batch; the request then omits the
 * field and the server falls back to the submitted track dictionaries.
 */
data class MusicJobBatch(
    val searchId: String?,
    val tracks: List<MusicTrack>,
)

/**
 * Splits a selection into per-`search_id` batches.
 *
 * Batches keep the order in which their first track was selected, and the
 * dictionary-only batch (if any) comes last: id-resolvable tracks are submitted
 * first, where they are most likely to succeed for the price of one request.
 * A selection spanning pages 1 and 2 thus produces two jobs instead of one
 * job whose second half fails with "search result expired".
 */
object MusicJobBatching {

    fun batches(
        chosen: List<MusicTrack>,
        idsByTrack: Map<String, String>,
    ): List<MusicJobBatch> {
        if (chosen.isEmpty()) return emptyList()
        val byId = LinkedHashMap<String, MutableList<MusicTrack>>()
        val withoutId = mutableListOf<MusicTrack>()
        chosen.forEach { track ->
            val id = idsByTrack[MusicJobResults.trackKey(track)]?.trim().orEmpty()
            if (id.isEmpty()) {
                withoutId += track
            } else {
                byId.getOrPut(id) { mutableListOf() } += track
            }
        }
        val batches = byId.map { (id, tracks) -> MusicJobBatch(id, tracks) }
        return if (withoutId.isEmpty()) batches else batches + MusicJobBatch(null, withoutId)
    }
}
