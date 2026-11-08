package com.lingyi.noveldl.ui.music

import com.lingyi.noveldl.data.MusicTrack

/**
 * Pure helpers for the "sources" label on a music result row.
 *
 * Aggregate searches fill [MusicTrack.sources] with every platform that has
 * the song (best source first); single-platform responses have no such field,
 * so the row falls back to [MusicTrack.provider]. Kept out of the Composable
 * so plain JVM unit tests can exercise it.
 */
object MusicSources {

    /** Platform keys to display, trimmed and with blanks removed. */
    fun keys(track: MusicTrack): List<String> =
        track.sources
            .map { it.trim() }
            .filter { it.isNotEmpty() }
            .ifEmpty { listOf(track.provider.trim()).filter { it.isNotEmpty() } }

    /**
     * Human-readable sources for [track], mapping known platform keys through
     * [labels] and leaving unknown keys untouched. Empty when the track has
     * neither sources nor a provider.
     */
    fun label(track: MusicTrack, labels: Map<String, String>): String =
        keys(track)
            .joinToString(" / ") { key -> labels[key]?.takeIf { it.isNotBlank() } ?: key }
}
