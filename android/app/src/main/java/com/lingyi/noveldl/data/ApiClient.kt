package com.lingyi.noveldl.data

import com.jakewharton.retrofit2.converter.kotlinx.serialization.asConverterFactory
import kotlinx.coroutines.CancellationException
import kotlinx.serialization.SerializationException
import kotlinx.serialization.json.Json
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import retrofit2.HttpException
import retrofit2.Retrofit
import java.io.IOException
import java.util.concurrent.TimeUnit

/** Holds the Retrofit service and maps transport/HTTP failures to readable messages. */
object Api {
    const val DEFAULT_BASE_URL: String = "http://10.0.2.2:8081/"

    /** Shown when the UI touches the API before the user configured it. */
    const val NOT_CONFIGURED_MESSAGE: String = "API 未配置，请先在「设置」中填写 API 地址"

    /** Error bodies longer than this are not scanned for a `detail` field. */
    private const val MAX_ERROR_BODY_CHARS = 100_000

    private val json = Json {
        ignoreUnknownKeys = true
        explicitNulls = false
        encodeDefaults = false
    }

    @Volatile
    private var service: ApiService? = null

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
     * The timeout parameters keep the defaults used in the app and exist so the
     * transport behaviour can be exercised from unit tests.
     */
    @JvmOverloads
    fun configure(
        baseUrl: String,
        apiKey: String?,
        connectTimeoutMillis: Long = 10_000,
        readTimeoutMillis: Long = 60_000,
        writeTimeoutMillis: Long = 60_000,
    ): Boolean {
        val trimmed = baseUrl.trim()
        val candidate = if (trimmed.endsWith("/")) trimmed else "$trimmed/"
        val usable = trimmed.isNotEmpty() && candidate.toHttpUrlOrNull() != null
        val normalized = if (usable) candidate else DEFAULT_BASE_URL
        this.baseUrl = normalized
        val key = apiKey?.trim().orEmpty()
        this.apiKey = key
        val client = OkHttpClient.Builder()
            .connectTimeout(connectTimeoutMillis, TimeUnit.MILLISECONDS)
            .readTimeout(readTimeoutMillis, TimeUnit.MILLISECONDS)
            .writeTimeout(writeTimeoutMillis, TimeUnit.MILLISECONDS)
            .addInterceptor { chain ->
                val builder = chain.request().newBuilder()
                if (key.isNotEmpty()) builder.header("X-API-Key", key)
                chain.proceed(builder.build())
            }
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

    fun service(): ApiService = service ?: throw IllegalStateException(NOT_CONFIGURED_MESSAGE)

    /**
     * Runs [block] and converts exceptions into a user-readable [Result].
     *
     * [CancellationException] is rethrown so that cancelled coroutines (a
     * LaunchedEffect restarting its poll, navigating away mid-request) stay
     * cancelled instead of resolving to a bogus failure result.
     */
    suspend fun <T> call(block: suspend () -> T): Result<T> = try {
        Result.success(block())
    } catch (e: CancellationException) {
        throw e
    } catch (e: HttpException) {
        Result.failure(IllegalStateException(readableError(e), e))
    } catch (e: SerializationException) {
        // A 200 response whose body is not the expected JSON (proxy/captive
        // portal HTML, empty bodies) surfaces as a decoding error; report it
        // in the app's language instead of a raw kotlinx message.
        val raw = e.message?.take(160)?.ifBlank { "非 JSON 响应" } ?: "非 JSON 响应"
        Result.failure(IllegalStateException("响应格式错误：$raw", e))
    } catch (e: IOException) {
        Result.failure(IllegalStateException("网络错误：${e.message ?: e.javaClass.simpleName}", e))
    } catch (e: Exception) {
        Result.failure(IllegalStateException(e.message ?: e.javaClass.simpleName, e))
    }

    private fun readableError(e: HttpException): String {
        val body = try {
            e.response()?.errorBody()?.string().orEmpty().take(MAX_ERROR_BODY_CHARS)
        } catch (_: Exception) {
            ""
        }
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
