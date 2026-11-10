package com.lingyi.noveldl.ui.music

import com.lingyi.noveldl.data.Api
import com.lingyi.noveldl.data.ApiErrorCode
import com.lingyi.noveldl.data.ApiFailure
import com.lingyi.noveldl.data.MusicJobResult
import com.lingyi.noveldl.data.MusicTrack
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * The cross-page job orchestrator: one `POST /api/v1/music/jobs` per
 * `search_id` page, sequential polling, merged paths keyed by provider + id,
 * and per-batch failure containment.
 */
class MusicJobRunnerTest {

    private lateinit var server: MockWebServer

    @Before
    fun startServer() {
        server = MockWebServer()
        server.start()
        resetService()
        Api.configure(server.url("/").toString(), null)
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

    private fun track(id: String, provider: String = "netease") =
        MusicTrack(provider = provider, id = id, name = "song-$id")

    private fun idsByTrack(vararg pairs: Pair<MusicTrack, String>): Map<String, String> =
        pairs.associate { (track, id) -> MusicJobResults.trackKey(track) to id }

    private fun created(jobId: String) = json("""{"job_id":"$jobId"}""")

    private fun done(jobId: String, results: String) = json(
        """{"job_id":"$jobId","status":"done","progress":{"done":0,"total":0},"results":$results}""",
    )

    private fun running(jobId: String, done: Int, total: Int) = json(
        """{"job_id":"$jobId","status":"running","progress":{"done":$done,"total":$total}}""",
    )

    private fun failed(jobId: String, reason: String) = json(
        """{"job_id":"$jobId","status":"failed","error":"$reason","results":[]}""",
    )

    private fun takeRequestPath(): String =
        server.takeRequest().requestUrl!!.encodedPath

    private fun takeCreateBody() =
        Json.parseToJsonElement(server.takeRequest().body.readUtf8()).jsonObject

    private fun tracksOf(body: kotlinx.serialization.json.JsonObject): List<String> =
        body["tracks"]!!.jsonArray.map { it.jsonObject["id"]!!.jsonPrimitive.content }

    @Test
    fun crossPageSelectionSubmitsOneJobPerPageAndMergesPaths() = runBlocking {
        val first = track("1")
        val second = track("2")
        val third = track("3", provider = "qq")
        val chosen = listOf(first, second, third)

        server.enqueue(created("jA"))
        server.enqueue(
            done(
                "jA",
                """[
                    {"status":"ok","path":"/a/1.mp3","download_url":"/dl/1.mp3"},
                    {"status":"ok","path":"/a/2.mp3","download_url":"/dl/2.mp3"}
                ]""",
            ),
        )
        server.enqueue(created("jB"))
        server.enqueue(
            done(
                "jB",
                """[{"status":"ok","path":"/a/3.mp3","download_url":"/dl/3.mp3"}]""",
            ),
        )

        val saver = RecordingSaver()
        val progress = mutableListOf<Pair<Int, Int>>()
        val run = MusicJobRunner(
            service = { Api.service() },
            saver = saver,
            pollIntervalMs = 0,
        ).run("all", chosen, idsByTrack(
            first to "sid-A",
            second to "sid-A",
            third to "sid-B",
        )) { done, total -> progress += done to total }

        // Batch A: both tracks of page 1 and page 1's id, nothing else.
        val createA = takeCreateBody()
        assertEquals("sid-A", createA["search_id"]!!.jsonPrimitive.content)
        assertEquals(listOf("1", "2"), tracksOf(createA))
        assertEquals("/api/v1/music/jobs/jA", takeRequestPath())

        // Batch B: the later page with its own id, instead of falling back to
        // dictionaries under page 1's expired id.
        val createB = takeCreateBody()
        assertEquals("sid-B", createB["search_id"]!!.jsonPrimitive.content)
        assertEquals(listOf("3"), tracksOf(createB))
        assertEquals("/api/v1/music/jobs/jB", takeRequestPath())

        // Paths merged by provider + id, so export can write all three rows.
        assertEquals(
            mapOf(
                MusicJobResults.trackKey(first) to "/a/1.mp3",
                MusicJobResults.trackKey(second) to "/a/2.mp3",
                MusicJobResults.trackKey(third) to "/a/3.mp3",
            ),
            run.pathsByTrack,
        )
        assertEquals(3, run.total)
        assertEquals(3, run.counts.ok)
        assertEquals(listOf("/dl/1.mp3", "/dl/2.mp3", "/dl/3.mp3"), saver.saved)
        assertTrue(run.errors.isEmpty())
        assertNull(run.jobFailure)
        assertEquals(3 to 3, progress.last())
        // Progress is monotonic across batches.
        progress.zipWithNext().forEach { (before, after) ->
            assertTrue("progress went backwards: $before -> $after", after.first >= before.first)
        }
    }

    @Test
    fun expiredPageDoesNotDiscardTheOtherPagesDownloads() = runBlocking {
        val first = track("1")
        val second = track("2")
        val chosen = listOf(first, second)

        server.enqueue(created("jA"))
        server.enqueue(failed("jA", "搜索结果已过期，请重新搜索"))
        server.enqueue(created("jB"))
        server.enqueue(
            done("jB", """[{"status":"ok","path":"/a/2.mp3","download_url":"/dl/2.mp3"}]"""),
        )

        val run = MusicJobRunner({ Api.service() }, RecordingSaver(), pollIntervalMs = 0)
            .run(
                "all",
                chosen,
                idsByTrack(first to "sid-A", second to "sid-B"),
            )

        assertEquals("搜索结果已过期，请重新搜索", run.jobFailure)
        assertEquals(1, run.counts.ok)
        assertEquals(
            mapOf(MusicJobResults.trackKey(second) to "/a/2.mp3"),
            run.pathsByTrack,
        )
        assertTrue(run.errors.isEmpty())
    }

    @Test
    fun batchThatCannotBeCreatedIsReportedAndTheRestStillRuns() = runBlocking {
        val first = track("1")
        val second = track("2")

        server.enqueue(json("""{"detail":"boom"}""", 500))
        server.enqueue(created("jB"))
        server.enqueue(
            done("jB", """[{"status":"ok","path":"/a/2.mp3","download_url":"/dl/2.mp3"}]"""),
        )

        val run = MusicJobRunner({ Api.service() }, RecordingSaver(), pollIntervalMs = 0)
            .run(
                "all",
                listOf(first, second),
                idsByTrack(first to "sid-A", second to "sid-B"),
            )

        assertEquals(1, run.errors.size)
        val failure = run.errors.single() as ApiFailure
        assertEquals(ApiErrorCode.HTTP, failure.code)
        assertEquals("boom", failure.detail)
        assertEquals(1, run.counts.ok)
        assertEquals(
            mapOf(MusicJobResults.trackKey(second) to "/a/2.mp3"),
            run.pathsByTrack,
        )
    }

    @Test
    fun progressReportsRunningPollsWithoutOvershootingTheSelection() = runBlocking {
        val first = track("1")
        val second = track("2")

        server.enqueue(created("jA"))
        server.enqueue(running("jA", done = 1, total = 2))
        server.enqueue(running("jA", done = 2, total = 2))
        server.enqueue(
            done(
                "jA",
                """[
                    {"status":"ok","path":"/a/1.mp3"},
                    {"status":"ok","path":"/a/2.mp3"}
                ]""",
            ),
        )

        val progress = mutableListOf<Pair<Int, Int>>()
        MusicJobRunner({ Api.service() }, RecordingSaver(), pollIntervalMs = 0)
            .run(
                "all",
                listOf(first, second),
                idsByTrack(first to "sid-A", second to "sid-A"),
            ) { done, total -> progress += done to total }

        assertEquals(2 to 2, progress.last())
        assertTrue(progress.all { it.second == 2 })
        // A running report of 2/2 for a 2-track batch must not become 4/2.
        assertTrue(progress.all { it.first <= it.second })
    }

    @Test
    fun saveFailuresAreCountedWithTheirFirstReason() = runBlocking {
        val first = track("1")
        val second = track("2")

        server.enqueue(created("jA"))
        server.enqueue(
            done(
                "jA",
                """[
                    {"status":"ok","path":"/a/1.mp3","download_url":"/dl/1.mp3"},
                    {"status":"ok","path":"/a/2.mp3","download_url":"/dl/2.mp3"}
                ]""",
            ),
        )

        val saver = RecordingSaver(failFor = { it.download_url.endsWith("/1.mp3") })
        val run = MusicJobRunner({ Api.service() }, saver, pollIntervalMs = 0)
            .run(
                "all",
                listOf(first, second),
                idsByTrack(first to "sid-A", second to "sid-A"),
            )

        assertEquals(1, run.saveFailures)
        assertEquals("disk full", run.firstSaveFailure)
        // A hand-off failure does not change the engine's success count.
        assertEquals(2, run.counts.ok)
        assertEquals(listOf("/dl/2.mp3"), saver.saved)
    }

    @Test
    fun remoteJobReportsTheServerPathWhenNothingCanBeCopiedLocally() = runBlocking {
        val first = track("1")

        server.enqueue(created("jA"))
        server.enqueue(done("jA", """[{"status":"ok","path":"/srv/music/1.mp3"}]"""))

        val saver = RecordingSaver()
        val run = MusicJobRunner({ Api.service() }, saver, pollIntervalMs = 0)
            .run("all", listOf(first), idsByTrack(first to "sid-A"))

        assertEquals("/srv/music/1.mp3", run.serverPath)
        assertEquals(1, run.counts.ok)
        assertTrue(saver.saved.isEmpty())
    }

    /** Records the download URLs handed to the platform saver. */
    private class RecordingSaver(
        private val failFor: (MusicJobResult) -> Boolean = { false },
    ) : MusicFileSaver {

        val saved = mutableListOf<String>()

        override fun save(result: MusicJobResult): Result<Unit> =
            if (failFor(result)) {
                Result.failure(IllegalStateException("disk full"))
            } else {
                saved += result.download_url
                Result.success(Unit)
            }
    }
}
