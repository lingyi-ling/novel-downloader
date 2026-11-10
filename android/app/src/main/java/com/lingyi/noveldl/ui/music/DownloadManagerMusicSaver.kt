package com.lingyi.noveldl.ui.music

import android.content.Context
import com.lingyi.noveldl.data.HistoryDownloader
import com.lingyi.noveldl.data.HistoryFile
import com.lingyi.noveldl.data.MusicJobResult

/**
 * Saves an engine-produced track through the system DownloadManager, i.e. into
 * the public Downloads folder (or the user's chosen directory on the other
 * path used by the history screen).
 *
 * Kept as an interface in the runner so job orchestration can be tested on a
 * plain JVM; this implementation is the Android side of the hand-off.
 */
class DownloadManagerMusicSaver(private val context: Context) : MusicFileSaver {

    override fun save(result: MusicJobResult): Result<Unit> {
        val name = result.download_url.substringAfterLast('/')
        return HistoryDownloader.enqueue(
            context,
            HistoryFile(
                name = name,
                ext = name.substringAfterLast('.', ""),
                download_url = result.download_url,
            ),
        ).map { Unit }
    }
}
