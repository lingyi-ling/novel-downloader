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
import java.io.InputStream
import java.io.OutputStream
import java.util.concurrent.TimeUnit
import kotlin.math.roundToInt

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
        apiOriginPolicy(Api.baseUrl, Api.requestKey)?.let { policy ->
            builder.addNetworkInterceptor(policy.interceptor())
        }
        return builder.build()
    }

    /**
     * Snapshot of the tree URIs the app currently holds a persisted write
     * grant for. Both the settings warning and the download pre-check use it,
     * so "looks available" and "actually writable" cannot drift apart.
     */
    fun writeGrantedTreeUris(context: Context): Set<String> =
        context.contentResolver.persistedUriPermissions
            .asSequence()
            .filter { it.isWritePermission }
            .mapTo(mutableSetOf()) { it.uri.toString() }

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
        downloadRequestHeaders(Api.requestKey, url, Api.baseUrl).forEach { (name, value) ->
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
     * [onProgress] receives throttled byte-count samples while the body is
     * streamed into the document. It is invoked from the IO dispatcher (not the
     * main thread) and may be called many times for a large file, so it must be
     * cheap and thread-safe — the history screen writes them into a StateFlow.
     *
     * Returns the saved file name, or a failure with a user-readable message:
     * a lost persisted permission or an uncreatable file is reported instead of
     * silently falling back to the system Downloads directory. Write failures
     * carry a [DownloadFailureKind] so the caller can name the cause; read
     * failures surface as the existing network error.
     */
    suspend fun downloadToDirectory(
        context: Context,
        file: HistoryFile,
        treeUri: Uri,
        onProgress: (DownloadProgress) -> Unit = {},
    ): Result<String> {
        var created: DocumentFile? = null
        return try {
            withContext(Dispatchers.IO) {
                val resolver = context.contentResolver
                if (!hasPersistedWriteAccess(treeUri.toString(), writeGrantedTreeUris(context))) {
                    throw DownloadDirectoryUnavailableException(
                        context.getString(R.string.settings_download_dir_unavailable),
                    )
                }
                val url = resolveDownloadUrl(Api.baseUrl, file.download_url)
                if (!isSupportedDownloadUrl(url)) {
                    throw IllegalArgumentException(
                        context.getString(R.string.history_download_invalid_url),
                    )
                }
                // A revoked grant can surface as SecurityException from the
                // provider query rather than as a null DocumentFile; treat
                // both as "the tree is gone" so the caller falls back.
                val tree = runCatching {
                    DocumentFile.fromTreeUri(context, treeUri)
                        ?.takeIf { it.isDirectory && it.exists() }
                }.getOrNull()
                val desiredName = downloadFileName(file.name, file.download_url, file.ext)
                // Resolve name clashes explicitly instead of relying on the
                // provider's auto-renaming: some providers overwrite, some
                // append a suffix, and the success toast should name the file
                // that actually exists.
                val prepared = prepareDownloadDestination(
                    tree = tree?.let(::DocumentFileSafDocument),
                    desiredName = desiredName,
                    mimeType = mimeTypeForExtension(file.ext) ?: "application/octet-stream",
                    unavailableMessage = context.getString(
                        R.string.settings_download_dir_unavailable,
                    ),
                    createFailedMessage = context.getString(
                        R.string.settings_download_dir_create_failed,
                    ),
                )
                val target = (prepared as DocumentFileSafDocument).document
                created = target
                // Providers may still adjust the display name (e.g. enforce an
                // extension); report what was actually created.
                val savedName = prepared.name ?: desiredName

                val builder = Request.Builder().url(url)
                downloadRequestHeaders(Api.requestKey, url, Api.baseUrl).forEach { (name, value) ->
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
                    val output = try {
                        resolver.openOutputStream(target.uri)
                    } catch (e: Exception) {
                        // A revoked grant (SecurityException), a deleted
                        // document (FileNotFoundException) or a full volume can
                        // surface here; classify before the generic catch.
                        throw DownloadFailureException(classifyWriteFailure(e), e.message, e)
                    } ?: throw DownloadFailureException(
                        DownloadFailureKind.Provider,
                        context.getString(R.string.settings_download_dir_create_failed),
                    )
                    // The cancellation check is captured once: currentCoroutineContext
                    // is suspend-only, while the streaming loop is a plain
                    // function so it can be unit tested with byte streams.
                    val coroutineContext = currentCoroutineContext()
                    body.byteStream().use { input ->
                        output.use {
                            streamToDocument(
                                input = input,
                                output = it,
                                totalBytes = body.contentLength().takeIf { length -> length > 0 },
                                onProgress = onProgress,
                                checkActive = { coroutineContext.ensureActive() },
                            )
                        }
                    }
                }
                savedName
            }.let { Result.success(it) }
        } catch (e: CancellationException) {
            // Never leave a half-written document behind, and never turn a
            // cancellation into a failure result.
            created?.let { runCatching { it.delete() } }
            throw e
        } catch (e: DownloadFailureException) {
            // Already classified (permission / space / provider / network);
            // keep the kind so the caller can name the cause. Do not let the
            // IOException branch below relabel it as a network error.
            created?.let { runCatching { it.delete() } }
            Result.failure(e)
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

/**
 * One progress sample for a SAF download.
 *
 * [totalBytes] is the response's Content-Length, or null when the server uses
 * chunked encoding / sends no length. [fraction] and [percent] are null in that
 * case so the UI can show an indeterminate bar instead of a fake 0 %.
 */
data class DownloadProgress(
    val bytesWritten: Long,
    val totalBytes: Long?,
) {
    /** Completed fraction (0f..1f), or null when the total size is unknown. */
    val fraction: Float?
        get() {
            val total = totalBytes ?: return null
            if (total <= 0L) return null
            return (bytesWritten.toDouble() / total).toFloat().coerceIn(0f, 1f)
        }

    /** Completed percentage (0..100), or null when the total size is unknown. */
    val percent: Int?
        get() = fraction?.let { (it * 100).roundToInt().coerceIn(0, 100) }
}

/**
 * Why a custom-directory download could not be written.
 *
 * Kept separate from the message because the history screen maps it to a
 * localized string, while the downloader only needs to classify the underlying
 * exception.
 */
enum class DownloadFailureKind {
    /** The persisted grant was revoked, or the provider denied the write. */
    Permission,

    /** The target volume is full (ENOSPC / quota exceeded). */
    NoSpace,

    /** The document provider failed for any other reason. */
    Provider,

    /** Reading the HTTP response body failed. */
    Network,
}

/**
 * A download failure whose cause [DownloadFailureKind] already names. The
 * message is diagnostic only; callers should translate [kind] for display.
 */
class DownloadFailureException(
    val kind: DownloadFailureKind,
    message: String? = null,
    cause: Throwable? = null,
) : Exception(message ?: kind.name, cause)

/** Lower-case fragments providers/volumes use for "no space left". */
private val NO_SPACE_MARKERS = listOf(
    "enospc",
    "no space left",
    "disk quota exceeded",
    "quota exceeded",
    "not enough space",
)

/** Lower-case fragments the kernel/provider layer uses for "denied". */
private val PERMISSION_MARKERS = listOf(
    "eacces",
    "eperm",
    "permission denied",
    "operation not permitted",
)

/**
 * Classifies an exception thrown while opening, writing to or flushing a SAF
 * document. [SecurityException] is explicit; everything else is inferred from
 * the message/cause chain because `ContentResolver` providers wrap errno in a
 * generic [IOException] (e.g. "write failed: ENOSPC (No space left on
 * device)"). Unrecognized failures are provider failures, never network ones:
 * this is only called on the write path.
 */
internal fun classifyWriteFailure(error: Throwable): DownloadFailureKind {
    if (error is SecurityException) return DownloadFailureKind.Permission
    val messages = generateSequence(error) { it.cause }
        .take(8)
        .mapNotNull { it.message?.lowercase() }
        .toList()
    return when {
        messages.any { message -> NO_SPACE_MARKERS.any(message::contains) } ->
            DownloadFailureKind.NoSpace
        messages.any { message -> PERMISSION_MARKERS.any(message::contains) } ->
            DownloadFailureKind.Permission
        else -> DownloadFailureKind.Provider
    }
}

/**
 * Rate limit for progress callbacks: a large file would otherwise emit one
 * update per 8 KB chunk. A sample passes at most once per [minIntervalNanos]
 * *or* [minBytes] written, and the first call always passes (the 0-byte start).
 *
 * Stateful but clock-free for testability: callers pass [shouldEmit]'s
 * `nowNanos`; tests drive it instead of sleeping.
 */
internal class DownloadProgressThrottle(
    private val minIntervalNanos: Long = 150_000_000L,
    private val minBytes: Long = 256L * 1024L,
) {
    private var first = true
    private var lastBytes = 0L
    private var lastNanos = 0L

    fun shouldEmit(bytesWritten: Long, nowNanos: Long): Boolean {
        val due = first ||
            bytesWritten - lastBytes >= minBytes ||
            nowNanos - lastNanos >= minIntervalNanos
        if (due) {
            first = false
            lastBytes = bytesWritten
            lastNanos = nowNanos
        }
        return due
    }
}

/**
 * Streams [input] into [output], counting bytes and reporting throttled
 * [DownloadProgress] samples.
 *
 * This runs on the caller's thread (the downloader uses `Dispatchers.IO`), so a
 * slow provider or network never blocks the main thread; [onProgress] is
 * therefore invoked from a background thread and must be thread-safe.
 *
 * Failures are classified by phase: a read error is [DownloadFailureKind.Network],
 * while open/write/flush errors go through [classifyWriteFailure]. [checkActive]
 * runs before every chunk; a cancelled coroutine's `CancellationException`
 * propagates unchanged so the caller can delete the partial document.
 *
 * Returns the final sample, whose total is [totalBytes] when known and the
 * written size otherwise (so a completed unknown-length download reads 100 %).
 */
internal fun streamToDocument(
    input: InputStream,
    output: OutputStream,
    totalBytes: Long?,
    onProgress: (DownloadProgress) -> Unit,
    checkActive: () -> Unit = {},
    throttle: DownloadProgressThrottle = DownloadProgressThrottle(),
    nowNanos: () -> Long = System::nanoTime,
): DownloadProgress {
    var written = 0L

    fun report() {
        if (throttle.shouldEmit(written, nowNanos())) {
            onProgress(DownloadProgress(written, totalBytes))
        }
    }

    // The first sample is the 0-byte start; the throttle always lets it pass.
    report()
    val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
    while (true) {
        checkActive()
        val read = try {
            input.read(buffer)
        } catch (e: IOException) {
            throw DownloadFailureException(
                DownloadFailureKind.Network,
                e.message,
                e,
            )
        }
        if (read <= 0) break
        try {
            output.write(buffer, 0, read)
        } catch (e: Exception) {
            throw DownloadFailureException(classifyWriteFailure(e), e.message, e)
        }
        written += read
        report()
    }
    try {
        output.flush()
    } catch (e: Exception) {
        throw DownloadFailureException(classifyWriteFailure(e), e.message, e)
    }
    val finalSample = DownloadProgress(written, totalBytes ?: written)
    onProgress(finalSample)
    return finalSample
}

/**
 * [SafDocument] view over a `DocumentFile`, so the destination rules in
 * [prepareDownloadDestination] stay testable on the JVM. Only documents
 * obtained from the provider are wrapped; [document] is unwrapped again for
 * the streaming `openOutputStream` call.
 */
private class DocumentFileSafDocument(val document: DocumentFile) : SafDocument {
    override val name: String? get() = document.name

    override val isDirectory: Boolean get() = document.isDirectory

    override val exists: Boolean get() = document.exists()

    override fun findDirectory(displayName: String): SafDocument? =
        document.findFile(displayName)?.let(::DocumentFileSafDocument)

    override fun createDirectory(displayName: String): SafDocument? =
        document.createDirectory(displayName)?.let(::DocumentFileSafDocument)

    override fun listChildNames(): List<String> = document.listFiles().mapNotNull { it.name }

    override fun createFile(mimeType: String, displayName: String): SafDocument? =
        document.createFile(mimeType, displayName)?.let(::DocumentFileSafDocument)

    override fun delete(): Boolean = document.delete()
}