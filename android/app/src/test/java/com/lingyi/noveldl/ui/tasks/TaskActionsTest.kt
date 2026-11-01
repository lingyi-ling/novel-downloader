package com.lingyi.noveldl.ui.tasks

import com.lingyi.noveldl.data.Api
import com.lingyi.noveldl.data.Task
import kotlinx.coroutines.runBlocking
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * JVM tests for the task screen interactions: expanding/collapsing the detail
 * panel, retrying failed/cancelled tasks (`POST /api/v1/tasks` with the
 * original identity), and converting `exported_paths` into downloadable files.
 */
class TaskActionsTest {

    // ---- expand / collapse ----

    @Test
    fun togglingAddsTheTaskToTheExpandedSet() {
        val expanded = toggleTaskExpanded(emptySet(), "t1")

        assertEquals(setOf("t1"), expanded)
    }

    @Test
    fun togglingRemovesAnAlreadyExpandedTask() {
        val expanded = toggleTaskExpanded(setOf("t1", "t2"), "t1")

        assertEquals(setOf("t2"), expanded)
    }

    @Test
    fun togglingKeepsOtherExpandedTasks() {
        val collapsed = toggleTaskExpanded(setOf("t1", "t2"), "t1")
        val reExpanded = toggleTaskExpanded(collapsed, "t1")

        assertEquals(setOf("t2"), collapsed)
        assertEquals(setOf("t1", "t2"), reExpanded)
    }

    @Test
    fun togglingDoesNotMutateTheInputSet() {
        val original = setOf("t1")

        toggleTaskExpanded(original, "t2")

        assertEquals(setOf("t1"), original)
    }

    // ---- retry availability ----

    private fun task(
        id: String = "t1",
        status: String = "failed",
        site: String = "b520",
        bookId: String = "8_8187",
        title: String = "示例小说",
    ) = Task(
        task_id = id,
        title = title,
        site = site,
        book_id = bookId,
        status = status,
    )

    @Test
    fun onlyFailedAndCancelledStatusesAreRetryable() {
        assertTrue(isRetryableTaskStatus("failed"))
        assertTrue(isRetryableTaskStatus("cancelled"))
        assertTrue(isRetryableTaskStatus(" FAILED "))
        assertTrue(task(status = "Cancelled").isRetryable())
        assertFalse(isRetryableTaskStatus("queued"))
        assertFalse(isRetryableTaskStatus("running"))
        assertFalse(isRetryableTaskStatus("processing"))
        assertFalse(isRetryableTaskStatus("exporting"))
        assertFalse(isRetryableTaskStatus("completed"))
        assertFalse(isRetryableTaskStatus(""))
    }

    @Test
    fun retryIsOfferedOnlyForRetryableTasksWithAnIdentity() {
        assertTrue(task(status = "failed").retryOffered())
        assertTrue(task(status = "cancelled").retryOffered())
        assertFalse(task(status = "completed").retryOffered())
        assertFalse(task(site = "  ").retryOffered())
        assertFalse(task(bookId = "").retryOffered())
    }

    @Test
    fun retryIsEnabledUntilTheTaskHasBeenEnqueuedAgain() {
        assertTrue(retryEnabled("t1", retriedIds = emptySet(), retryingIds = emptySet()))
        assertFalse(retryEnabled("t1", retriedIds = setOf("t1"), retryingIds = emptySet()))
        assertFalse(retryEnabled("t1", retriedIds = emptySet(), retryingIds = setOf("t1")))
        // Other tasks being retried/enqueued must not block this one.
        assertTrue(retryEnabled("t1", retriedIds = setOf("t2"), retryingIds = setOf("t3")))
    }

    @Test
    fun retriedIdsSurviveSaveAndRestoreAcrossPageSwitches() {
        // The retried set persists (rememberSaveable) so a page switch cannot
        // re-enable retry on a task whose fresh copy is already queued.
        val saved = saveRetriedIds(setOf("t1", "t2"))
        assertEquals(listOf("t1", "t2"), saved)
        assertEquals(setOf("t1", "t2"), restoreRetriedIds(saved))
        // Degenerate cases round-trip too.
        assertEquals(emptyList<String>(), saveRetriedIds(emptySet()))
        assertEquals(emptySet<String>(), restoreRetriedIds(emptyList()))
    }

    @Test
    fun retryAvailableCombinesStatusIdentityAndDisableState() {
        assertTrue(task(status = "failed").retryAvailable(emptySet(), emptySet()))
        assertFalse(task(status = "failed").retryAvailable(setOf("t1"), emptySet()))
        assertFalse(task(status = "failed").retryAvailable(emptySet(), setOf("t1")))
        assertFalse(task(status = "running").retryAvailable(emptySet(), emptySet()))
        assertFalse(task(site = "").retryAvailable(emptySet(), emptySet()))
    }

