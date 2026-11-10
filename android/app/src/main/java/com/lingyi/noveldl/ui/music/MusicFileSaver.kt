package com.lingyi.noveldl.ui.music

import com.lingyi.noveldl.data.MusicJobResult

/**
 * Hands a finished track to the platform downloader so the file lands in the
 * user's Downloads folder. Separated from the job runner so the orchestration
 * stays plain JVM-testable; the production implementation is
 * [DownloadManagerMusicSaver].
 */
fun interface MusicFileSaver {

    fun save(result: MusicJobResult): Result<Unit>

    companion object {
        /** Used by unit tests and by any transport that cannot write locally. */
        val None: MusicFileSaver = MusicFileSaver { Result.success(Unit) }
    }
}
