package com.lingyi.noveldl.data

import android.app.DownloadManager
import android.content.Context
import android.net.Uri
import android.os.Environment
import com.lingyi.noveldl.R

/**
 * Enqueues history files into the system DownloadManager.
 *
 * The destination is the public Downloads directory, which DownloadManager may
 * write to without `WRITE_EXTERNAL_STORAGE` even on Android 8/9. The API key is
 * attached as a request header because the download endpoint is authenticated.
 */
object HistoryDownloader {

    const val API_KEY_HEADER = "X-API-Key"

    /** Starts the download and returns the DownloadManager id, or a failure. */
    fun enqueue(context: Context, file: HistoryFile): Result<Long> = try {
        val url = resolveDownloadUrl(Api.baseUrl, file.download_url)
        if (!isSupportedDownloadUrl(url)) {
            throw IllegalArgumentException(context.getString(R.string.history_download_invalid_url))
        }
        val manager = context.getSystemService(DownloadManager::class.java)
            ?: throw IllegalStateException(context.getString(R.string.history_download_no_manager))

        val fileName = downloadFileName(file.name, file.download_url, file.ext)
        val request = DownloadManager.Request(Uri.parse(url))
            .setTitle(fileName)
            .setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, fileName)
            .setNotificationVisibility(
                DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED,
            )
            .setAllowedOverMetered(true)
            .setAllowedOverRoaming(false)
        mimeTypeForExtension(file.ext)?.let(request::setMimeType)
        Api.apiKey.takeIf { it.isNotEmpty() }?.let {
            request.addRequestHeader(API_KEY_HEADER, it)
        }
        Result.success(manager.enqueue(request))
    } catch (e: Exception) {
        Result.failure(e)
    }
}