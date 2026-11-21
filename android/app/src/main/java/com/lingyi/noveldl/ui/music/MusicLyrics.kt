package com.lingyi.noveldl.ui.music

import android.content.Context
import com.lingyi.noveldl.data.EmbeddedEngine
import com.lingyi.noveldl.data.MusicJobResult
import com.lingyi.noveldl.data.MusicLocalPaths
import com.lingyi.noveldl.data.MusicLyricFile
import java.io.File

/**
 * Rules for the lyric files the music engine writes beside an audio download.
 *
 * The engine saves lyrics as separate files next to the audio (see the Python
 * `save_lyric`): `<title> - <artist>.mp3` gets `<title> - <artist>.lrc` for
 * the original text and, when a translation exists,
 * `<title> - <artist>.zh.lrc` for it. Both files are exposed: the result row
 * shows one button when only the original was saved and two buttons (original
 * + translation) when the translation exists too.
 *
 * Nothing in this object touches Android: derivation, containment and the
 * existence probe are injectable, so plain JVM tests can cover the path rules
 * without a device.
 */
object MusicLyrics {

    /** Extension of the original lyric file; the translation uses `.zh.lrc`. */
    const val EXTENSION: String = ".lrc"

    /** Extension of the translated lyric file, written beside the original. */
    const val TRANSLATION_EXTENSION: String = ".zh.lrc"

    /**
     * Stable language slots reported for a track's lyric files. These are
     * identifiers, not display copy: the UI maps them to localized labels so
     * an old result keeps its meaning when the interface language changes.
     * They match what the desktop bridge reports (`lyric_paths[].lang`).
     */
    const val LANG_ORIGINAL: String = "原文"
    const val LANG_TRANSLATION: String = "译文"

    /**
     * Music output directory below [EmbeddedEngine.HOME_DIRECTORY], i.e. the
     * engine's default `general.output_dir` (`./downloads`) plus `music`.
     * The app never sends a custom `out_dir`, so every engine-produced file
     * lands below this directory.
     */
    const val EMBEDDED_MUSIC_SUBDIRECTORY: String = "downloads/music"

    /**
     * Absolute path of the single private directory declared in
     * `res/xml/file_paths.xml` (`files/engine_home/downloads/music/`).
     *
     * The lyric lookup and [MusicLyricOpener] both validate against this one
     * definition instead of the whole app sandbox: a path that passes the
     * check is always inside the provider range, and anything else is rejected
     * before `FileProvider.getUriForFile` is reached.
     */
    fun providerRoot(context: Context): String = File(
        context.applicationContext.filesDir,
        "${EmbeddedEngine.HOME_DIRECTORY}/$EMBEDDED_MUSIC_SUBDIRECTORY",
    ).absolutePath

    /**
     * Sibling `.lrc` path for [audioPath]: the last extension of the file name
     * is replaced, so `.mp3`/`.flac`/`.m4a` all map to the same lyric name.
     * A name without an extension gets `.lrc` appended; blank input yields
     * an empty string.
     *
     * Both separators are accepted so a path produced on Windows (tests,
     * desktop conversions) keeps its shape, while Android paths are POSIX.
     */
    fun lyricPathFor(audioPath: String): String = withExtension(audioPath, EXTENSION)

    /**
     * Sibling `.zh.lrc` path for [audioPath]: the translation the engine wrote
     * beside the audio when the provider supplied one. Derived the same way as
     * [lyricPathFor], so both files always share the audio's stem.
     */
    fun translationLyricPathFor(audioPath: String): String =
        withExtension(audioPath, TRANSLATION_EXTENSION)

    /**
     * Stem of [audioPath] with its last file extension removed. Empty for a
     * blank path or a trailing separator (which names no file at all).
     */
    private fun lyricStem(audioPath: String): String {
        val path = audioPath.trim()
        if (path.isEmpty()) return ""
        val separator = maxOf(path.lastIndexOf('/'), path.lastIndexOf('\\'))
        val nameStart = separator + 1
        // A trailing separator names no file at all.
        if (nameStart >= path.length) return ""
        // A dot in a parent directory (`/a.b/song`) is not an extension; a
        // leading dot (`.mp3`) is a hidden name, not an extension either.
        val dot = path.lastIndexOf('.')
        return if (dot > nameStart) path.substring(0, dot) else path
    }

