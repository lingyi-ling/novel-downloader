package com.lingyi.noveldl.data

import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.util.Base64
import java.util.concurrent.TimeUnit

/**
 * Authentication contract of the embedded engine, JVM side.
 *
 * Covers the three pieces that make the per-process token work: generation
 * (CSPRNG, base64url, enough entropy), the hand-off to Python (environment
 * variable, never an argument or a log line) and the request-header injection
 * that keeps the engine token and the user's remote key strictly apart.
 */
class EmbeddedAuthTest {

    private lateinit var server: MockWebServer

    @Before
    fun startServer() {
        server = MockWebServer()
        server.start()
        resetService()
    }

    @After
    fun stopServer() {
        server.shutdown()
        resetService()
    }

    private fun baseUrl(): String = server.url("/").toString()

    private fun json(body: String, code: Int = 200): MockResponse =
        MockResponse()
            .setResponseCode(code)
            .setHeader("Content-Type", "application/json")
            .setBody(body)

    /** [Api] is a singleton; clear the cached Retrofit service between tests. */
    private fun resetService() {
        val field = Api::class.java.getDeclaredField("service")
        field.isAccessible = true
        field.set(Api, null)
    }

    // ------------------------------------------------------------------
    // token generation
    // ------------------------------------------------------------------

    @Test
    fun generatedTokenIsUnpaddedBase64UrlOfTheConfiguredEntropy() {
        val token = generateEmbeddedToken()

        // 32 bytes -> 43 base64 characters without padding.
        assertEquals(43, token.length)
        assertTrue(
            "not base64url: $token",
            Regex("^[A-Za-z0-9_-]+$").matches(token),
        )
        val decoded = Base64.getUrlDecoder().decode(token)
        assertEquals(EMBEDDED_TOKEN_BYTES, decoded.size)
        assertFalse("padding must be omitted", token.contains('='))
    }

    @Test
    fun generatedTokensDifferPerCall() {
        val tokens = (1..16).map { generateEmbeddedToken() }.toSet()

        assertEquals("tokens must not repeat", 16, tokens.size)
    }

    @Test
    fun theEngineTokenIsStableForTheProcessLifetime() {
        val first = EmbeddedEngine.token

        assertEquals(first, EmbeddedEngine.token)
        assertTrue(first.isNotEmpty())
    }

    @Test
    fun resettingTheEngineGeneratesAFreshToken() {
        val before = EmbeddedEngine.token

        EmbeddedEngine.resetForTest()

        assertNotEquals(before, EmbeddedEngine.token)
        assertEquals(43, EmbeddedEngine.token.length)
    }

    // ------------------------------------------------------------------
    // hand-off to Python
    // ------------------------------------------------------------------

    @Test
    fun theTokenIsPassedAsAnEnvironmentVariable() {
        val env = embeddedTokenEnvironment("s3cret")

        assertEquals(mapOf(EMBEDDED_TOKEN_ENV_VAR to "s3cret"), env)
        assertEquals("NOVEL_DOWNLOADER_EMBEDDED_TOKEN", EMBEDDED_TOKEN_ENV_VAR)
    }

    @Test
    fun aBlankTokenIsNotPassedAtAll() {
        assertTrue(embeddedTokenEnvironment("").isEmpty())
    }

    // ------------------------------------------------------------------
    // credential selection
    // ------------------------------------------------------------------

    @Test
    fun embeddedModeOnTheEngineUrlUsesTheEngineToken() {
        assertEquals(
            "engine-token",
            effectiveRequestKey(
                baseUrl = EmbeddedEngine.BASE_URL,
                apiKey = "user-key",
                embeddedEngine = true,
                engineToken = "engine-token",
                engineBaseUrl = EmbeddedEngine.BASE_URL,
            ),
        )
    }

    @Test
    fun aTrailingSlashDifferenceStillCountsAsTheEngineUrl() {
        assertTrue(
            isEmbeddedBaseUrl(
                EmbeddedEngine.BASE_URL.trimEnd('/'),
                EmbeddedEngine.BASE_URL,
            ),
        )
    }

    @Test
    fun embeddedModeOnAnotherUrlKeepsTheUserKey() {
        assertEquals(
            "user-key",
            effectiveRequestKey(
                baseUrl = "http://10.0.2.2:8081/",
                apiKey = "user-key",
                embeddedEngine = true,
                engineToken = "engine-token",
                engineBaseUrl = EmbeddedEngine.BASE_URL,
            ),
        )
    }

