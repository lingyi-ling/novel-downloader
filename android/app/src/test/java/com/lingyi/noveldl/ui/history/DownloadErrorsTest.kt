package com.lingyi.noveldl.ui.history

import android.app.DownloadManager
import com.lingyi.noveldl.R
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * `DownloadManager` failure reasons mapped to readable text instead of the
 * bare numeric code the completion broadcast carries.
 */
class DownloadErrorsTest {

    @Test
    fun storageAndFileProblemsHaveDedicatedTexts() {
        // The two causes a custom Downloads directory actually produces.
        assertEquals(
            R.string.download_error_no_space,
            downloadFailureReasonRes(DownloadManager.ERROR_INSUFFICIENT_SPACE),
        )
        assertEquals(
            R.string.download_error_file,
            downloadFailureReasonRes(DownloadManager.ERROR_FILE_ERROR),
        )
    }

    @Test
    fun httpProblemsHaveDedicatedTexts() {
        assertEquals(
            R.string.download_error_http_data,
            downloadFailureReasonRes(DownloadManager.ERROR_HTTP_DATA_ERROR),
        )
        assertEquals(
            R.string.download_error_http_code,
            downloadFailureReasonRes(DownloadManager.ERROR_UNHANDLED_HTTP_CODE),
        )
        assertEquals(
            R.string.download_error_redirects,
            downloadFailureReasonRes(DownloadManager.ERROR_TOO_MANY_REDIRECTS),
        )
    }

    @Test
    fun unknownReasonKeepsTheNumericFallback() {
        // The caller formats the code itself when nothing specific matches.
        assertNull(downloadFailureReasonRes(-1))
        assertNull(downloadFailureReasonRes(424242))
    }
}
