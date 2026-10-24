package com.lingyi.noveldl.data

import com.jakewharton.retrofit2.converter.kotlinx.serialization.asConverterFactory
import kotlinx.serialization.json.Json
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
    ) {
        val normalized = baseUrl.trim().ifEmpty { DEFAULT_BASE_URL }.let {
            if (it.endsWith("/")) it else "$it/"
        }
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
    }

    /** Whether [configure] has been called at least once. */
    fun isConfigured(): Boolean = service != null

    fun service(): ApiService = service ?: throw IllegalStateException(NOT_CONFIGURED_MESSAGE)

    /** Runs [block] and converts exceptions into a user-readable [Result]. */
    suspend fun <T> call(block: suspend () -> T): Result<T> = try {
        Result.success(block())
    } catch (e: HttpException) {
        Result.failure(IllegalStateException(readableError(e), e))
    } catch (e: IOException) {
        Result.failure(IllegalStateException("网络错误：${e.message ?: e.javaClass.simpleName}", e))
    } catch (e: Exception) {
        Result.failure(IllegalStateException(e.message ?: e.javaClass.simpleName, e))
    }

    private fun readableError(e: HttpException): String {
        val body = try {
            e.response()?.errorBody()?.string().orEmpty()
        } catch (_: Exception) {
            ""
        }
        val detail = Regex("\"detail\"\\s*:\\s*\"([^\"]*)\"")
            .find(body)
            ?.groupValues
            ?.getOrNull(1)
        return detail ?: "HTTP ${e.code()}"
    }
}
