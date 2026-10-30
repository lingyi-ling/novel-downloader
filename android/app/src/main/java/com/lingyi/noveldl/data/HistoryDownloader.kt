package com.lingyi.noveldl.data

import android.app.DownloadManager
import android.content.Context
import android.net.Uri
import android.os.Environment
import androidx.documentfile.provider.DocumentFile
import com.lingyi.noveldl.R
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.util.concurrent.TimeUnit

/**
 * Enqueues history files into the system DownloadManager, or — when the user
 * picked a custom SAF directory in the settings — downloads them over OkHttp
 * straight into a [DOWNLOAD_SUBDIRECTORY] folder of that tree.
 *
 * The DownloadManager destination is the public Downloads directory, which it
 * may write to without `WRITE_EXTERNAL_STORAGE` even on Android 8/9. The API
 * key is attached as a request header because the download endpoint is
 * authenticated.
 *
 * DownloadManager cannot write to arbitrary SAF tree URIs, so the custom
 * directory path performs its own streaming download via ContentResolver.
 */
object HistoryDownloader {

    const val API_KEY_HEADER = DOWNLOAD_API_KEY_HEADER

    /** Subfolder created inside the user-chosen SAF tree. */
    const val SUBDIRECTORY = DOWNLOAD_SUBDIRECTORY

    /** Starts the DownloadManager download and returns its id, or a failure. */
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
        // No key configured -> empty map -> the download is sent unauthenticated,
        // which is what an API started without --token expects.
        downloadRequestHeaders(Api.apiKey).forEach { (name, value) ->
            request.addRequestHeader(name, value)
        }
        Result.success(manager.enqueue(request))
    } catch (e: Exception) {
        Result.failure(e)
    }

    /**
     * Downloads [file] into the custom SAF directory at [treeUri], creating the
     * [SUBDIRECTORY] folder with DocumentFile when needed. Suspend + IO because
     * the network and content-provider work must stay off the main thread.
     *
     * Returns the saved file name, or a failure with a user-readable message:
     * a lost persisted permission or an uncreatable file is reported instead of
     * silently falling back to the system Downloads directory.
     */
    suspend fun downloadToDirectory(
        context: Context,
        file: HistoryFile,
        treeUri: Uri,
    ): Result<String> {
        var created: DocumentFile? = null
        return try {
            withContext(Dispatchers.IO) {
                val resolver = context.contentResolver
                val hasWrite = resolver.persistedUriPermissions.any {
                    it.uri == treeUri && it.isWritePermission
                }
                if (!hasWrite) {
                    throw IllegalStateException(
                        context.getString(R.string.settings_download_dir_unavailable),
                    )
                }
                val url = resolveDownloadUrl(Api.baseUrl, file.download_url)
                if (!isSupportedDownloadUrl(url)) {
                    throw IllegalArgumentException(
                        context.getString(R.string.history_download_invalid_url),
                    )
                }
                val tree = DocumentFile.fromTreeUri(context, treeUri)
                    ?: throw IllegalStateException(
                        context.getString(R.string.settings_download_dir_unavailable),
                    )
                val dir = tree.findFile(SUBDIRECTORY)?.takeIf { it.isDirectory }
                    ?: tree.createDirectory(SUBDIRECTORY)
                    ?: throw IllegalStateException(
                        context.getString(R.string.settings_download_dir_create_failed),
                    )
                val fileName = downloadFileName(file.name, file.download_url, file.ext)
                val mime = mimeTypeForExtension(file.ext) ?: "application/octet-stream"
                val target = dir.createFile(mime, fileName)
                    ?: throw IllegalStateException(
                        context.getString(R.string.settings_download_dir_create_failed),
                    )
                created = target

                val builder = Request.Builder().url(url)
                downloadRequestHeaders(Api.apiKey).forEach { (name, value) ->
                    builder.header(name, value)
                }
                val client = OkHttpClient.Builder()
                    .connectTimeout(10, TimeUnit.SECONDS)
                    .readTimeout(60, TimeUnit.SECONDS)
                    .build()
                client.newCall(builder.build()).execute().use { response ->
                    if (!response.isSuccessful) {
                        throw IllegalStateException("HTTP ${response.code}")
                    }
                    val body = response.body
                        ?: throw IllegalStateException(
                            context.getString(R.string.history_download_unknown_error),
                        )
                    val output = resolver.openOutputStream(target.uri)
                        ?: throw IllegalStateException(
                            context.getString(R.string.settings_download_dir_create_failed),
                        )
                    body.byteStream().use { input ->
                        output.use { input.copyTo(it) }
                    }
                }
                fileName
            }.let { Result.success(it) }
        } catch (e: Exception) {
            // Never leave a half-written document behind.
            created?.let { runCatching { it.delete() } }
            Result.failure(e)
        }
    }
}