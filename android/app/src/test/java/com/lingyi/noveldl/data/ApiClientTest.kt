package com.lingyi.noveldl.data

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okhttp3.mockwebserver.SocketPolicy
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.util.concurrent.TimeUnit

/**
 * Transport level tests for [Api] backed by MockWebServer: request shape,
 * header injection, error mapping and failure containment.
 */
class ApiClientTest {

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
    fun healthUsesGetOnTheApiPath() = runBlocking {
        server.enqueue(json("""{"status":"ok","version":"1.0"}"""))
        Api.configure(baseUrl(), null)

        val result = Api.call { Api.service().health() }

        assertTrue(result.isSuccess)
        assertEquals("ok", result.getOrNull()?.status ?: "")
        assertEquals("1.0", result.getOrNull()?.version ?: "")
        val request = server.takeRequest()
        assertEquals("GET", request.method)
        assertEquals("/api/v1/health", request.path)
    }

    @Test
    fun sitesEndpointUsesGetAndParsesSearchAndDownloadMaps() = runBlocking {
        server.enqueue(
            json(
                """{"search":{"qidian":"起点","biquge5":"笔趣阁"},""" +
                    """"download":{"qidian":"起点"}}""",
            ),
        )
        Api.configure(baseUrl(), null)

        val result = Api.call { Api.service().sites() }

        assertTrue(result.isSuccess)
        assertEquals("起点", result.getOrNull()?.search?.get("qidian"))
        assertEquals("笔趣阁", result.getOrNull()?.search?.get("biquge5"))
        assertEquals("起点", result.getOrNull()?.download?.get("qidian"))
        val request = server.takeRequest()
        assertEquals("GET", request.method)
        assertEquals("/api/v1/sites", request.path)
    }

    @Test
    fun searchUsesPostWithJsonBody() = runBlocking {
        server.enqueue(json("""{"total":0,"results":[]}"""))
        Api.configure(baseUrl(), null)

        val result = Api.call {
            Api.service().search(SearchRequest(keyword = "斗破苍穹", sites = listOf("qidian")))
        }

        assertTrue(result.isSuccess)
        val request = server.takeRequest()
        assertEquals("POST", request.method)
        assertEquals("/api/v1/search", request.path)
        assertTrue(request.getHeader("Content-Type")!!.startsWith("application/json"))
        val body = request.body.readUtf8()
        assertTrue(body.contains("\"keyword\":\"斗破苍穹\""))
        assertTrue(body.contains("qidian"))
    }

    @Test
    fun taskAndMusicEndpointsUseExpectedPaths() = runBlocking {
        server.enqueue(json("""{"task_id":"t1"}"""))
        server.enqueue(json("""{"ok":true}"""))
        server.enqueue(json("""{"total":0,"files":[]}"""))
        server.enqueue(json("""{"platforms":[]}"""))
        server.enqueue(json("""{"job_id":"j1"}"""))
        server.enqueue(json("""{"job_id":"j1","status":"running"}"""))
        Api.configure(baseUrl(), null)

        Api.call { Api.service().addTask(AddTaskRequest(site = "qidian", book_id = "1")) }
        Api.call { Api.service().cancelTask("t1") }
        Api.call { Api.service().history() }
        Api.call { Api.service().musicPlatforms() }
        Api.call {
            Api.service().createMusicJob(
                MusicJobRequest(platform = "netease", tracks = emptyList()),
            )
        }
        Api.call { Api.service().musicJob("j1") }

        val addTask = server.takeRequest()
        assertEquals("POST", addTask.method)
        assertEquals("/api/v1/tasks", addTask.path)
        val cancel = server.takeRequest()
        assertEquals("POST", cancel.method)
        assertEquals("/api/v1/tasks/t1/cancel", cancel.path)
        val history = server.takeRequest()
        assertEquals("GET", history.method)
        assertEquals("/api/v1/history", history.path)
        val platforms = server.takeRequest()
        assertEquals("GET", platforms.method)
        assertEquals("/api/v1/music/platforms", platforms.path)
        val createJob = server.takeRequest()
        assertEquals("POST", createJob.method)
        assertEquals("/api/v1/music/jobs", createJob.path)
        assertEquals("/api/v1/music/jobs/j1", server.takeRequest().path)
    }