    // ---- retry request construction ----

    @Test
    fun retryRequestReusesTheOriginalIdentity() {
        val request = retryRequest(
            task(id = "t9", title = "示例小说", site = "b520", bookId = "8_8187"),
        )

        assertEquals("b520", request!!.site)
        assertEquals("8_8187", request.book_id)
        assertEquals("示例小说", request.title)
    }

    @Test
    fun retryRequestTrimsSurroundingWhitespace() {
        val request = retryRequest(
            task(site = " b520 ", bookId = " 8_8187 ", title = " 示例小说 "),
        )

        assertEquals("b520", request!!.site)
        assertEquals("8_8187", request.book_id)
        assertEquals("示例小说", request.title)
    }

    @Test
    fun retryRequestSendsNoTitleWhenTheOriginalIsBlank() {
        // The server falls back to "{site} (id = {book_id})" for a missing title.
        val request = retryRequest(task(title = "   "))

        assertNull(request!!.title)
    }

    @Test
    fun retryRequestIsNullWithoutASiteOrBookId() {
        assertNull(retryRequest(task(site = "", bookId = "8_8187")))
        assertNull(retryRequest(task(site = "b520", bookId = "  ")))
    }

    @Test
    fun retryRequestIsNullForTasksWithoutARetryableStatus() {
        // Pure guard for callers that bypass the button state.
        assertNull(retryRequest(task(status = "completed")))
    }

    // ---- export file mapping ----

    @Test
    fun exportedPathsBecomeDownloadableHistoryFiles() {
        val task = task().copy(
            exported_paths = mapOf(
                "txt" to "/data/exports/书名.txt",
                "epub" to "D:\\out\\书名.epub",
            ),
        )

        val files = taskExportFiles(task)

        // Sorted by format key: epub first, then txt.
        assertEquals(listOf("书名.epub", "书名.txt"), files.map { it.name })
        assertEquals(listOf("epub", "txt"), files.map { it.ext })
        assertEquals(
            listOf("/downloads/%E4%B9%A6%E5%90%8D.epub", "/downloads/%E4%B9%A6%E5%90%8D.txt"),
            files.map { it.download_url },
        )
    }

    @Test
    fun exportFileNamesPercentEncodeSpecialCharacters() {
        val task = task().copy(
            exported_paths = mapOf("txt" to "/out/a b#c?.txt"),
        )

        val file = taskExportFiles(task).single()

        assertEquals("a b#c?.txt", file.name)
        assertEquals("/downloads/a%20b%23c%3F.txt", file.download_url)
    }

    @Test
    fun exportFilesWithoutAnExtensionKeepAnEmptyExt() {
        val task = task().copy(exported_paths = mapOf("raw" to "/out/datafile"))

        val file = taskExportFiles(task).single()

        assertEquals("datafile", file.name)
        assertEquals("", file.ext)
    }

    @Test
    fun blankExportPathsAreDroppedAndEmptyMapsYieldNoFiles() {
        assertTrue(taskExportFiles(task()).isEmpty())
        assertTrue(
            taskExportFiles(
                task().copy(exported_paths = mapOf("txt" to "   ", "epub" to "")),
            ).isEmpty(),
        )
    }

    // ---- the retry flow over the wire ----

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

    private fun taskResponse(): String =
        """{"task_id":"t-new","title":"示例小说","site":"b520",""" +
            """"book_id":"8_8187","status":"queued","chapters_done":0,""" +
            """"chapters_total":0,"progress":0.0,"error":null,""" +
            """"exported_paths":{},"created_at":0.0,"finished_at":null}"""

    @Test
    fun retryPostsTheOriginalIdentityToTasks() = runBlocking {
        server.enqueue(json(taskResponse()))
        Api.configure(baseUrl(), null)

        val failed = task(id = "t-old", status = "failed")
        val target = retryRequest(failed)!!
        assertTrue(failed.retryAvailable(emptySet(), emptySet()))
        val result = Api.call { Api.service().addTask(target) }

        assertTrue(result.isSuccess)
        assertEquals(1, server.requestCount)
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
    fun retryFailureCarriesTheServerDetail() = runBlocking {
        server.enqueue(json("""{"detail":"task already queued"}""", 409))
        Api.configure(baseUrl(), null)

        val result = Api.call { Api.service().addTask(retryRequest(task())!!) }

        assertTrue(result.isFailure)
        assertEquals("task already queued", result.exceptionOrNull()?.message)
    }
}
