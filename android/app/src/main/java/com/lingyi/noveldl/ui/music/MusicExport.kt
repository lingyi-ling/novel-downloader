package com.lingyi.noveldl.ui.music

import com.lingyi.noveldl.data.MusicTrack

/** Default playlist name; matches the embedded engine's own default. */
const val MUSIC_PLAYLIST_FILE_NAME = "playlist.m3u8"

/**
 * What `POST /api/v1/music/export` receives: only tracks that carry a local
 * file, the number of tracks dropped because they have none, and the playlist
 * destination (null lets the server choose its default directory).
 */
data class MusicExportPlan(
    val tracks: List<MusicTrack>,
    val skippedTracks: Int,
    val outPath: String?,
)

/**
 * Pure rules for exporting the current search results as an M3U8 playlist,
 * kept out of the Composable so plain JVM unit tests can exercise them.
 *
 * The server contract (`MusicExportRequest` in `apps/api/schemas.py`) writes
 * one entry per track with a non-blank `path` and counts the rest as skipped.
 * By default the playlist goes next to the audio files (the directory of the
 * first exportable track), which is also where the embedded engine downloads
 * music.
 */
object MusicExport {

    /** Tracks the server can write into a playlist (non-blank local path). */
    fun tracksWithLocalPath(tracks: List<MusicTrack>): List<MusicTrack> =
        tracks.filter { it.path.isNotBlank() }

    /**
     * Directory part of [path] using either separator; empty when [path] is
     * just a file name (or a root-relative name such as `/a.mp3`).
     */
    fun directoryOf(path: String): String {
        val trimmed = path.trim()
        val cut = maxOf(trimmed.lastIndexOf('/'), trimmed.lastIndexOf('\\'))
        return if (cut > 0) trimmed.substring(0, cut) else ""
    }

    /**
     * Builds the export request plan, or `null` when no track has a local
     * path (the caller then reports "nothing to export" instead of sending a
     * request the server would answer with an empty playlist).
     */
    fun plan(tracks: List<MusicTrack>): MusicExportPlan? {
        val usable = tracksWithLocalPath(tracks)
        if (usable.isEmpty()) return null
        val directory = usable.firstNotNullOfOrNull {
            directoryOf(it.path).ifBlank { null }
        }
        return MusicExportPlan(
            tracks = usable,
            skippedTracks = tracks.size - usable.size,
            outPath = directory?.let { "$it/$MUSIC_PLAYLIST_FILE_NAME" },
        )
    }
}