    private fun withExtension(audioPath: String, extension: String): String {
        val stem = lyricStem(audioPath)
        return if (stem.isEmpty()) "" else stem + extension
    }

    /**
     * Verifies the original lyric beside [audioPath] and lives inside one of
     * [roots] (the app's private directories).
     *
     * Returns the original path only after both checks pass, preserving the
     * legacy single-button contract. [findAll] is the translation-aware entry
     * point used by the job runner.
     */
    fun find(
        audioPath: String,
        roots: Collection<String>,
        exists: (String) -> Boolean,
    ): String? = findAll(audioPath, roots, exists)
        .firstOrNull { it.lang == LANG_ORIGINAL }
        ?.path

    /**
     * Verifies every lyric file beside [audioPath], original first, then the
     * translation.
     *
     * A path is returned only after two checks: it must live inside one of
     * [roots] (otherwise the FileProvider cannot expose it), and the existence
     * probe must confirm the file. A stale or failed write is thus never
     * offered as a button, and a probe that throws (permission change,
     * provider gone) counts as "no lyric" instead of crashing the job result.
     */
    fun findAll(
        audioPath: String,
        roots: Collection<String>,
        exists: (String) -> Boolean,
    ): List<MusicLyricFile> {
        val candidates = listOf(
            LANG_ORIGINAL to lyricPathFor(audioPath),
            LANG_TRANSLATION to translationLyricPathFor(audioPath),
        )
        return candidates.mapNotNull { (lang, candidate) ->
            if (candidate.isEmpty()) return@mapNotNull null
            if (roots.none { MusicLocalPaths.isInside(it, candidate) }) return@mapNotNull null
            val present = runCatching { exists(candidate) }.getOrDefault(false)
            if (present) MusicLyricFile(lang = lang, path = candidate) else null
        }
    }

    /**
     * Number of tracks that carry at least one verified lyric file, for the
     * summary. A track with both the original and the translation counts once.
     */
    fun savedCount(results: List<MusicJobResult>): Int =
        results.count { result ->
            !result.lyricPath.isNullOrBlank() ||
                result.lyricPaths.any { it.path.isNotBlank() }
        }
}

/**
 * Resolves the verified lyric files of one finished track.
 *
 * Kept as a small interface (like [MusicFileSaver]) so [MusicJobRunner] stays
 * plain JVM-testable; the production implementation is
 * [PrivateMusicLyricLookup].
 */
fun interface MusicLyricLookup {

    /**
     * Verified lyric files beside [audioPath], original first, then the
     * translation; empty when none exists (or when this transport has no
     * local files).
     */
    fun find(audioPath: String): List<MusicLyricFile>

    companion object {
        /** Used by unit tests and by transports without local files. */
        val None: MusicLyricLookup = MusicLyricLookup { emptyList() }
    }
}

/**
 * Looks for lyrics under the embedded engine's private music directory.
 *
 * The check is deliberately limited to that directory (inside `filesDir`), not
 * the whole app sandbox: it is exactly the range declared in
 * `res/xml/file_paths.xml`, so a path that passes this check (including the
 * `.zh.lrc` translation) can always be turned into a FileProvider URI and
 * opened. A remote server's paths are outside it and therefore never produce a
 * button.
 */
class PrivateMusicLyricLookup(
    private val roots: List<String>,
    private val exists: (String) -> Boolean = { File(it).isFile },
) : MusicLyricLookup {

    constructor(context: Context) : this(listOf(MusicLyrics.providerRoot(context)))

    override fun find(audioPath: String): List<MusicLyricFile> =
        MusicLyrics.findAll(audioPath, roots, exists)
}
