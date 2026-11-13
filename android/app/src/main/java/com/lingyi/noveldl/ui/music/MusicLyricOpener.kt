package com.lingyi.noveldl.ui.music

import android.content.ActivityNotFoundException
import android.content.ClipData
import android.content.Context
import android.content.Intent
import androidx.core.content.FileProvider
import com.lingyi.noveldl.data.MusicLocalPaths
import java.io.File

/** What [MusicLyricOpener.open] managed to do, mapped to a message by the UI. */
enum class MusicLyricOpenResult {
    /** A viewer was found and the intent was started. */
    Opened,

    /** The `.lrc` disappeared, or it is not reachable through the provider. */
    Missing,

    /** No installed app handles `.lrc` / plain text. */
    NoApp,
}

/**
 * Opens a downloaded `.lrc` in whichever text viewer the user has installed.
 *
 * The file lives in the app's private storage, so it is handed out through
 * the existing [FileProvider] (`${applicationId}.fileprovider`); the URI grant
 * is scoped to this one file. `.lrc` has no registered MIME type on Android,
 * so `text/x-lyric` is tried first (players that know lyrics) and `text/plain`
 * second (any text editor); [MusicLyricOpenResult.NoApp] is returned only when
 * neither resolves, instead of failing silently.
 *
 * The path is re-validated here rather than trusting the caller: the URI is
 * only produced for files inside the app's own directories, so a stale or
 * engine-supplied path outside the sandbox can never be exposed.
 */
object MusicLyricOpener {

    /** Common MIME type for LRC files, tried before plain text. */
    const val MIME_LYRIC: String = "text/x-lyric"

    /** Fallback MIME type understood by every text viewer. */
    const val MIME_PLAIN: String = "text/plain"

    private val MIME_TYPES = listOf(MIME_LYRIC, MIME_PLAIN)

    fun open(context: Context, lyricPath: String): MusicLyricOpenResult {
        val path = lyricPath.trim()
        if (path.isEmpty()) return MusicLyricOpenResult.Missing
        if (!MusicLocalPaths.isLocal(path, MusicLocalPaths.deviceRoots(context))) {
            return MusicLyricOpenResult.Missing
        }
        val file = File(path)
        if (!file.isFile) return MusicLyricOpenResult.Missing

        // getUriForFile throws when the file is outside file_paths.xml; a
        // lyric that cannot be exposed is indistinguishable from a missing
        // one for the user, and reporting "no app" would be wrong.
        val uri = runCatching {
            FileProvider.getUriForFile(context, "${context.packageName}.fileprovider", file)
        }.getOrNull() ?: return MusicLyricOpenResult.Missing

        MIME_TYPES.forEach { mime ->
            val intent = Intent(Intent.ACTION_VIEW).apply {
                setDataAndType(uri, mime)
                // Some viewers only look at the clip data even though the URI
                // is also the intent data.
                clipData = ClipData.newRawUri(null, uri)
                addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            }
            try {
                context.startActivity(intent)
                return MusicLyricOpenResult.Opened
            } catch (_: ActivityNotFoundException) {
                // Try the next MIME type; some devices register only one.
            } catch (_: SecurityException) {
                // The grant was rejected; another viewer is unlikely to help.
                return MusicLyricOpenResult.NoApp
            }
        }
        return MusicLyricOpenResult.NoApp
    }
}