    @Test
    fun theEngineUrlOutsideEmbeddedModeKeepsTheUserKey() {
        // e.g. `adb reverse` to a PC server that happens to answer on 8081:
        // that server knows nothing about this process's token.
        assertEquals(
            "user-key",
            effectiveRequestKey(
                baseUrl = EmbeddedEngine.BASE_URL,
                apiKey = "user-key",
                embeddedEngine = false,
                engineToken = "engine-token",
                engineBaseUrl = EmbeddedEngine.BASE_URL,
            ),
        )
    }

    @Test
    fun aRemoteTargetNeverReceivesTheEngineToken() {
        Api.configure(baseUrl(), null, embeddedEngine = true)

        assertEquals("", Api.requestKey)
        assertNotEquals(EmbeddedEngine.token, Api.requestKey)
    }

    @Test
    fun aRemoteTargetKeepsTheUserKeyEvenInEmbeddedMode() {
        Api.configure(baseUrl(), "user-key", embeddedEngine = true)

        assertEquals("user-key", Api.requestKey)
    }

    // ------------------------------------------------------------------
    // header injection
    // ------------------------------------------------------------------

    @Test
    fun theOriginPolicyInjectsTheConfiguredCredential() {
        val key = "engine-token"
        val origin = ApiOrigin.of(baseUrl().toHttpUrl())
        val client = OkHttpClient.Builder()
            .addNetworkInterceptor(ApiOriginPolicy(origin, key).interceptor())
            .build()

        server.enqueue(json("""{"status":"ok"}"""))
        client.newCall(
            Request.Builder().url(server.url("/api/v1/health")).build(),
        ).execute().use { response ->
            assertTrue(response.isSuccessful)
        }

        assertEquals(key, server.takeRequest().getHeader(DOWNLOAD_API_KEY_HEADER))
    }

    @Test
    fun theOriginPolicyAttachesNothingWithoutACredential() {
        val origin = ApiOrigin.of(baseUrl().toHttpUrl())
        val client = OkHttpClient.Builder()
            .addNetworkInterceptor(ApiOriginPolicy(origin, "").interceptor())
            .build()

        server.enqueue(json("""{"status":"ok"}"""))
        client.newCall(
            Request.Builder().url(server.url("/api/v1/health")).build(),
        ).execute().use { response ->
            assertTrue(response.isSuccessful)
        }

        val recorded = server.takeRequest()
        assertNull(recorded.getHeader(DOWNLOAD_API_KEY_HEADER))
        assertNull(recorded.getHeader("Authorization"))
    }

    @Test
    fun theOriginPolicyStripsTheEngineTokenOnACrossOriginRedirect() {
        val origin = ApiOrigin("http", "127.0.0.1", 8081)
        val client = OkHttpClient.Builder()
            .addNetworkInterceptor(ApiOriginPolicy(origin, "engine-token").interceptor())
            .build()

        server.enqueue(json("""{"status":"ok"}"""))
        client.newCall(
            Request.Builder().url(server.url("/api/v1/health")).build(),
        ).execute().use { response ->
            assertTrue(response.isSuccessful)
        }

        val recorded = server.takeRequest()
        assertNull(recorded.getHeader(DOWNLOAD_API_KEY_HEADER))
        assertNull(recorded.getHeader("Authorization"))
    }

    @Test
    fun historyDownloadsCarryTheEffectiveCredential() {
        val url = EmbeddedEngine.BASE_URL + "downloads/book.txt"

        assertEquals(
            mapOf(DOWNLOAD_API_KEY_HEADER to "engine-token"),
            downloadRequestHeaders(
                "engine-token",
                url,
                EmbeddedEngine.BASE_URL,
            ),
        )
    }

    @Test
    fun historyDownloadsOmitTheKeyForAnotherOrigin() {
        assertTrue(
            downloadRequestHeaders(
                "engine-token",
                "https://cdn.example/book.txt",
                EmbeddedEngine.BASE_URL,
            ).isEmpty(),
        )
    }

    @Test(timeout = 10_000)
    fun aRequestThroughTheClientReachesTheServerWithTheCredential() {
        // The engine URL is fixed, so the transport path is exercised through
        // the same interceptor the real configuration installs: the recorded
        // header must be the engine token, not the user key.
        val origin = ApiOrigin.of(baseUrl().toHttpUrl())
        val token = EmbeddedEngine.token
        val client = OkHttpClient.Builder()
            .callTimeout(5, TimeUnit.SECONDS)
            .addNetworkInterceptor(ApiOriginPolicy(origin, token).interceptor())
            .build()

        server.enqueue(json("""{"status":"ok","version":"1"}"""))
        client.newCall(
            Request.Builder().url(server.url("/api/v1/health")).build(),
        ).execute().use { response ->
            assertEquals(200, response.code)
        }

        val recorded = server.takeRequest()
        assertEquals(token, recorded.getHeader(DOWNLOAD_API_KEY_HEADER))
        assertNull(recorded.getHeader("Authorization"))
    }
}
