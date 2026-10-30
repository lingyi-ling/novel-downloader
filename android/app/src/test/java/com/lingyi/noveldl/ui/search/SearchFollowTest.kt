package com.lingyi.noveldl.ui.search

import com.lingyi.noveldl.data.Api
import com.lingyi.noveldl.data.SearchResult
import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * The search screen reuses `POST /api/v1/watchlist` for its one-tap "follow"
 * button. These tests pin the identity key used for de-duplication, the
 * request-body mapping from a search result, and the transport error mapping.
 */
class SearchFollowTest {

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

    private fun result(
        site: String = "b520",
        bookId: String = "8_8187",
        title: String = "示例小说",
    ) = SearchResult(site = site, book_id = bookId, title = title)

    // ---- pure mapping: key + request body ----

    @Test
    fun watchKeysDistinguishSitesAndBooks() {
        assertEquals("b520/8_8187", searchWatchKey("b520", "8_8187"))
        assertEquals("b520/8_8187", result().watchKey())
        assertEquals("qidian/123", result(site = "qidian", bookId = "123").watchKey())
    }

    @Test
    fun followRequestCarriesSiteBookIdAndResultTitle() {
        val target = followRequest(result())

        assertNotNull(target)
        assertEquals("b520", target!!.site)
        assertEquals("8_8187", target.book_id)
        assertEquals("示例小说", target.title)
    }

    @Test
    fun followRequestRefusesResultsWithoutSiteOrBookId() {
        assertNull(followRequest(result(site = " ")))
        assertNull(followRequest(result(bookId = "")))
    }

    // ---- the follow flow over MockWebServer ----

    @Test
    fun followingAResultPostsTheWatchlistBody() = runBlocking {
        server.enqueue(json("""{"created":true,"entry":{"site":"b520","book_id":"8_8187"}}"""))
        Api.configure(baseUrl(), null)

        val target = followRequest(result())!!
        val outcome = Api.call { Api.service().addWatch(target) }

        assertTrue(outcome.isSuccess)
        assertTrue(outcome.getOrNull()!!.created)
        val request = server.takeRequest()
        assertEquals("POST", request.method)
        assertEquals("/api/v1/watchlist", request.path)
        assertTrue(request.getHeader("Content-Type")!!.startsWith("application/json"))
        val body = request.body.readUtf8()
        assertTrue("site missing in $body", body.contains("\"site\":\"b520\""))
        assertTrue("book_id missing in $body", body.contains("\"book_id\":\"8_8187\""))
        assertTrue("title missing in $body", body.contains("\"title\":\"示例小说\""))
    }

    @Test
    fun followFailureIsMappedToTheServerDetail() = runBlocking {
        server.enqueue(json("""{"detail":"watchlist is full"}""", 422))
        Api.configure(baseUrl(), null)

        val target = followRequest(result())!!
        val outcome = Api.call { Api.service().addWatch(target) }

        assertTrue(outcome.isFailure)
        assertEquals("watchlist is full", outcome.exceptionOrNull()?.message)
        assertEquals("POST", server.takeRequest().method)
    }
}