    @Test
    fun apiKeyHeaderIsAttachedAndTrimmed() = runBlocking {
        server.enqueue(json("""{"status":"ok","version":"1.0"}"""))
        Api.configure(baseUrl(), "  secret-key  ")

        Api.call { Api.service().health() }

        assertEquals("secret-key", server.takeRequest().getHeader("X-API-Key"))
    }

    @Test
    fun apiKeyHeaderIsAbsentWhenNotConfigured() = runBlocking {
        server.enqueue(json("""{"status":"ok","version":"1.0"}"""))
        Api.configure(baseUrl(), "   ")

        Api.call { Api.service().health() }

        assertNull(server.takeRequest().getHeader("X-API-Key"))
    }

    @Test
    fun errorDetailBecomesReadableMessage() = runBlocking {
        server.enqueue(json("""{"detail":"Invalid or missing API token"}""", 401))
        Api.configure(baseUrl(), "wrong")

        val result = Api.call { Api.service().health() }

        assertTrue(result.isFailure)
        assertEquals("Invalid or missing API token", result.exceptionOrNull()?.message ?: "")
    }

    @Test
    fun errorWithoutDetailFallsBackToHttpStatus() = runBlocking {
        server.enqueue(json("""{"oops":true}""", 500))
        Api.configure(baseUrl(), null)

        val result = Api.call { Api.service().health() }

        assertTrue(result.isFailure)
        assertEquals("HTTP 500", result.exceptionOrNull()?.message ?: "")
    }

    @Test
    fun unknownJsonFieldsAreIgnoredAndMissingOnesDefaulted() = runBlocking {
        server.enqueue(
            json("""{"status":"ok","version":"1.0","extra":{"a":1},"count":7,"list":[1,2]}"""),
        )
        server.enqueue(json("""{}"""))
        Api.configure(baseUrl(), null)

        val first = Api.call { Api.service().health() }
        val second = Api.call { Api.service().health() }

        assertTrue(first.isSuccess)
        assertEquals("ok", first.getOrNull()?.status ?: "")
        assertTrue(second.isSuccess)
        assertEquals("", second.getOrNull()?.status ?: "")
        assertEquals("", second.getOrNull()?.version ?: "")
    }

    @Test
    fun baseUrlIsNormalizedWithTrailingSlash() {
        val withoutSlash = server.url("/api").toString().removeSuffix("/")
        Api.configure(withoutSlash, null)

        assertEquals(server.url("/api/").toString(), Api.baseUrl)
        assertTrue(Api.isConfigured())
    }

    @Test
    fun baseUrlWithoutSlashStillResolvesPrefixedPaths() = runBlocking {
        server.enqueue(json("""{"status":"ok","version":"1.0"}"""))
        Api.configure(server.url("/api").toString().removeSuffix("/"), null)

        Api.call { Api.service().health() }

        assertEquals("/api/api/v1/health", server.takeRequest().path)
    }

    @Test
    fun blankBaseUrlFallsBackToDefault() {
        Api.configure("   ", null)

        assertEquals(Api.DEFAULT_BASE_URL, Api.baseUrl)
    }

