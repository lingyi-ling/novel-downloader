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

    fun configure(baseUrl: String, apiKey: String?) {
        val normalized = baseUrl.trim().ifEmpty { DEFAULT_BASE_URL }.let {
            if (it.endsWith("/")) it else "$it/"
        }
        this.baseUrl = normalized
        val key = apiKey?.trim().orEmpty()
        val client = OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(60, TimeUnit.SECONDS)
            .writeTimeout(60, TimeUnit.SECONDS)
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

    fun service(): ApiService =
        service ?: throw IllegalStateException("API client is not configured yet")

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
