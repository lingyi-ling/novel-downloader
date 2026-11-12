package com.lingyi.noveldl.data

import android.app.DownloadManager
import android.content.Context
import android.net.Uri
import android.os.Environment
import androidx.documentfile.provider.DocumentFile
import com.lingyi.noveldl.R
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.IOException
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

    /**
     * Shared connection pool for SAF downloads. A new client per download
     * would allocate a fresh pool/dispatcher every time and keep the previous
     * one until GC.
     */
    private val baseClient by lazy {
        OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(60, TimeUnit.SECONDS)
            .build()
    }

    /**
     * One client for [downloadToDirectory]. The origin policy keeps the API
     * key on the configured API host: a server-supplied absolute URL or a
     * redirect to a CDN gets no credentials, and an HTTPS server cannot be
     * downgraded to plain HTTP by a redirect.
     */
    private fun httpClient(): OkHttpClient {
        val builder = baseClient.newBuilder()
        apiOriginPolicy(Api.baseUrl, Api.apiKey)?.let { policy ->
            builder.addNetworkInterceptor(policy.interceptor())
        }
        return builder.build()
    }

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
        // which is what an API started without --token expects. A URL on
        // another origin gets no key: DownloadManager would forward it across
        // redirects.
        downloadRequestHeaders(Api.apiKey, url, Api.baseUrl).forEach { (name, value) ->
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
                downloadRequestHeaders(Api.apiKey, url, Api.baseUrl).forEach { (name, value) ->
                    builder.header(name, value)
                }
                // Enqueued so cancellation (navigating away, closing the
                // screen) cancels the call instead of running to the read
                // timeout and leaving a half-written document.
                httpClient().executeCancellable(builder.build()).use { response ->
                    if (!response.isSuccessful) {
                        throw IllegalStateException(
                            context.getString(
                                R.string.history_download_http_error,
                                response.code,
                            ),
                        )
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
                        output.use {
                            val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
                            while (true) {
                                // A cancelled coroutine must stop reading; the
                                // catch below deletes the partial document.
                                currentCoroutineContext().ensureActive()
                                val read = input.read(buffer)
                                if (read <= 0) break
                                it.write(buffer, 0, read)
                            }
                        }
                    }
                }
                fileName
            }.let { Result.success(it) }
        } catch (e: CancellationException) {
            // Never leave a half-written document behind, and never turn a
            // cancellation into a failure result.
            created?.let { runCatching { it.delete() } }
            throw e
        } catch (e: IOException) {
            // Never leave a half-written document behind.
            created?.let { runCatching { it.delete() } }
            Result.failure(
                IllegalStateException(
                    context.getString(R.string.history_download_network_error),
                    e,
                ),
            )
        } catch (e: Exception) {
            // Never leave a half-written document behind.
            created?.let { runCatching { it.delete() } }
            Result.failure(e)
        }
    }
}