package com.lingyi.noveldl.data

import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * `GET /api/v1/music/health` contract tests backed by MockWebServer: query
 * parameters (platform + timeout), parsing of ok / failed probes including
 * the error field, and mapping of the music-unavailable 503 to a message.
 */
class MusicHealthApiTest {

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

    @Test
    fun healthUsesGetWithPlatformAndTimeoutQuery() = runBlocking {
        server.enqueue(json("""[{"platform":"netease","ok":true,"elapsed_ms":812,"error":""}]"""))
        Api.configure(baseUrl(), null)

        val result = Api.call { Api.service().musicHealth("netease", 5) }

        assertTrue(result.isSuccess)
        val request = server.takeRequest()
        assertEquals("GET", request.method)
        val url = request.requestUrl!!
        assertEquals("/api/v1/music/health", url.encodedPath)
        assertEquals("netease", url.queryParameter("platform"))
        assertEquals("5", url.queryParameter("timeout"))
    }

    @Test
    fun healthParsesOkProbeWithElapsedTime() = runBlocking {
        server.enqueue(json("""[{"platform":"netease","ok":true,"elapsed_ms":812,"error":""}]"""))
        Api.configure(baseUrl(), null)

        val probes = Api.call { Api.service().musicHealth("netease", 5) }.getOrThrow()

        assertEquals(1, probes.size)
        val probe = probes.single()
        assertEquals("netease", probe.platform)
        assertTrue(probe.ok)
        assertEquals(812L, probe.elapsed_ms)
        assertEquals("", probe.error)
    }

    @Test
    fun healthParsesFailedProbeWithServerErrorText() = runBlocking {
        server.enqueue(
            json("""[{"platform":"qq","ok":false,"elapsed_ms":5003,"error":"搜索超时（>5s）"}]"""),
        )
        Api.configure(baseUrl(), null)

        val probes = Api.call { Api.service().musicHealth("qq", 5) }.getOrThrow()

        val probe = probes.single()
        assertEquals("qq", probe.platform)
        assertFalse(probe.ok)
        assertEquals(5003L, probe.elapsed_ms)
        assertEquals("搜索超时（>5s）", probe.error)
    }

    @Test
    fun healthMissingFieldsDefaultAndUnknownFieldsAreIgnored() = runBlocking {
        // The array shape must survive probes that omit `error`/`elapsed_ms`
        // or carry extra fields.
        server.enqueue(
            json("""[{"platform":"netease","ok":true,"extra":1},{"ok":false,"platform":"qq"}]"""),
        )
        Api.configure(baseUrl(), null)

        val probes = Api.call { Api.service().musicHealth("netease", 5) }.getOrThrow()

        assertEquals(2, probes.size)
        assertTrue(probes[0].ok)
        assertEquals(0L, probes[0].elapsed_ms)
        assertEquals("", probes[0].error)
        assertFalse(probes[1].ok)
        assertEquals("qq", probes[1].platform)
    }

    @Test
    fun health503WithoutMusicSupportSurfacesTheInstallHint() = runBlocking {
        // The real server 503s with an install hint; inner quotes are avoided
        // because Api's error mapping reads the detail with a plain regex.
        val detail = "Music support is not available. Install it with: pip install novel-downloader[music]"
        server.enqueue(json("""{"detail":"$detail"}""", 503))
        Api.configure(baseUrl(), null)

        val result = Api.call { Api.service().musicHealth("netease", 5) }

        assertTrue(result.isFailure)
        assertEquals(detail, result.exceptionOrNull()?.message)
    }
}
