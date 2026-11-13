package com.lingyi.noveldl.ui.music

import android.content.Context
import com.lingyi.noveldl.data.EmbeddedEngine
import com.lingyi.noveldl.data.MusicJobResult
import com.lingyi.noveldl.data.MusicLocalPaths
import java.io.File

/**
 * Rules for the `<stem>.lrc` file the music engine writes beside an audio
 * download.
 *
 * The engine saves lyrics as a separate file next to the audio (see the
 * Python `save_lyric`): `<title> - <artist>.mp3` gets `<title> - <artist>.lrc`
 * and, when a translation exists, `<title> - <artist>.zh.lrc`. Only the
 * original file is exposed here, matching the desktop client and CLI: the
 * translation alone is not reported as "lyrics saved".
 *
 * Nothing in this object touches Android: derivation, containment and the
 * existence probe are injectable, so plain JVM tests can cover the path rules
 * without a device.
 */
object MusicLyrics {

    /** Extension of the original lyric file; the translation uses `.zh.lrc`. */
    const val EXTENSION: String = ".lrc"

    /**
     * Music output directory below [EmbeddedEngine.HOME_DIRECTORY], i.e. the
     * engine's default `general.output_dir` (`./downloads`) plus `music`.
     * The app never sends a custom `out_dir`, so every engine-produced file
     * lands below this directory.
     */
    const val EMBEDDED_MUSIC_SUBDIRECTORY: String = "downloads/music"

    /**
     * Sibling `.lrc` path for [audioPath]: the last extension of the file name
     * is replaced, so `.mp3`/`.flac`/`.m4a` all map to the same lyric name.
     * A name without an extension gets `.lrc` appended; blank input yields
     * an empty string.
     *
     * Both separators are accepted so a path produced on Windows (tests,
     * desktop conversions) keeps its shape, while Android paths are POSIX.
     */
    fun lyricPathFor(audioPath: String): String {
        val path = audioPath.trim()
        if (path.isEmpty()) return ""
        val separator = maxOf(path.lastIndexOf('/'), path.lastIndexOf('\\'))
        val nameStart = separator + 1
        // A trailing separator names no file at all.
        if (nameStart >= path.length) return ""
        // A dot in a parent directory (`/a.b/song`) is not an extension; a
        // leading dot (`.mp3`) is a hidden name, not an extension either.
        val dot = path.lastIndexOf('.')
        val stem = if (dot > nameStart) path.substring(0, dot) else path
        return stem + EXTENSION
    }

    /**
     * Verifies that the lyric beside [audioPath] exists and lives inside one
     * of [roots] (the app's private directories).
     *
     * Returns the path only after both checks pass: a lyric outside the app
     * sandbox is not reachable through the FileProvider and must not be
     * offered as a button, and a stale or failed write must not be reported as
     * saved. A probe that throws (permission change, provider gone) counts as
     * "no lyric" instead of crashing the job result.
     */
    fun find(
        audioPath: String,
        roots: Collection<String>,
        exists: (String) -> Boolean,
    ): String? {
        val candidate = lyricPathFor(audioPath)
        if (candidate.isEmpty()) return null
        if (roots.none { MusicLocalPaths.isInside(it, candidate) }) return null
        return if (runCatching { exists(candidate) }.getOrDefault(false)) candidate else null
    }

    /** Number of results that carry a verified lyric file, for the summary. */
    fun savedCount(results: List<MusicJobResult>): Int =
        results.count { !it.lyricPath.isNullOrBlank() }
}

/**
 * Resolves the verified `.lrc` of one finished track.
 *
 * Kept as a small interface (like [MusicFileSaver]) so [MusicJobRunner] stays
 * plain JVM-testable; the production implementation is
 * [PrivateMusicLyricLookup].
 */
fun interface MusicLyricLookup {

    /** Verified `<stem>.lrc` beside [audioPath], or null when there is none. */
    fun find(audioPath: String): String?

    companion object {
        /** Used by unit tests and by transports without local files. */
        val None: MusicLyricLookup = MusicLyricLookup { null }
    }
}

/**
 * Looks for lyrics under the embedded engine's private music directory.
 *
 * The check is deliberately limited to that directory (inside `filesDir`), not
 * the whole app sandbox: it is exactly the range declared in
 * `res/xml/file_paths.xml`, so a path that passes this check can always be
 * turned into a FileProvider URI and opened. A remote server's paths are
 * outside it and therefore never produce a button.
 */
class PrivateMusicLyricLookup(
    private val roots: List<String>,
    private val exists: (String) -> Boolean = { File(it).isFile },
) : MusicLyricLookup {

    constructor(context: Context) : this(
        listOf(
            File(
                context.applicationContext.filesDir,
                "${EmbeddedEngine.HOME_DIRECTORY}/${MusicLyrics.EMBEDDED_MUSIC_SUBDIRECTORY}",
            ).absolutePath,
        ),
    )

    override fun find(audioPath: String): String? = MusicLyrics.find(audioPath, roots, exists)
}
