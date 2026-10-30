package com.lingyi.noveldl.data

import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * Watchlist endpoint tests backed by MockWebServer: request shape (path,
 * method, JSON body) and response parsing, including the optional ``task``
 * payload attached by ``download=true`` checks.
 */
class WatchlistApiTest {

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
    fun watchlistUsesGetAndParsesBooks() = runBlocking {
        server.enqueue(
            json(
                """{"total":1,"books":[{"site":"b520","book_id":"8_8187",""" +
                    """"title":"示例小说","last_chapter":"第 120 章",""" +
                    """"updated_at":"2026-01-01T00:00:00+00:00"}]}""",
            ),
        )
        Api.configure(baseUrl(), null)

        val result = Api.call { Api.service().watchlist() }

        assertTrue(result.isSuccess)
        val response = result.getOrNull()!!
        assertEquals(1, response.total)
        assertEquals(1, response.books.size)
        val entry = response.books.single()
        assertEquals("b520", entry.site)
        assertEquals("8_8187", entry.book_id)
        assertEquals("示例小说", entry.title)
        assertEquals("第 120 章", entry.last_chapter)
        assertEquals("2026-01-01T00:00:00+00:00", entry.updated_at)
        val request = server.takeRequest()
        assertEquals("GET", request.method)
        assertEquals("/api/v1/watchlist", request.path)
    }

    @Test
    fun addWatchUsesPostWithJsonBody() = runBlocking {
        server.enqueue(
            json(
                """{"created":true,"entry":{"site":"b520","book_id":"8_8187",""" +
                    """"title":"示例小说","last_chapter":"","updated_at":""}}""",
            ),
        )
        Api.configure(baseUrl(), null)

        val result = Api.call {
            Api.service().addWatch(
                WatchlistAddRequest(site = "b520", book_id = "8_8187", title = "示例小说"),
            )
        }

        assertTrue(result.isSuccess)
        assertTrue(result.getOrNull()!!.created)
        assertEquals("示例小说", result.getOrNull()!!.entry.title)
        val request = server.takeRequest()
        assertEquals("POST", request.method)
        assertEquals("/api/v1/watchlist", request.path)
        assertTrue(request.getHeader("Content-Type")!!.startsWith("application/json"))
        val body = request.body.readUtf8()
        assertTrue(body.contains("\"site\":\"b520\""))
        assertTrue(body.contains("\"book_id\":\"8_8187\""))
        assertTrue(body.contains("\"title\":\"示例小说\""))
    }

    @Test
    fun addWatchOmitsNullTitleFromTheBody() = runBlocking {
        server.enqueue(json("""{"created":false,"entry":{"site":"b520","book_id":"8_8187"}}"""))
        Api.configure(baseUrl(), null)

        val result = Api.call {
            Api.service().addWatch(WatchlistAddRequest(site = "b520", book_id = "8_8187"))
        }

        assertTrue(result.isSuccess)
        assertFalse(result.getOrNull()!!.created)
        val body = server.takeRequest().body.readUtf8()
        assertTrue(body.contains("\"book_id\":\"8_8187\""))
        assertTrue("title should be omitted when null: $body", !body.contains("title"))
    }

    @Test
    fun addWatchSurfacesTheServerDetailOnValidationError() = runBlocking {
        server.enqueue(json("""{"detail":"Request validation failed: site: Field required"}""", 422))
        Api.configure(baseUrl(), null)

        val result = Api.call {
            Api.service().addWatch(WatchlistAddRequest(site = "", book_id = "8_8187"))
        }

        assertTrue(result.isFailure)
        assertEquals(
            "Request validation failed: site: Field required",
            result.exceptionOrNull()?.message,
        )
        assertEquals("POST", server.takeRequest().method)
    }

    @Test
    fun removeWatchUsesDeleteAndParsesOk() = runBlocking {
        server.enqueue(json("""{"ok":true}"""))
        Api.configure(baseUrl(), null)

        val result = Api.call { Api.service().removeWatch("b520", "8_8187") }

        assertTrue(result.isSuccess)
        assertTrue(result.getOrNull()!!.ok)
        val request = server.takeRequest()
        assertEquals("DELETE", request.method)
        assertEquals("/api/v1/watchlist/b520/8_8187", request.path)
    }