    @Test
    fun readTimeoutBecomesFailureInsteadOfCrash() = runBlocking {
        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.NO_RESPONSE))
        Api.configure(baseUrl(), null, connectTimeoutMillis = 1_000, readTimeoutMillis = 300)

        val result = Api.call { Api.service().health() }

        assertTrue(result.isFailure)
        val message = result.exceptionOrNull()?.message ?: ""
        assertTrue("unexpected message: $message", message.startsWith("网络错误"))
    }

    @Test
    fun ioFailureBecomesFailureInsteadOfCrash() = runBlocking {
        val url = baseUrl()
        server.shutdown()
        Api.configure(url, null)

        val result = Api.call { Api.service().health() }

        assertTrue(result.isFailure)
        val message = result.exceptionOrNull()?.message ?: ""
        assertTrue("unexpected message: $message", message.startsWith("网络错误"))
    }

    @Test
    fun unconfiguredServiceFailsGracefully() = runBlocking {
        assertFalse(Api.isConfigured())

        val result = Api.call { Api.service().health() }

        assertTrue(result.isFailure)
        assertNotNull(result.exceptionOrNull())
        assertEquals(Api.NOT_CONFIGURED_MESSAGE, result.exceptionOrNull()?.message ?: "")
    }

    // ------------------------------------------------------------------
    // Cancellation must propagate: Api.call runs inside LaunchedEffect /
    // rememberCoroutineScope coroutines that are cancelled on navigation or
    // effect restart, and a swallowed CancellationException turns those into
    // bogus "failure" results (e.g. a "Job was cancelled" error flash on the
    // tasks screen) while breaking structured concurrency.
    // ------------------------------------------------------------------

    @Test
    fun cancellingTheCallerCancelsTheCallInsteadOfFailing() = runBlocking {
        server.enqueue(MockResponse().setSocketPolicy(SocketPolicy.NO_RESPONSE))
        Api.configure(baseUrl(), null, readTimeoutMillis = 30_000)

        var resolved: Result<Health>? = null
        // Dispatched off the runBlocking event loop so the blocking
        // takeRequest below does not starve the coroutine.
        val job = launch(Dispatchers.IO) {
            resolved = Api.call { Api.service().health() }
        }
        // The request reaching the server proves the call is suspended now.
        assertNotNull("request should reach the server", server.takeRequest(5, TimeUnit.SECONDS))
        job.cancelAndJoin()

        assertTrue("job must end cancelled", job.isCancelled)
        assertNull(
            "a cancelled call must not resolve to a failure Result",
            resolved,
        )
    }

    @Test
    fun cancellationThrownInsideTheBlockPropagates() {
        val thrown = runCatching {
            runBlocking {
                Api.call { throw CancellationException("stop") }
            }
        }
        assertTrue(
            "CancellationException must reach the caller, not become a Result",
            thrown.exceptionOrNull() is CancellationException,
        )
    }

    // ------------------------------------------------------------------
    // configure() is called with user-typed text from the settings screen
    // (inside a rememberCoroutineScope launch) and with persisted values at
    // app start; a malformed URL must never crash those coroutines.
    // ------------------------------------------------------------------

    @Test
    fun malformedBaseUrlFallsBackToDefaultInsteadOfThrowing() {
        val outcome = runCatching { Api.configure("not a url at all", null) }

        assertTrue("configure must not throw for malformed input", outcome.isSuccess)
        assertEquals(Api.DEFAULT_BASE_URL, Api.baseUrl)
        assertTrue(Api.isConfigured())
    }

    @Test
    fun nonHttpSchemeBaseUrlFallsBackToDefaultInsteadOfThrowing() {
        val outcome = runCatching { Api.configure("ftp://10.0.2.2:8081/", null) }

        assertTrue("configure must not throw for non-http schemes", outcome.isSuccess)
        assertEquals(Api.DEFAULT_BASE_URL, Api.baseUrl)
    }

    @Test
    fun reconfigureWithNewBaseUrlIsPickedUpImmediately() = runBlocking {
        server.enqueue(json("""{"status":"ok","version":"1"}"""))
        Api.configure(baseUrl(), null)
        Api.call { Api.service().health() }
        assertEquals("/api/v1/health", server.takeRequest().path)

        // A second server: requests after configure() must go there, never to
        // a cached service bound to the old base URL.
        val second = MockWebServer()
        second.start()
        try {
            second.enqueue(json("""{"status":"ok","version":"2"}"""))
            Api.configure(second.url("/").toString(), null)
            Api.call { Api.service().health() }

            assertEquals("/api/v1/health", second.takeRequest().path)
            assertNull("old server must see no further requests", server.takeRequest(200, TimeUnit.MILLISECONDS))
        } finally {
            second.shutdown()
        }
    }

    // ------------------------------------------------------------------
    // Readable error mapping for non-JSON bodies and escaped details.
    // ------------------------------------------------------------------

    @Test
    fun nonJsonSuccessBodyBecomesReadableFormatError() = runBlocking {
        server.enqueue(
            MockResponse()
                .setResponseCode(200)
                .setHeader("Content-Type", "text/html")
                .setBody("<html><body>proxy login page</body></html>"),
        )
        Api.configure(baseUrl(), null)

        val result = Api.call { Api.service().health() }

        assertTrue(result.isFailure)
        val message = result.exceptionOrNull()?.message ?: ""
        assertTrue("unexpected message: $message", message.startsWith("响应格式错误"))
    }

    @Test
    fun errorDetailWithEscapedQuotesIsFullyDecoded() = runBlocking {
        server.enqueue(json("""{"detail":"bad \"quoted\" value"}""", 400))
        Api.configure(baseUrl(), null)

        val result = Api.call { Api.service().health() }

        assertTrue(result.isFailure)
        assertEquals("""bad "quoted" value""", result.exceptionOrNull()?.message ?: "")
    }
}