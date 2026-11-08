package com.lingyi.noveldl.data

import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * `POST /api/v1/music/jobs` body contract: aggregate downloads carry the
 * search response's `search_id` so the server can restore the original
 * provider objects; without one the field is omitted and the server falls
 * back to the submitted track dictionaries.
 */
class MusicJobApiTest {

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

    private fun track(id: String) = MusicTrack(
        provider = "netease",
        id = id,
        name = "song-$id",
        artists = "周杰伦",
        album = "叶惠美",
        duration_s = 269,
        sources = listOf("netease", "qq"),
    )

    private suspend fun createJob(body: MusicJobRequest) {
        val result = Api.call { Api.service().createMusicJob(body) }
        result.getOrThrow()
    }

    @Test
    fun jobRequestEchoesSearchIdAndPlatformAll() = runBlocking {
        server.enqueue(json("""{"job_id":"j1"}"""))
        Api.configure(server.url("/").toString(), null)

        createJob(
            MusicJobRequest(
                platform = MUSIC_ALL_PLATFORMS,
                tracks = listOf(track("1")),
                search_id = "sid-9",
            ),
        )

        val request = server.takeRequest()
        assertEquals("POST", request.method)
        assertEquals("/api/v1/music/jobs", request.requestUrl!!.encodedPath)
        val body = Json.parseToJsonElement(request.body.readUtf8()).jsonObject
        assertEquals("all", body["platform"]!!.jsonPrimitive.content)
        assertEquals("sid-9", body["search_id"]!!.jsonPrimitive.content)
        val sent = body["tracks"]!!.jsonArray.single().jsonObject
        assertEquals("netease", sent["provider"]!!.jsonPrimitive.content)
        assertEquals(
            listOf("netease", "qq"),
            sent["sources"]!!.jsonArray.map { it.jsonPrimitive.content },
        )
    }

    @Test
    fun jobRequestOmitsMissingSearchIdSoServerCanFallBack() = runBlocking {
        server.enqueue(json("""{"job_id":"j2"}"""))
        Api.configure(server.url("/").toString(), null)

        createJob(
            MusicJobRequest(
                platform = MUSIC_ALL_PLATFORMS,
                tracks = listOf(track("2")),
            ),
        )

        val body = Json.parseToJsonElement(server.takeRequest().body.readUtf8()).jsonObject
        assertFalse(body.containsKey("search_id"))
        // platform is explicit on the wire even when it equals the default.
        assertEquals("all", body["platform"]!!.jsonPrimitive.content)
        assertTrue(body["tracks"]!!.jsonArray.isNotEmpty())
    }
}