    @Test
    fun removeWatchSurfacesTheServerDetailOn404() = runBlocking {
        server.enqueue(json("""{"detail":"Book not in watchlist: b520/8_8187"}""", 404))
        Api.configure(baseUrl(), null)

        val result = Api.call { Api.service().removeWatch("b520", "8_8187") }

        assertTrue(result.isFailure)
        assertEquals(
            "Book not in watchlist: b520/8_8187",
            result.exceptionOrNull()?.message,
        )
        assertEquals("DELETE", server.takeRequest().method)
    }

    @Test
    fun checkWatchlistUsesPostAndParsesSummaryAndItems() = runBlocking {
        server.enqueue(
            json(
                """{"checked":2,"updated":1,"failed":1,"download":true,""" +
                    """"needs_download":[{"site":"b520","book_id":"8_8187",""" +
                    """"title":"示例小说","previous_chapter":"第 119 章",""" +
                    """"last_chapter":"第 120 章","found":true,"changed":true,""" +
                    """"needs_download":true,"error":null,""" +
                    """"task":{"site":"b520","book_id":"8_8187","title":"示例小说"}}],""" +
                    """"items":[{"site":"b520","book_id":"8_8187","title":"示例小说",""" +
                    """"previous_chapter":"第 119 章","last_chapter":"第 120 章",""" +
                    """"found":true,"changed":true,"needs_download":true,"error":null,""" +
                    """"task":{"site":"b520","book_id":"8_8187","title":"示例小说"}},""" +
                    """{"site":"qidian","book_id":"1","title":"",""" +
                    """"previous_chapter":"","last_chapter":"","found":false,""" +
                    """"changed":false,"needs_download":false,""" +
                    """"error":"RuntimeError: site down"}]}""",
            ),
        )
        Api.configure(baseUrl(), null)

        val result = Api.call {
            Api.service().checkWatchlist(WatchlistCheckRequest(download = true))
        }

        assertTrue(result.isSuccess)
        val response = result.getOrNull()!!
        assertEquals(2, response.checked)
        assertEquals(1, response.updated)
        assertEquals(1, response.failed)
        assertTrue(response.download)
        assertEquals(1, response.needs_download.size)

        val updatedItem = response.needs_download.single()
        assertEquals("第 120 章", updatedItem.last_chapter)
        assertEquals("第 119 章", updatedItem.previous_chapter)
        assertTrue(updatedItem.needs_download)
        assertNull(updatedItem.error)
        // The ready-to-post task body the UI confirms instead of re-submitting.
        val task = updatedItem.task
        assertNotNull("task should be attached to needs_download entries", task)
        assertEquals("b520", task!!.site)
        assertEquals("8_8187", task.book_id)
        assertEquals("示例小说", task.title)

        assertEquals(2, response.items.size)
        val failedItem = response.items[1]
        assertNull(failedItem.task)
        assertEquals("RuntimeError: site down", failedItem.error)
        assertFalse(failedItem.needs_download)

        val request = server.takeRequest()
        assertEquals("POST", request.method)
        assertEquals("/api/v1/watchlist/check", request.path)
        assertEquals("""{"download":true}""", request.body.readUtf8())
    }

    @Test
    fun checkWatchlistEntriesWithoutTaskParseAsNull() = runBlocking {
        // Unchanged entries (or checks with download=false) carry no task.
        server.enqueue(
            json(
                """{"checked":1,"updated":0,"failed":0,"download":false,""" +
                    """"needs_download":[],""" +
                    """"items":[{"site":"b520","book_id":"8_8187","title":"示例小说",""" +
                    """"previous_chapter":"第 120 章","last_chapter":"第 120 章",""" +
                    """"found":true,"changed":false,"needs_download":false,""" +
                    """"error":null}]}""",
            ),
        )
        Api.configure(baseUrl(), null)

        val result = Api.call {
            Api.service().checkWatchlist(WatchlistCheckRequest(download = false))
        }

        assertTrue(result.isSuccess)
        val item = result.getOrNull()!!.items.single()
        assertNull(item.task)
        assertFalse(item.changed)
        assertEquals("第 120 章", item.last_chapter)
        // download=false is the default and therefore omitted from the body.
        assertEquals("{}", server.takeRequest().body.readUtf8())
    }
}
