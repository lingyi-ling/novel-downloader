package com.lingyi.noveldl.ui.music

import com.lingyi.noveldl.data.Api
import com.lingyi.noveldl.data.ApiErrorCode
import com.lingyi.noveldl.data.ApiFailure
import com.lingyi.noveldl.data.MusicTrack
import java.util.concurrent.TimeUnit
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.setMain
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.int
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
 * `POST /api/v1/music/export` contract through the music screen's
 * [MusicExportViewModel]: request filtering/destination, both response field
 * spellings (`tracks_written` from REST, `written` from the embedded engine),
 * the explicit outcomes for no results, no local paths and failures, and the
 * ViewModel-owned state that must survive a tab switch (running, last path,
 * failure).
 */
@OptIn(ExperimentalCoroutinesApi::class)
class MusicExportViewModelTest {

    private lateinit var server: MockWebServer

    @Before
    fun startServer() {
        // `start()` runs in viewModelScope (Dispatchers.Main); Unconfined makes
        // the launch run inline up to the first suspension.
        Dispatchers.setMain(Dispatchers.Unconfined)
        server = MockWebServer()
        server.start()
        resetService()
    }

    @After
    fun stopServer() {
        server.shutdown()
        resetService()
        Dispatchers.resetMain()
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

    private fun track(id: String, path: String = "") = MusicTrack(
        provider = "netease",
        id = id,
        name = "song-$id",
        artists = "周杰伦",
        album = "叶惠美",
        duration_s = 269,
        path = path,
    )

    private suspend fun export(tracks: List<MusicTrack>): MusicExportOutcome =
        MusicExportViewModel().export("netease", tracks)

    // ---------- success ----------

    @Test
    fun exportSendsOnlyPathTracksAndDefaultsToTheirDirectory() = runBlocking {
        server.enqueue(
            json(
                """{"path":"/data/music/playlist.m3u8","tracks_written":2,"skipped":0}""",
            ),
        )
        Api.configure(server.url("/").toString(), null)

        val outcome = export(
            listOf(
                track("1", "/data/music/a.mp3"),
                track("2"),
                track("3", "/data/music/c.flac"),
            ),
        )

        assertTrue(outcome is MusicExportOutcome.Success)
        val success = outcome as MusicExportOutcome.Success
        assertEquals("/data/music/playlist.m3u8", success.path)
        assertEquals(2, success.written)
        // The client dropped the pathless track before the request.
        assertEquals(1, success.skipped)

        val request = server.takeRequest()
        assertEquals("POST", request.method)
        assertEquals("/api/v1/music/export", request.requestUrl!!.encodedPath)
        val body = Json.parseToJsonElement(request.body.readUtf8()).jsonObject
        assertEquals("netease", body["platform"]!!.jsonPrimitive.content)
        assertEquals(
            "/data/music/${MUSIC_PLAYLIST_FILE_NAME}",
            body["out_path"]!!.jsonPrimitive.content,
        )
        val sent = body["tracks"]!!.jsonArray
        assertEquals(2, sent.size)
        assertEquals(
            listOf("1", "3"),
            sent.map { it.jsonObject["id"]!!.jsonPrimitive.content },
        )
        assertEquals(
            "/data/music/a.mp3",
            sent[0].jsonObject["path"]!!.jsonPrimitive.content,
        )
    }

    @Test
    fun exportParsesEmbeddedEngineWrittenField() = runBlocking {
        // The embedded server answers with `written` instead of
        // `tracks_written`; both spellings must drive the same UI value.
        server.enqueue(
            json("""{"written":1,"skipped":0,"path":"/data/music/playlist.m3u8"}"""),
        )
        Api.configure(server.url("/").toString(), null)

        val outcome = export(listOf(track("1", "/data/music/a.mp3")))

        val success = outcome as MusicExportOutcome.Success
        assertEquals(1, success.written)
        assertEquals(0, success.skipped)
        assertEquals("/data/music/playlist.m3u8", success.path)
    }

    @Test
    fun exportOmitsOutPathWhenTracksOnlyHaveFileNames() = runBlocking {
        // A bare file name has no directory: let the server pick its default
        // (its own music output directory) instead of inventing one.
        server.enqueue(
            json("""{"tracks_written":1,"skipped":0,"path":"/srv/playlist.m3u8"}"""),
        )
        Api.configure(server.url("/").toString(), null)

        export(listOf(track("1", "a.mp3")))

        val body = Json.parseToJsonElement(server.takeRequest().body.readUtf8()).jsonObject
        assertTrue("out_path" !in body)
    }

    // ---------- edge cases: no request sent ----------

    @Test
    fun exportWithoutSearchResultsReportsNoResults() = runBlocking {
        val outcome = export(emptyList())

        assertEquals(MusicExportOutcome.NoResults, outcome)
        assertEquals(0, server.requestCount)
    }

    @Test
    fun exportWithoutLocalPathsReportsNoLocalPaths() = runBlocking {
        val outcome = export(listOf(track("1"), track("2")))

        assertEquals(MusicExportOutcome.NoLocalPaths, outcome)
        assertEquals(0, server.requestCount)
    }

    // ---------- failures ----------

    @Test
    fun exportWriteFailureSurfacesTheServerDetail() = runBlocking {
        val detail = "播放列表写入失败: /data/music/playlist.m3u8 (PermissionError)"
        server.enqueue(json("""{"detail":"$detail"}""", 500))
        Api.configure(server.url("/").toString(), null)

        val outcome = export(listOf(track("1", "/data/music/a.mp3")))

        assertTrue(outcome is MusicExportOutcome.Failed)
        val failure = (outcome as MusicExportOutcome.Failed).error as ApiFailure
        assertEquals(ApiErrorCode.HTTP, failure.code)
        assertEquals(detail, failure.detail)
    }

    @Test
    fun export503WithoutMusicSupportReportsUnavailable() = runBlocking {
        val detail = "Music support is not available. Install it with: pip install novel-downloader[music]"
        server.enqueue(json("""{"detail":"$detail"}""", 503))
        Api.configure(server.url("/").toString(), null)

        val outcome = export(listOf(track("1", "/data/music/a.mp3")))

        assertTrue(outcome is MusicExportOutcome.Unavailable)
        val failure = (outcome as MusicExportOutcome.Unavailable).error as ApiFailure
        assertEquals(ApiErrorCode.HTTP, failure.code)
        assertEquals(detail, failure.detail)
    }

    @Test
    fun exportBeforeApiIsConfiguredReportsUnavailable() = runBlocking {
        val outcome = export(listOf(track("1", "/data/music/a.mp3")))

        assertTrue(outcome is MusicExportOutcome.Unavailable)
        // The screen localizes the code; the data layer only reports it.
        assertEquals(
            ApiErrorCode.NOT_CONFIGURED,
            ((outcome as MusicExportOutcome.Unavailable).error as ApiFailure).code,
        )
        assertEquals(0, server.requestCount)
    }

    // ---------- activity-scoped state (tab switch) ----------

    /** Waits for the ViewModel to report a state matching [predicate]. */
    private suspend fun awaitState(
        model: MusicExportViewModel,
        predicate: (MusicExportUiState) -> Boolean,
    ): MusicExportUiState = withTimeout(5_000) { model.state.first(predicate) }

    @Test
    fun startKeepsTheRequestAndTheWrittenPathAliveAfterTheScreenIsGone() = runBlocking {
        // The body delay keeps the export in flight long enough to observe
        // `running`, which is what the button and progress row render.
        server.enqueue(
            json("""{"path":"/data/music/playlist.m3u8","tracks_written":1,"skipped":0}""")
                .setBodyDelay(200, TimeUnit.MILLISECONDS),
        )
        Api.configure(server.url("/").toString(), null)
        val model = MusicExportViewModel()

        assertTrue(model.start("netease", listOf(track("1", "/data/music/a.mp3"))))
        // Owned by the ViewModel, not by the composition: a tab switch no
        // longer cancels it.
        assertTrue(model.state.value.running)

        val done = awaitState(model) { !it.running }
        assertEquals("/data/music/playlist.m3u8", done.lastPath)
        assertEquals(1, done.lastWritten)
        assertEquals(0, done.lastSkipped)
        assertNull(done.failure)

        // A rebuilt screen reads this state, so the path is still displayed.
        assertEquals("/data/music/playlist.m3u8", model.state.value.lastPath)
    }

    @Test
    fun aSecondStartIsIgnoredWhileTheFirstExportIsInFlight() = runBlocking {
        server.enqueue(
            json("""{"tracks_written":1,"skipped":0,"path":"/p.m3u8"}""")
                .setBodyDelay(200, TimeUnit.MILLISECONDS),
        )
        Api.configure(server.url("/").toString(), null)
        val model = MusicExportViewModel()

        assertTrue(model.start("netease", listOf(track("1", "/a.mp3"))))
        assertFalse(model.start("netease", listOf(track("2", "/b.mp3"))))

        assertEquals("/p.m3u8", awaitState(model) { !it.running }.lastPath)
        assertEquals(1, server.requestCount)
    }

    @Test
    fun failureStaysAvailableAndRetryRepeatsTheSameRequest() = runBlocking {
        server.enqueue(json("""{"detail":"disk full"}""", 500))
        Api.configure(server.url("/").toString(), null)
        val model = MusicExportViewModel()

        model.start("netease", listOf(track("1", "/a.mp3")))
        val failed = awaitState(model) { !it.running }
        assertTrue(failed.failure is MusicExportOutcome.Failed)
        assertNull(failed.lastPath)
        // The snackbar outcome is buffered while no screen collects it.
        assertTrue(withTimeout(5_000) { model.incoming.first() } is MusicExportOutcome.Failed)

        server.enqueue(json("""{"tracks_written":1,"skipped":0,"path":"/p.m3u8"}"""))
        assertTrue(model.retry())
        val retried = awaitState(model) { !it.running }
        assertEquals("/p.m3u8", retried.lastPath)
        assertNull(retried.failure)
        assertEquals(2, server.requestCount)

        // A later failure must not erase the last successful path: that is
        // exactly what the user needs to locate the file after a rebuild.
        server.enqueue(json("""{"detail":"disk full"}""", 500))
        model.start("netease", listOf(track("1", "/a.mp3")))
        val failedAgain = awaitState(model) { !it.running && it.failure != null }
        assertEquals("/p.m3u8", failedAgain.lastPath)
    }

    @Test
    fun noLocalPathsIsReportedWithoutTouchingTheLastSuccessfulPath() = runBlocking {
        server.enqueue(json("""{"tracks_written":1,"skipped":0,"path":"/p.m3u8"}"""))
        Api.configure(server.url("/").toString(), null)
        val model = MusicExportViewModel()
        model.start("netease", listOf(track("1", "/a.mp3")))
        awaitState(model) { !it.running }

        // Nothing downloaded since then: the outcome is a message, not an
        // error line, and the previous path stays visible.
        model.start("netease", listOf(track("2")))
        val state = awaitState(model) { !it.running && it.lastPath != null }
        assertNull(state.failure)
        assertEquals("/p.m3u8", state.lastPath)
        assertEquals(1, server.requestCount)
    }

    // ---------- destination helper ----------

    @Test
    fun planUsesTheFirstUsableDirectory() {
        val plan = MusicExport.plan(
            listOf(
                track("1", "   "),
                track("2", "C:\\Music\\a.mp3"),
                track("3", "/sdcard/Music/b.mp3"),
            ),
        )

        assertEquals(listOf("2", "3"), plan!!.tracks.map { it.id })
        assertEquals(1, plan.skippedTracks)
        // The destination keeps the track directory; the joined file name
        // uses '/' (accepted by both the POSIX engine and Python on Windows).
        assertEquals("C:\\Music/$MUSIC_PLAYLIST_FILE_NAME", plan.outPath)
    }

    @Test
    fun directoryOfHandlesBothSeparatorsAndRootlessNames() {
        assertEquals("/sdcard/Music", MusicExport.directoryOf("/sdcard/Music/a.mp3"))
        assertEquals("C:\\Music", MusicExport.directoryOf("C:\\Music\\a.mp3"))
        assertEquals("", MusicExport.directoryOf("a.mp3"))
        assertEquals("", MusicExport.directoryOf("/a.mp3"))
    }
}
