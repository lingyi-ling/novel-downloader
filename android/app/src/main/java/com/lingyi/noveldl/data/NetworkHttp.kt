package com.lingyi.noveldl.data

import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.suspendCancellableCoroutine
import okio.Buffer
import okio.BufferedSource
import okio.ForwardingSource
import okio.buffer
import okhttp3.Call
import okhttp3.Callback
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.Interceptor
import okhttp3.MediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.ResponseBody
import java.io.ByteArrayOutputStream
import java.io.IOException
import kotlin.coroutines.resumeWithException

/**
 * Transport policy shared by the API client, the updater and the history
 * downloader.
 *
 * The app deliberately keeps `usesCleartextTraffic="true"`: the embedded
 * engine listens on `http://127.0.0.1` and self-hosted servers are commonly
 * reached over plain HTTP on a LAN, and a static network-security config
 * cannot enumerate arbitrary LAN addresses. The credential and downgrade
 * rules below therefore live in the request path instead of the manifest:
 *
 *  - the API key is attached only to the configured origin (scheme, host,
 *    port) and stripped from every redirect that leaves it;
 *  - an API configured over HTTPS refuses a redirect that downgrades the
 *    transport to plain HTTP.
 */

/** Upper bound for a regular JSON API response (search/browse/tasks lists). */
internal const val MAX_JSON_RESPONSE_BYTES: Long = 8L * 1024L * 1024L

/**
 * Upper bound for updater metadata (manifest / GitHub release). These bodies
 * are a few kilobytes; the cap only exists so a hostile endpoint cannot make
 * the app buffer an unbounded document.
 */
internal const val MAX_METADATA_RESPONSE_BYTES: Long = 2L * 1024L * 1024L

/**
 * How much of an error body is inspected for a `detail` field. Enough for
 * every server message the UI has to show, small enough that a multi-megabyte
 * HTML page cannot be buffered just to render "HTTP 500".
 */
internal const val MAX_ERROR_BODY_BYTES: Long = 64L * 1024L

/** Scheme + host + port of the configured API endpoint. */
internal data class ApiOrigin(val scheme: String, val host: String, val port: Int) {
    companion object {
        fun of(url: HttpUrl): ApiOrigin = ApiOrigin(url.scheme, url.host, url.port)
    }
}

/**
 * Marker for transport failures that are deterministic: replaying the request
 * would hit the same policy/size decision, so the retry interceptor must not
 * treat them as a flaky connection.
 */
internal interface NonRetryableTransportFailure

/**
 * Raised when an HTTPS endpoint tries to redirect to plain HTTP.
 *
 * It is an [IOException] on purpose: OkHttp's async path (used by Retrofit
 * suspend functions) wraps any other exception in `IOException("canceled due
 * to ...")`, which would lose the distinction. [Api.call] maps this type to
 * [ApiErrorCode.INSECURE_REDIRECT].
 */
internal class InsecureRedirectException(message: String) :
    IOException(message),
    NonRetryableTransportFailure

/**
 * Raised when a response declares or streams more bytes than the configured
 * cap. [Api.call] maps it to [ApiErrorCode.BAD_RESPONSE].
 */
internal class ResponseTooLargeException(message: String) :
    IOException(message),
    NonRetryableTransportFailure

/**
 * [ResponseBody] that fails as soon as [limit] bytes have been read, so a
 * chunked response (no `Content-Length`) cannot bypass the cap either.
 *
 * The delegate source is wrapped once and reused; the default
 * `ResponseBody.close()` therefore closes the delegate.
 */
internal class BoundedResponseBody(
    private val delegate: ResponseBody,
    private val limit: Long,
) : ResponseBody() {

    private val bounded: BufferedSource by lazy {
        object : ForwardingSource(delegate.source()) {
            private var total = 0L

            override fun read(sink: Buffer, byteCount: Long): Long {
                val read = super.read(sink, byteCount)
                if (read == -1L) return -1L
                total += read
                if (total > limit) {
                    throw ResponseTooLargeException("response body larger than $limit bytes")
                }
                return read
            }
        }.buffer()
    }

    override fun contentType(): MediaType? = delegate.contentType()

    override fun contentLength(): Long = delegate.contentLength()

    override fun source(): BufferedSource = bounded
}

/**
 * Network-level interceptor applying the origin rules above.
 *
 * It must be a *network* interceptor: OkHttp builds redirect requests from the
 * request that actually went over the wire, so an application interceptor
 * would only ever see the first URL and could not notice a cross-host
 * redirect.
 */
