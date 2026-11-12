package com.lingyi.noveldl.data

import com.jakewharton.retrofit2.converter.kotlinx.serialization.asConverterFactory
import kotlinx.coroutines.CancellationException
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.Interceptor
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import retrofit2.HttpException
import retrofit2.Retrofit
import java.io.IOException
import java.io.InterruptedIOException
import java.net.UnknownHostException
import java.util.concurrent.TimeUnit
import javax.net.ssl.SSLException

/**
 * Holds the Retrofit service and classifies failures into [ApiErrorCode]s.
 *
 * No user-facing text lives here: the UI resolves the code through
 * `apiErrorMessage(...)` so both languages stay in `strings.xml`.
 */
object Api {
    const val DEFAULT_BASE_URL: String = "http://10.0.2.2:8081/"

    /**
     * Default overall deadline for one API call. `readTimeout` only bounds a
     * single socket read, so a server that trickles one byte per read could
     * otherwise keep a request alive indefinitely.
     */
    const val DEFAULT_CALL_TIMEOUT_MILLIS: Long = 120_000

    /**
     * Shared factory for every client produced by [configure]. `newBuilder()`
     * keeps the dispatcher and connection pool, so re-configuring the app
     * (settings save, standalone toggle, process start) does not strand a
     * thread pool per previous configuration.
     */
    private val baseClient: OkHttpClient = OkHttpClient.Builder()
        // OkHttp's own retry can replay a request that was written but got no
        // response. For a non-idempotent POST (adding a task, submitting a
        // login, exporting) a blind replay can duplicate the operation, so it
        // is disabled here and re-added for GET/HEAD only by
        // [idempotentRetryInterceptor].
        .retryOnConnectionFailure(false)
        .build()

    private val json = Json {
        ignoreUnknownKeys = true
        explicitNulls = false
        encodeDefaults = false
    }

    @Volatile
    private var service: ApiService? = null

    /**
     * Response-size ceiling enforced before the converter buffers a body.
     * Exposed to unit tests so the guard can be exercised without allocating a
     * multi-megabyte payload.
     */
    @Volatile
    internal var maxResponseBytes: Long = MAX_JSON_RESPONSE_BYTES

    @Volatile
    var baseUrl: String = DEFAULT_BASE_URL
        private set

    /** Trimmed API key of the active configuration; empty when unauthenticated. */
    @Volatile
    var apiKey: String = ""
        private set

    /**
     * Builds the Retrofit service. [baseUrl] is trimmed and normalized to end
     * with a slash; a blank value falls back to [DEFAULT_BASE_URL].
     *
     * A non-blank value that is not a usable `http(s)` URL (e.g. user-typed
     * text like "my pc") would make Retrofit's builder throw, crashing the
     * caller's coroutine; such input is contained by falling back to
     * [DEFAULT_BASE_URL] as well. Returns whether the passed URL (after the
     * blank fallback) was used as-is — `false` means the input was malformed
     * and the default is now active.
     *
     * Timeouts: connect 10 s, read/write 60 s (a search can legitimately take
     * a while; polling requests are short) and one overall call deadline of
     * [callTimeoutMillis]. They are parameters so the transport behaviour can
     * be exercised from unit tests.
     */
    @JvmOverloads
    fun configure(
        baseUrl: String,
        apiKey: String?,
        connectTimeoutMillis: Long = 10_000,
        readTimeoutMillis: Long = 60_000,
        writeTimeoutMillis: Long = 60_000,
        callTimeoutMillis: Long = DEFAULT_CALL_TIMEOUT_MILLIS,
    ): Boolean {
        val trimmed = baseUrl.trim()
        val candidate = if (trimmed.endsWith("/")) trimmed else "$trimmed/"
        val parsed = candidate.toHttpUrlOrNull()
        val usable = trimmed.isNotEmpty() && parsed != null
        val normalized = if (usable) candidate else DEFAULT_BASE_URL
        this.baseUrl = normalized
        val key = apiKey?.trim().orEmpty()
        this.apiKey = key
        val origin = ApiOrigin.of(normalized.toHttpUrl())
        val client = baseClient.newBuilder()
            .connectTimeout(connectTimeoutMillis, TimeUnit.MILLISECONDS)
            .readTimeout(readTimeoutMillis, TimeUnit.MILLISECONDS)
            .writeTimeout(writeTimeoutMillis, TimeUnit.MILLISECONDS)
            .callTimeout(callTimeoutMillis, TimeUnit.MILLISECONDS)
            .addInterceptor(idempotentRetryInterceptor())
            .addInterceptor(responseSizeGuard())
            // Network (not application) interceptor: redirects must be seen so
            // the key is not forwarded to another origin.
            .addNetworkInterceptor(ApiOriginPolicy(origin, key).interceptor())
            .build()
        service = Retrofit.Builder()
            .baseUrl(normalized)
            .client(client)
            .addConverterFactory(json.asConverterFactory("application/json".toMediaType()))
            .build()
            .create(ApiService::class.java)
        return usable
    }

    /** Whether [configure] has been called at least once. */
    fun isConfigured(): Boolean = service != null

    fun service(): ApiService =
        service ?: throw ApiFailure(ApiErrorCode.NOT_CONFIGURED)

