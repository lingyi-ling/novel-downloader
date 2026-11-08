package com.lingyi.noveldl.ui.music

import com.lingyi.noveldl.data.Api
import com.lingyi.noveldl.data.MusicTrack
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
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
 * `POST /api/v1/music/export` contract through the music screen's
 * [MusicExportViewModel]: request filtering/destination, both response field
 * spellings (`tracks_written` from REST, `written` from the embedded engine),
 * and the explicit outcomes for no results, no local paths and failures.
 */
class MusicExportViewModelTest {

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
        assertEquals(detail, (outcome as MusicExportOutcome.Failed).reason)
    }

    @Test
    fun export503WithoutMusicSupportReportsUnavailable() = runBlocking {
        val detail = "Music support is not available. Install it with: pip install novel-downloader[music]"
        server.enqueue(json("""{"detail":"$detail"}""", 503))
        Api.configure(server.url("/").toString(), null)

        val outcome = export(listOf(track("1", "/data/music/a.mp3")))

        assertTrue(outcome is MusicExportOutcome.Unavailable)
        assertEquals(detail, (outcome as MusicExportOutcome.Unavailable).reason)
    }

    @Test
    fun exportBeforeApiIsConfiguredReportsUnavailable() = runBlocking {
        val outcome = export(listOf(track("1", "/data/music/a.mp3")))

        assertTrue(outcome is MusicExportOutcome.Unavailable)
        assertEquals(Api.NOT_CONFIGURED_MESSAGE, (outcome as MusicExportOutcome.Unavailable).reason)
        assertEquals(0, server.requestCount)
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
