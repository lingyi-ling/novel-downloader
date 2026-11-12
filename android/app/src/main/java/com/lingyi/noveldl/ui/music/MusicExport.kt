package com.lingyi.noveldl.ui.music

import com.lingyi.noveldl.data.MusicLocalPaths
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
     * Fills blank paths from the persisted `provider\u0000id -> path` map.
     *
     * A path already present on the visible row wins: it was produced by the
     * current session (or restored with the search state) and is fresher than
     * the cache. Returns [tracks] itself when nothing changes.
     */
    fun mergePersistedPaths(
        tracks: List<MusicTrack>,
        persisted: Map<String, String>,
    ): List<MusicTrack> {
        if (tracks.isEmpty() || persisted.isEmpty()) return tracks
        var changed = false
        val merged = tracks.map { track ->
            if (track.path.isNotBlank()) {
                track
            } else {
                val path = persisted[MusicJobResults.trackKey(track)]
                if (path.isNullOrBlank() || path == track.path) {
                    track
                } else {
                    changed = true
                    track.copy(path = path)
                }
            }
        }
        return if (changed) merged else tracks
    }

    /**
     * Whether [path] may be written into a playlist.
     *
     * Device-local paths (under [localRoots]) must still exist — a stale cache
     * entry for a deleted file would otherwise produce a dead playlist line.
     * Paths outside those roots are engine/server-side paths in remote mode:
     * the phone cannot inspect them, so they are left for the engine to
     * validate when it writes the playlist.
     */
    fun isExportablePath(
        path: String,
        localRoots: List<String>,
        exists: (String) -> Boolean,
    ): Boolean {
        val trimmed = path.trim()
        if (trimmed.isEmpty()) return false
        if (!MusicLocalPaths.isLocal(trimmed, localRoots)) return true
        // If the check itself fails (permissions), keep the path rather than
        // silently dropping a file that may well still be there.
        return runCatching { exists(trimmed) }.getOrDefault(true)
    }

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
     * Builds the export request plan, or `null` when no track has a usable
     * local path (the caller then reports "nothing to export" instead of
     * sending a request the server would answer with an empty playlist).
     *
     * [persisted] supplies paths for rows whose in-memory path died with the
     * previous process; [isUsable] rejects entries pointing at files that no
     * longer exist (the persisted store already prunes them, but a file can be
     * deleted between the read and the request).
     */
    fun plan(
        tracks: List<MusicTrack>,
        persisted: Map<String, String> = emptyMap(),
        isUsable: (String) -> Boolean = { true },
    ): MusicExportPlan? {
        val merged = mergePersistedPaths(tracks, persisted)
        val usable = merged.filter { it.path.isNotBlank() && isUsable(it.path) }
        if (usable.isEmpty()) return null
        val directory = usable.firstNotNullOfOrNull {
            directoryOf(it.path).ifBlank { null }
        }
        return MusicExportPlan(
            tracks = usable,
            skippedTracks = merged.size - usable.size,
            outPath = directory?.let { "$it/$MUSIC_PLAYLIST_FILE_NAME" },
        )
    }
}
