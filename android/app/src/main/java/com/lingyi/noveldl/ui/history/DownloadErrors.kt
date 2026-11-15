package com.lingyi.noveldl.ui.history

import android.app.DownloadManager
import androidx.annotation.StringRes
import com.lingyi.noveldl.R
import com.lingyi.noveldl.data.DownloadDirectoryUnavailableException
import com.lingyi.noveldl.data.DownloadFailureException
import com.lingyi.noveldl.data.DownloadFailureKind

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

/**
 * Maps a classified SAF download failure to the reason shown inside
 * "下载失败：file（reason）".
 *
 * Space, permission and provider failures each get wording that names the fix;
 * a network failure reuses the existing message. Kept beside
 * [downloadFailureReasonRes] so both download paths explain causes the same
 * way, and JVM-testable because it only returns resource ids.
 */
@StringRes
internal fun downloadFailureMessageRes(kind: DownloadFailureKind): Int = when (kind) {
    DownloadFailureKind.Network -> R.string.history_download_network_error
    DownloadFailureKind.NoSpace -> R.string.download_error_no_space
    DownloadFailureKind.Permission -> R.string.download_error_permission
    DownloadFailureKind.Provider -> R.string.download_error_provider
}

/**
 * Whether a failed custom-directory download may be retried into the system
 * Downloads directory.
 *
 * Only a missing/deleted tree ([DownloadDirectoryUnavailableException]) or a
 * revoked write grant ([DownloadFailureKind.Permission]) can succeed there: the
 * destination changed, not the file. A full volume, a broken provider, a
 * network/HTTP failure or a cancellation would fail at the fallback destination
 * too, and silently moving the file would hide the real cause, so those are
 * reported instead.
 */
internal fun shouldFallBackToSystemDownloads(error: Throwable): Boolean =
    error is DownloadDirectoryUnavailableException ||
        (error is DownloadFailureException && error.kind == DownloadFailureKind.Permission)