internal class ApiOriginPolicy(
    private val origin: ApiOrigin,
    private val apiKey: String,
) {
    fun interceptor(): Interceptor = Interceptor { chain ->
        val request = chain.request()
        val url = request.url
        if (origin.scheme == "https" && url.scheme != "https") {
            // The user configured an HTTPS server; following a redirect to
            // plain HTTP would expose the request (and its body) unencrypted.
            throw InsecureRedirectException(
                "refusing cleartext request to ${url.host}",
            )
        }
        val sameOrigin = url.host == origin.host && url.port == origin.port
        val builder = request.newBuilder()
        if (sameOrigin) {
            if (apiKey.isNotEmpty()) builder.header(DOWNLOAD_API_KEY_HEADER, apiKey)
        } else {
            // A redirect (or a server-supplied absolute download URL) must not
            // carry our credentials to another host.
            builder.removeHeader(DOWNLOAD_API_KEY_HEADER)
            builder.removeHeader("Authorization")
        }
        chain.proceed(builder.build())
    }
}

/** Builds the origin policy for [baseUrl]; `null` when it is unusable. */
internal fun apiOriginPolicy(baseUrl: String, apiKey: String): ApiOriginPolicy? =
    baseUrl.toHttpUrlOrNull()?.let { ApiOriginPolicy(ApiOrigin.of(it), apiKey) }

/**
 * Reads at most [limit] bytes of [this] body as UTF-8 (a diagnostic only).
 *
 * Unlike [Response.readBoundedText] this never throws for a large body: the
 * caller wants a best-effort snippet, and the connection is simply abandoned
 * (OkHttp will not return it to the pool).
 */
internal fun ResponseBody?.readAtMost(limit: Long = MAX_ERROR_BODY_BYTES): String {
    if (this == null) return ""
    return try {
        byteStream().use { input ->
            val out = ByteArrayOutputStream()
            val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
            var total = 0L
            while (total < limit) {
                val want = minOf(buffer.size.toLong(), limit - total).toInt()
                val read = input.read(buffer, 0, want)
                if (read <= 0) break
                out.write(buffer, 0, read)
                total += read
            }
            out.toString(Charsets.UTF_8.name())
        }
    } catch (_: Exception) {
        ""
    }
}

/**
 * Reads the whole body as UTF-8, failing when it exceeds [limit].
 *
 * `Content-Length` is checked first so a huge declared body is refused before
 * any buffering; the streaming counter covers chunked responses. Throws
 * [IOException] on overflow; the caller maps that to the transport/response
 * error it is handling.
 */
internal fun Response.readBoundedText(limit: Long): String {
    val body = this.body ?: return ""
    val announced = body.contentLength()
    if (announced > limit) {
        throw IOException("response body larger than $limit bytes")
    }
    body.byteStream().use { input ->
        val initial = if (announced in 1..limit) announced.toInt() else 8 * 1024
        val out = ByteArrayOutputStream(initial)
        val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
        var total = 0L
        while (true) {
            val read = input.read(buffer)
            if (read <= 0) break
            total += read
            if (total > limit) {
                throw IOException("response body larger than $limit bytes")
            }
            out.write(buffer, 0, read)
        }
        return out.toString(Charsets.UTF_8.name())
    }
}

/**
 * Executes [request] without blocking the calling thread and cancels the
 * underlying call when the coroutine is cancelled.
 *
 * `Call.execute()` cannot be interrupted: a cancelled coroutine would keep
 * downloading until the socket's read timeout, and the response body would
 * leak. `enqueue` gives cancellation a handle on the [Call]. The response is
 * delivered on the OkHttp dispatcher; callers already run inside
 * `withContext(Dispatchers.IO)`, so consuming the body stays off the UI
 * thread.
 */
// `resume(value, onCancellation)` is experimental but is the only way to
// close the response body exactly when a resume races a cancellation; the
// stable overload silently drops the value and would leak the connection.
@OptIn(ExperimentalCoroutinesApi::class)
internal suspend fun OkHttpClient.executeCancellable(request: Request): Response =
    suspendCancellableCoroutine { continuation ->
        val call: Call = newCall(request)
        continuation.invokeOnCancellation { call.cancel() }
        call.enqueue(
            object : Callback {
                override fun onFailure(call: Call, e: IOException) {
                    if (continuation.isActive) continuation.resumeWithException(e)
                }

                override fun onResponse(call: Call, response: Response) {
                    if (continuation.isActive) {
                        // Closing on cancellation avoids leaking the
                        // connection when the coroutine dies between the
                        // response arriving and the caller using it.
                        continuation.resume(response) { response.close() }
                    } else {
                        response.close()
                    }
                }
            },
        )
    }