    /**
     * Runs [block] and converts exceptions into a user-readable [Result].
     *
     * Failures are [ApiFailure]s carrying an [ApiErrorCode]; [ApiFailure]
     * itself is passed through unchanged so a nested `Api.call` (or
     * [service] throwing "not configured") keeps its original code.
     *
     * [CancellationException] is rethrown so that cancelled coroutines (a
     * LaunchedEffect restarting its poll, navigating away mid-request) stay
     * cancelled instead of resolving to a bogus failure result.
     */
    suspend fun <T> call(block: suspend () -> T): Result<T> = try {
        Result.success(block())
    } catch (e: CancellationException) {
        throw e
    } catch (e: ApiFailure) {
        Result.failure(e)
    } catch (e: HttpException) {
        Result.failure(ApiFailure(ApiErrorCode.HTTP, readableError(e), e))
    } catch (e: SerializationException) {
        // A 200 response whose body is not the expected JSON (proxy/captive
        // portal HTML, empty bodies) surfaces as a decoding error; keep the
        // decoder's own text as the detail, the UI supplies the sentence.
        val raw = e.message?.take(160)?.ifBlank { "not JSON" } ?: "not JSON"
        Result.failure(ApiFailure(ApiErrorCode.BAD_RESPONSE, raw, e))
    } catch (e: ResponseTooLargeException) {
        Result.failure(
            ApiFailure(ApiErrorCode.BAD_RESPONSE, e.message ?: "response too large", e),
        )
    } catch (e: InsecureRedirectException) {
        Result.failure(
            ApiFailure(ApiErrorCode.INSECURE_REDIRECT, e.message ?: "insecure redirect", e),
        )
    } catch (e: IOException) {
        Result.failure(transportFailure(baseUrl, e))
    } catch (e: Exception) {
        Result.failure(
            ApiFailure(
                ApiErrorCode.UNKNOWN,
                e.message ?: e.javaClass.simpleName,
                e,
            ),
        )
    }

    /**
     * Classifies a transport failure. A loopback request can only fail because
     * the embedded engine has not bound its port yet (or died), which is a
     * different situation from a wrong/unreachable remote address.
     *
     * Outside the loopback case the socket exception is mapped to the most
     * specific [ApiErrorCode]: [ApiErrorCode.TIMEOUT] for any interruptible
     * timeout (connect/read/call), [ApiErrorCode.DNS] for an unresolvable
     * host, [ApiErrorCode.TLS] for a failed handshake, and
     * [ApiErrorCode.NETWORK] for everything else.
     */
    internal fun transportFailure(baseUrl: String, error: IOException): ApiFailure {
        val detail = error.message ?: error.javaClass.simpleName
        if (baseUrl == EmbeddedEngine.BASE_URL) {
            return ApiFailure(ApiErrorCode.ENGINE_UNREACHABLE, detail, error)
        }
        val code = when (error) {
            // SocketTimeoutException (connect/read) and OkHttp's call timeout
            // both extend InterruptedIOException.
            is InterruptedIOException -> ApiErrorCode.TIMEOUT
            is UnknownHostException -> ApiErrorCode.DNS
            is SSLException -> ApiErrorCode.TLS
            else -> ApiErrorCode.NETWORK
        }
        return ApiFailure(code, detail, error)
    }

    private fun idempotentRetryInterceptor(): Interceptor = Interceptor { chain ->
        val request = chain.request()
        val replayable = request.method == "GET" || request.method == "HEAD"
        try {
            chain.proceed(request)
        } catch (e: IOException) {
            // One retry for safe methods only. Timeouts and TLS failures are
            // not retried: the connection already had its chance and a second
            // wait would only delay the user's error message.
            val mayRetry = replayable && !chain.call().isCanceled() &&
                e !is NonRetryableTransportFailure &&
                e !is InterruptedIOException && e !is SSLException &&
                e !is java.net.ProtocolException
            if (!mayRetry) throw e
            chain.proceed(request)
        }
    }

    /**
     * Refuses oversized success bodies before the JSON converter buffers them.
     *
     * A declared `Content-Length` is checked up front; the returned body is
     * also wrapped in [BoundedResponseBody] so a chunked response (which has
     * no length) fails once it crosses the same limit.
     */
    private fun responseSizeGuard(): Interceptor = Interceptor { chain ->
        val response = chain.proceed(chain.request())
        val limit = maxResponseBytes
        val body = response.body ?: return@Interceptor response
        if (body.contentLength() > limit) {
            response.close()
            throw ResponseTooLargeException("response body larger than $limit bytes")
        }
        response.newBuilder()
            .body(BoundedResponseBody(body, limit))
            .build()
    }

    private fun readableError(e: HttpException): String {
        // Bounded: the message is rendered as one line, but an error endpoint
        // (or a proxy) can otherwise make us buffer the whole body.
        val body = e.response()?.errorBody().readAtMost(MAX_ERROR_BODY_BYTES)
        val detail = Regex("\"detail\"\\s*:\\s*\"((?:[^\"\\\\]|\\\\.)*)\"")
            .find(body)
            ?.groupValues
            ?.getOrNull(1)
            ?.let(::unescapeJsonString)
        return detail ?: "HTTP ${e.code()}"
    }

    /** Best-effort JSON string unescaping for error details. */
    private fun unescapeJsonString(raw: String): String = buildString(raw.length) {
        var i = 0
        while (i < raw.length) {
            val c = raw[i]
            if (c == '\\' && i + 1 < raw.length) {
                when (val next = raw[i + 1]) {
                    'n' -> {
                        append('\n'); i += 2
                    }
                    't' -> {
                        append('\t'); i += 2
                    }
                    'r' -> {
                        append('\r'); i += 2
                    }
                    'b' -> {
                        append('\b'); i += 2
                    }
                    'u' -> {
                        val hex = if (i + 6 <= raw.length) raw.substring(i + 2, i + 6) else null
                        val code = hex?.let { runCatching { it.toInt(16) }.getOrNull() }
                        if (code != null) {
                            append(code.toChar())
                            i += 6
                        } else {
                            append(next); i += 2
                        }
                    }
                    else -> {
                        append(next); i += 2
                    }
                }
            } else {
                append(c); i += 1
            }
        }
    }
}
