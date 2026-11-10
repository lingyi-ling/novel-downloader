package com.lingyi.noveldl.ui.history

import android.app.DownloadManager
import androidx.annotation.StringRes
import com.lingyi.noveldl.R

/**
 * Maps a `DownloadManager` failure reason (`COLUMN_REASON` of a
 * `STATUS_FAILED` download) to a user-facing string.
 *
 * The raw code ("错误码 1006") tells the user nothing; the common causes are
 * worth naming, especially "no space" and "cannot write the file" because they
 * are the ones a custom Downloads directory produces. Unknown codes keep the
 * numeric fallback in the caller.
 *
 * `DownloadManager.ERROR_*` are compile-time int constants, so this stays
 * plain JVM-testable.
 */
@StringRes
internal fun downloadFailureReasonRes(reason: Int): Int? = when (reason) {
    DownloadManager.ERROR_INSUFFICIENT_SPACE -> R.string.download_error_no_space
    DownloadManager.ERROR_FILE_ERROR -> R.string.download_error_file
    DownloadManager.ERROR_FILE_ALREADY_EXISTS -> R.string.download_error_file_exists
    DownloadManager.ERROR_HTTP_DATA_ERROR -> R.string.download_error_http_data
    DownloadManager.ERROR_UNHANDLED_HTTP_CODE -> R.string.download_error_http_code
    DownloadManager.ERROR_TOO_MANY_REDIRECTS -> R.string.download_error_redirects
    DownloadManager.ERROR_CANNOT_RESUME -> R.string.download_error_cannot_resume
    DownloadManager.ERROR_UNKNOWN -> R.string.download_error_unknown
    else -> null
}
