package com.lingyi.noveldl.ui.follow

import com.lingyi.noveldl.data.AddTaskRequest
import com.lingyi.noveldl.data.Api
import com.lingyi.noveldl.data.WatchCheckItem
import com.lingyi.noveldl.data.WatchTaskBody
import com.lingyi.noveldl.data.WatchlistCheckRequest
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
 * The follow screen must turn `download=true` check results into real
 * `POST /api/v1/tasks` submissions. These tests pin that contract: extract
 * the server-provided task body, post it to /tasks with the right path and
 * JSON shape, map failures to readable messages, and never submit an entry
 * that carries no task.
 */
class FollowQueueingTest {

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

    /**
     * Mirrors the UI flow: run a `download=true` check, then submit every
     * entry whose check result carries a usable task body.
     */
    private suspend fun checkAndQueueEverything() {
        val check = Api.call {
            Api.service().checkWatchlist(WatchlistCheckRequest(download = true))
        }.getOrThrow()
        for (item in check.needs_download) {
            val target = queueTarget(item) ?: continue
            Api.call { Api.service().addTask(target) }
        }
    }

    private fun checkResponse(withTask: Boolean): String {
        val task = if (withTask) {
            """"task":{"site":"b520","book_id":"8_8187","title":"示例小说"}"""
        } else {
            """"task":null"""
        }
        return """{"checked":1,"updated":1,"failed":0,"download":true,""" +
            """"needs_download":[{"site":"b520","book_id":"8_8187",""" +
            """"title":"示例小说","previous_chapter":"第 119 章",""" +
            """"last_chapter":"第 120 章","found":true,"changed":true,""" +
            """"needs_download":true,"error":null,$task}],"items":[]}"""
    }

    private fun taskResponse(): String =
        """{"task_id":"t-1","title":"示例小说","site":"b520",""" +
            """"book_id":"8_8187","status":"queued","chapters_done":0,""" +
            """"chapters_total":0,"progress":0.0,"error":null,""" +
            """"exported_paths":{},"created_at":0.0,"finished_at":null}"""

    // ---- queueTarget: the pure submit/skip decision ----

    @Test
    fun queueTargetUsesTheServerProvidedTaskBody() {
        val item = WatchCheckItem(
            site = "b520",
            book_id = "8_8187",
            title = "示例小说",
            needs_download = true,
            task = WatchTaskBody(site = "b520", book_id = "8_8187", title = "示例小说"),
        )

        val target = queueTarget(item)

        assertNotNull(target)
        assertEquals("b520", target!!.site)
        assertEquals("8_8187", target.book_id)
        assertEquals("示例小说", target.title)
    }

    @Test
    fun queueTargetIsNullWithoutATask() {
        val item = WatchCheckItem(site = "b520", book_id = "8_8187", needs_download = true)

        assertNull(queueTarget(item))
    }

    @Test
    fun queueTargetRefusesTaskBodiesWithoutASiteOrBookId() {
        assertNull(
            queueTarget(
                WatchCheckItem(
                    site = "b520",
                    book_id = "8_8187",
                    task = WatchTaskBody(site = "", book_id = "8_8187"),
                ),
            ),
        )
        assertNull(
            queueTarget(
                WatchCheckItem(
                    site = "b520",
                    book_id = "8_8187",
                    task = WatchTaskBody(site = "b520", book_id = "  "),
                ),
            ),
        )
    }

    @Test
    fun watchItemKeysDistinguishSites() {
        assertEquals("b520/8_8187", watchItemKey("b520", "8_8187"))
        assertEquals("b520/8_8187", WatchCheckItem(site = "b520", book_id = "8_8187").key())
    }

    // ---- the full check -> POST /tasks flow over MockWebServer ----

    @Test
    fun extractedTaskIsPostedToTasksWithTheServerProvidedBody() = runBlocking {
        server.enqueue(json(checkResponse(withTask = true)))
        server.enqueue(json(taskResponse()))
        Api.configure(baseUrl(), null)

        checkAndQueueEverything()

        assertEquals(2, server.requestCount)
        server.takeRequest() // skip the check request (pinned in WatchlistApiTest)
        val post = server.takeRequest()
        assertEquals("POST", post.method)
        assertEquals("/api/v1/tasks", post.path)
        assertTrue(post.getHeader("Content-Type")!!.startsWith("application/json"))
        val body = post.body.readUtf8()
        assertTrue("site missing in $body", body.contains("\"site\":\"b520\""))
        assertTrue("book_id missing in $body", body.contains("\"book_id\":\"8_8187\""))
        assertTrue("title missing in $body", body.contains("\"title\":\"示例小说\""))
    }

    @Test
    fun addTaskFailureIsMappedToTheServerDetail() = runBlocking {
        server.enqueue(json(checkResponse(withTask = true)))
        server.enqueue(json("""{"detail":"task already queued"}""", 409))
        Api.configure(baseUrl(), null)

        val check = Api.call {
            Api.service().checkWatchlist(WatchlistCheckRequest(download = true))
        }.getOrThrow()
        val target = queueTarget(check.needs_download.single())!!
        val result = Api.call { Api.service().addTask(target) }

        assertTrue(result.isFailure)
        assertEquals("task already queued", result.exceptionOrNull()?.message)
        assertEquals(2, server.requestCount)
    }

    @Test
    fun addTaskHttpErrorWithoutDetailFallsBackToTheStatusCode() = runBlocking {
        server.enqueue(json("{}", 500))
        Api.configure(baseUrl(), null)

        val result = Api.call {
            Api.service().addTask(AddTaskRequest(site = "b520", book_id = "8_8187"))
        }

        assertTrue(result.isFailure)
        assertEquals("HTTP 500", result.exceptionOrNull()?.message)
    }

    @Test
    fun entriesWithoutTaskNeverTriggerAddTask() = runBlocking {
        // Two check entries: one with a task, one without. Exactly one
        // POST /api/v1/tasks must be issued, for the task-bearing entry.
        server.enqueue(
            json(
                """{"checked":2,"updated":1,"failed":0,"download":true,""" +
                    """"needs_download":[""" +
                    """{"site":"b520","book_id":"8_8187","title":"示例小说",""" +
                    """"found":true,"changed":true,"needs_download":true,""" +
                    """"error":null,"task":{"site":"b520","book_id":"8_8187",""" +
                    """"title":"示例小说"}},""" +
                    """{"site":"b520","book_id":"9_9999","title":"无任务",""" +
                    """"found":true,"changed":true,"needs_download":true,""" +
                    """"error":null,"task":null}],"items":[]}""",
            ),
        )
        server.enqueue(json(taskResponse()))
        Api.configure(baseUrl(), null)

        checkAndQueueEverything()

        assertEquals(2, server.requestCount)
        server.takeRequest() // skip the check request
        val post = server.takeRequest()
        assertEquals("POST", post.method)
        assertEquals("/api/v1/tasks", post.path)
        val body = post.body.readUtf8()
        assertTrue(body.contains("\"book_id\":\"8_8187\""))
        assertTrue(
            "the task-less entry must not be submitted: $body",
            !body.contains("9_9999"),
        )
    }
}
