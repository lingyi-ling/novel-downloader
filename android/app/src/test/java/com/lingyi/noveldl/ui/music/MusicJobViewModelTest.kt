package com.lingyi.noveldl.ui.music

import com.lingyi.noveldl.data.Api
import com.lingyi.noveldl.data.ApiErrorCode
import com.lingyi.noveldl.data.ApiFailure
import com.lingyi.noveldl.data.MusicPathPersistence
import com.lingyi.noveldl.data.MusicTrack
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.setMain
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
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
 * Lifecycle contract of the Activity-scoped job owner:
 *
 * - a second "download selected" tap while a job runs submits nothing;
 * - the job (and its paths/events) outlives the composable, so a tab switch
 *   neither cancels the watch nor loses what it produced;
 * - a failed run is repeated by [MusicJobViewModel.retry] with the same input;
 * - the watch parks while the host is in the background and resumes after.
 *
 * The ViewModel runs on `Dispatchers.Main`; tests swap in `Dispatchers.Unconfined`
 * so calling `start()` executes the coroutine inline and the assertions can
 * wait on the observable state instead of on a timer.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class MusicJobViewModelTest {

    private lateinit var server: MockWebServer

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
        resetService()
        Api.configure(server.url("/").toString(), null)
        Dispatchers.setMain(Dispatchers.Unconfined)
    }

    @After
    fun tearDown() {
        Dispatchers.resetMain()
        server.shutdown()
        resetService()
    }

    /** [Api] is a singleton; clear the cached Retrofit service between tests. */
    private fun resetService() {
        val field = Api::class.java.getDeclaredField("service")
        field.isAccessible = true
        field.set(Api, null)
    }

    private fun json(body: String, code: Int = 200): MockResponse =
        MockResponse()
            .setResponseCode(code)
            .setHeader("Content-Type", "application/json")
            .setBody(body)

    private fun created(jobId: String) = json("""{"job_id":"$jobId"}""")

    private fun done(jobId: String, body: String) = json(
        """{"job_id":"$jobId","status":"done","progress":{"done":1,"total":1},"results":$body}""",
    )

    private fun track(id: String) = MusicTrack(provider = "netease", id = id, name = "song-$id")

    /** In-memory stand-in for the on-disk music path cache. */
    private class RecordingPaths(initial: Map<String, String> = emptyMap()) :
        MusicPathPersistence {
        var stored: Map<String, String> = initial
        override suspend fun snapshot(): Map<String, String> = stored
        override suspend fun putAll(paths: Map<String, String>) {
            stored = stored + paths
        }

        override suspend fun remove(keys: Collection<String>) {
            stored = stored - keys.toSet()
        }
    }

    private fun idsByTrack(vararg pairs: Pair<MusicTrack, String>): Map<String, String> =
        pairs.associate { (track, id) -> MusicJobResults.trackKey(track) to id }

    private suspend fun MusicJobViewModel.awaitIdle(): MusicJobUiState =
        withTimeout(5_000) {
            state.first { !it.running && it.total > 0 }
        }

    @Test
    fun secondTapWhileRunningSubmitsNothing() = runBlocking {
        server.enqueue(created("j1"))
        server.enqueue(done("j1", """[{"status":"ok","path":"/a/1.mp3"}]"""))
        val model = MusicJobViewModel({ Api.service() }, pollIntervalMs = 0)
        val chosen = listOf(track("1"))

        assertTrue(model.start("netease", chosen, idsByTrack(chosen.single() to "sid")))
        // Even before the first poll lands, the second tap is a no-op.
        assertFalse(model.start("netease", chosen, idsByTrack(chosen.single() to "sid")))

        val idle = model.awaitIdle()
        assertEquals(1, idle.total)
        // Exactly one job creation and one poll.
        assertEquals(2, server.requestCount)
    }

    @Test
    fun jobOutlivesTheScreenAndKeepsItsPaths() = runBlocking {
        server.enqueue(created("j1"))
        server.enqueue(
            done(
                "j1",
                """[
                    {"status":"ok","path":"/a/1.mp3","download_url":"/dl/1.mp3"},
                    {"status":"ok","path":"/a/2.mp3","download_url":"/dl/2.mp3"}
                ]""",
            ),
        )
        val model = MusicJobViewModel({ Api.service() }, pollIntervalMs = 0)
        val first = track("1")
        val second = track("2")

        // The screen would be gone after this call in the real app; the
        // ViewModel still owns the run.
        assertTrue(
            model.start(
                "netease",
                listOf(first, second),
                idsByTrack(first to "sid", second to "sid"),
            ),
        )

        val finished = model.awaitIdle()
        assertFalse(finished.running)
        assertEquals(2, finished.done)
        assertEquals("/a/1.mp3", finished.paths[MusicJobResults.trackKey(first)])
        assertEquals("/a/2.mp3", finished.paths[MusicJobResults.trackKey(second)])
        assertNull(finished.failure)
    }

    @Test
    fun finishedRunIsDeliveredAsAnEventEvenWithoutACollector() = runBlocking {
        server.enqueue(created("j1"))
        server.enqueue(done("j1", """[{"status":"ok","path":"/a/1.mp3"}]"""))
        val model = MusicJobViewModel({ Api.service() }, pollIntervalMs = 0)
        val chosen = listOf(track("1"))

        model.start("netease", chosen, idsByTrack(chosen.single() to "sid"))
        model.awaitIdle()

        // The channel buffers, so the screen can collect after coming back.
        val event = withTimeout(5_000) { model.incoming.first() }
        val finished = event as MusicJobEvent.Finished
        assertEquals(1, finished.counts.ok)
    }

    @Test
    fun retryRepeatsTheFailedRunWithTheSameSelection() = runBlocking {
        server.enqueue(json("""{"detail":"boom"}""", 500))
        server.enqueue(created("j2"))
        server.enqueue(done("j2", """[{"status":"ok","path":"/a/1.mp3"}]"""))
        val model = MusicJobViewModel({ Api.service() }, pollIntervalMs = 0)
        val chosen = listOf(track("1"))

        model.start("netease", chosen, idsByTrack(chosen.single() to "sid"))
        val failed = model.awaitIdle()
        val failure = failed.failure as ApiFailure
        assertEquals(ApiErrorCode.HTTP, failure.code)
        assertEquals("boom", failure.detail)

        assertTrue(model.retry())

        val retried = model.awaitIdle()
        assertNull(retried.failure)
        assertEquals("/a/1.mp3", retried.paths[MusicJobResults.trackKey(chosen.single())])
        assertEquals(3, server.requestCount)
    }

    @Test
    fun retryResubmitsOnlyTheCheckedTracks() = runBlocking {
        server.enqueue(json("""{"detail":"boom"}""", 500))
        server.enqueue(created("j2"))
        server.enqueue(done("j2", """[{"status":"ok","path":"/a/2.mp3"}]"""))
        val model = MusicJobViewModel({ Api.service() }, pollIntervalMs = 0)
        val unchecked = track("1")
        val checked = track("2")

        // The screen resolves the checkbox set to tracks before calling start,
        // so the unchecked row never reaches the ViewModel.
        assertTrue(model.start("netease", listOf(checked), idsByTrack(checked to "sid")))
        model.awaitIdle()
        assertTrue(model.retry())
        val retried = model.awaitIdle()

        // Both the failed attempt and its retry carry only the checked track.
        val firstBody = Json.parseToJsonElement(server.takeRequest().body.readUtf8()).jsonObject
        val retryBody = Json.parseToJsonElement(server.takeRequest().body.readUtf8()).jsonObject
        listOf(firstBody, retryBody).forEach { body ->
            val sent = body["tracks"]!!.jsonArray
            assertEquals(1, sent.size)
            assertEquals(
                "2",
                sent.single().jsonObject["id"]!!.jsonPrimitive.content,
            )
        }
        assertEquals(1, retried.total)
        assertEquals("/a/2.mp3", retried.paths[MusicJobResults.trackKey(checked)])
        assertNull(retried.paths[MusicJobResults.trackKey(unchecked)])
    }

    @Test
    fun finishedPathsArePersistedAndFailedTracksAreRemoved() = runBlocking {
        server.enqueue(created("j1"))
        server.enqueue(
            done(
                "j1",
                """[
                    {"status":"ok","path":"/a/1.mp3"},
                    {"status":"fail","reason":"boom"}
                ]""",
            ),
        )
        val second = track("2")
        val persistence = RecordingPaths(
            mapOf(MusicJobResults.trackKey(second) to "/old/2.mp3"),
        )
        val model = MusicJobViewModel(
            { Api.service() },
            pollIntervalMs = 0,
            paths = persistence,
        )
        val first = track("1")

        model.start(
            "netease",
            listOf(first, second),
            idsByTrack(first to "sid", second to "sid"),
        )
        model.awaitIdle()

        // The successful path is on disk (the export ViewModel reads it after
        // a restart); the explicit failure drops the track's old entry.
        assertEquals("/a/1.mp3", persistence.stored[MusicJobResults.trackKey(first)])
        assertFalse(persistence.stored.containsKey(MusicJobResults.trackKey(second)))
    }

    @Test
    fun watchParksWhileBackgroundedAndResumesOnReturn() = runBlocking {
        server.enqueue(created("j1"))
        server.enqueue(done("j1", """[{"status":"ok","path":"/a/1.mp3"}]"""))
        val model = MusicJobViewModel({ Api.service() }, pollIntervalMs = 0)
        val chosen = listOf(track("1"))

        model.setForeground(false)
        model.start("netease", chosen, idsByTrack(chosen.single() to "sid"))
        // The creation request still goes out (the user tapped in the
        // foreground); polling must not. Wait for the create to arrive.
        server.takeRequest()
        assertEquals(1, server.requestCount)

        model.setForeground(true)
        val finished = model.awaitIdle()

        assertEquals(2, server.requestCount)
        assertEquals("/a/1.mp3", finished.paths[MusicJobResults.trackKey(chosen.single())])
    }
}
