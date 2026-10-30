package com.lingyi.noveldl.data

import com.lingyi.noveldl.ui.music.MUSIC_PAGE_SIZE
import com.lingyi.noveldl.ui.music.MusicPaging
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * `POST /api/v1/music/search` paging contract backed by MockWebServer: the
 * `limit`/`offset` request parameters (offset = number of results already
 * loaded), response parsing of `total` + `tracks`, and the page-append flow
 * with dedup by track id.
 */
class MusicSearchApiTest {

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

    private fun tracksJson(vararg ids: String): String =
        ids.joinToString(",") { id ->
            """{"provider":"netease","id":"$id","name":"song-$id","artists":"周杰伦","album":"叶惠美","duration_s":269,"cover_url":"","vip":false}"""
        }

    private fun searchResponseJson(total: Int, ids: List<String>): String =
        """{"total":$total,"tracks":[${tracksJson(*ids.toTypedArray())}]}"""

    private suspend fun search(offset: Int): MusicSearchResponse {
        val result = Api.call {
            Api.service().musicSearch(
                MusicSearchRequest(
                    platform = "netease",
                    keyword = "晴天",
                    limit = MUSIC_PAGE_SIZE,
                    offset = offset,
                ),
            )
        }
        return result.getOrThrow()
    }

    @Test
    fun firstPageSendsPostWithLimitAndZeroOffset() = runBlocking {
        server.enqueue(json(searchResponseJson(2, listOf("1", "2"))))
        Api.configure(baseUrl(), null)

        search(offset = 0)

        val request = server.takeRequest()
        assertEquals("POST", request.method)
        assertEquals("/api/v1/music/search", request.requestUrl!!.encodedPath)
        val body = Json.parseToJsonElement(request.body.readUtf8()).jsonObject
        assertEquals("netease", body["platform"]!!.jsonPrimitive.content)
        assertEquals("晴天", body["keyword"]!!.jsonPrimitive.content)
        assertEquals(MUSIC_PAGE_SIZE, body["limit"]!!.jsonPrimitive.int)
        assertEquals(0, body["offset"]!!.jsonPrimitive.int)
    }

    @Test
    fun nextPageRequestsOffsetEqualToLoadedCount() = runBlocking {
        server.enqueue(json(searchResponseJson(3, listOf("1", "2"))))
        server.enqueue(json(searchResponseJson(3, listOf("2", "3"))))
        Api.configure(baseUrl(), null)

        val first = search(offset = 0)
        search(offset = first.tracks.size)

        // Skip the first request; inspect the "load more" request.
        server.takeRequest()
        val body = Json.parseToJsonElement(server.takeRequest().body.readUtf8()).jsonObject
        assertEquals(2, body["offset"]!!.jsonPrimitive.int)
        assertEquals(MUSIC_PAGE_SIZE, body["limit"]!!.jsonPrimitive.int)
        assertEquals("晴天", body["keyword"]!!.jsonPrimitive.content)
    }

    @Test
    fun pagesAppendWithoutDuplicatesAndStopAtShortPage() = runBlocking {
        // Page 2 repeats id "2" (overlap) and ends short of a full page.
        server.enqueue(json(searchResponseJson(3, listOf("1", "2"))))
        server.enqueue(json(searchResponseJson(3, listOf("2", "3"))))
        Api.configure(baseUrl(), null)

        val page1 = search(offset = 0)
        val page2 = search(offset = page1.tracks.size)

        assertEquals(2, page1.tracks.size)
        assertEquals(3, page1.total)
        val merged = MusicPaging.merge(page1.tracks, page2.tracks)
        assertEquals(listOf("1", "2", "3"), merged.map { it.id })
        // Short page (2 < MUSIC_PAGE_SIZE): no further request may be made.
        assertTrue(
            !MusicPaging.hasMore(merged.size, page2.total, page2.tracks.size, page1.tracks.size),
        )
        assertEquals(2, server.requestCount)
    }

    @Test
    fun offsetAtTotalSignalsNoMoreRequests() = runBlocking {
        server.enqueue(json(searchResponseJson(2, listOf("1", "2"))))
        Api.configure(baseUrl(), null)

        val page = search(offset = 0)

        assertEquals(2, page.total)
        assertTrue(
            !MusicPaging.hasMore(page.tracks.size, page.total, page.tracks.size, 0),
        )
        assertEquals(1, server.requestCount)
    }

    @Test
    fun responseUnknownFieldsAreIgnoredAndDefaultsApply() = runBlocking {
        server.enqueue(json("""{"total":1,"tracks":[{"id":"9","name":"夜曲","extra":1}]}"""))
        Api.configure(baseUrl(), null)

        val page = search(offset = 0)

        val track = page.tracks.single()
        assertEquals("9", track.id)
        assertEquals("夜曲", track.name)
        assertEquals("", track.artists)
        assertEquals(1, page.total)
    }
}
