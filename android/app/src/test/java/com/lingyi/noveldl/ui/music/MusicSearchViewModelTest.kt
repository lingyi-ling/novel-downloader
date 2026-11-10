package com.lingyi.noveldl.ui.music

import androidx.lifecycle.SavedStateHandle
import com.lingyi.noveldl.data.ApiErrorCode
import com.lingyi.noveldl.data.ApiFailure
import com.lingyi.noveldl.data.MUSIC_ALL_PLATFORMS
import com.lingyi.noveldl.data.MusicPlatform
import com.lingyi.noveldl.data.MusicPlatformsResponse
import com.lingyi.noveldl.data.MusicSearchRequest
import com.lingyi.noveldl.data.MusicSearchResponse
import com.lingyi.noveldl.data.MusicTrack
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.setMain
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * Lifecycle contract of the Activity-scoped search owner:
 *
 * - keyword, platform, loaded pages, `search_id` per page and the checkbox
 *   selection outlive the composable (tab switch / rebuild), so coming back
 *   neither re-searches nor loses what was selected;
 * - a new search or a platform switch drops the old pages and selection and
 *   invalidates answers still in flight — a late response of the old request
 *   cannot merge into the new list;
 * - local paths are written back by `provider + id`, so a restored list is
 *   exportable and a later search of the same song reuses the path;
 * - nothing polls: exactly one request per user action.
 *
 * Requests are suspended on a [CompletableDeferred] per call, so the tests can
 * answer them out of order. `Dispatchers.Main` is replaced by `Unconfined`,
 * which makes a launch run inline up to the first suspension and lets each
 * completion resume on the completing test thread.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class MusicSearchViewModelTest {

    @Before
    fun setUp() {
        Dispatchers.setMain(Dispatchers.Unconfined)
    }

    @After
    fun tearDown() {
        Dispatchers.resetMain()
    }

    // ---------- fakes ----------

    /** Every `musicSearch` call parks on its own gate until the test answers. */
    private class FakeSearch {
        val requests = mutableListOf<MusicSearchRequest>()
        private val pending = mutableListOf<CompletableDeferred<MusicSearchResponse>>()

        suspend fun search(request: MusicSearchRequest): MusicSearchResponse {
            requests += request
            val gate = CompletableDeferred<MusicSearchResponse>()
            pending += gate
            return gate.await()
        }

        fun respond(index: Int, response: MusicSearchResponse) {
            pending[index].complete(response)
        }

        fun fail(index: Int, error: Throwable) {
            pending[index].completeExceptionally(error)
        }
    }

    private class FakePlatforms {
        var calls = 0
        var failure: Throwable? = null
        var response: MusicPlatformsResponse = MusicPlatformsResponse(
            listOf(
                MusicPlatform(key = "netease", label = "网易云"),
                MusicPlatform(key = "qq", label = "QQ音乐"),
            ),
        )

        suspend fun load(): MusicPlatformsResponse {
            calls++
            failure?.let { throw it }
            return response
        }
    }

    private fun newModel(
        search: FakeSearch,
        platforms: FakePlatforms = FakePlatforms(),
        initialPlatform: String = MUSIC_ALL_PLATFORMS,
        persist: (String) -> Unit = {},
        savedState: SavedStateHandle? = null,
    ) = MusicSearchViewModel(
        searchPage = search::search,
        platformList = platforms::load,
        initialPlatform = initialPlatform,
        persistPlatform = persist,
        savedState = savedState,
    )

    private fun track(id: String, provider: String = "netease") =
        MusicTrack(provider = provider, id = id, name = "song-$id")

    private fun tracks(range: IntRange, provider: String = "netease") =
        range.map { track(it.toString(), provider) }

    private fun page(
        pageTracks: List<MusicTrack>,
        total: Int = pageTracks.size,
        searchId: String = "sid",
    ) = MusicSearchResponse(
        total = total,
        tracks = pageTracks,
        search_id = searchId,
        platform = MUSIC_ALL_PLATFORMS,
    )

    private fun ids(vararg paired: Pair<MusicTrack, String>): Map<String, String> =
        paired.associate { (track, id) -> MusicJobResults.trackKey(track) to id }

    // ---------- retention across screen rebuilds ----------

    @Test
    fun keywordPagesSelectionAndSearchIdsSurviveTheScreenComingBack() = runBlocking {
        val fake = FakeSearch()
        val model = newModel(fake)
        model.onKeywordChange("jay")

        model.search()
        assertTrue(model.state.value.searching)
        assertEquals(0, fake.requests.single().offset)
        assertEquals("jay", fake.requests.single().keyword)

        // The answer lands while the music tab is not in composition: no
        // collector is required, the StateFlow is the single owner.
        val first = track("1")
        val second = track("2")
        fake.respond(0, page(listOf(first, second), total = 2, searchId = "s1"))
        model.setSelected(second.id, true)

        // Re-reading the state is what a rebuilt screen does; nothing may be
        // requested again for it.
        val restored = model.state.value
        assertEquals("jay", restored.keyword)
        assertEquals(MUSIC_ALL_PLATFORMS, restored.platform)
        assertEquals(listOf("1", "2"), restored.tracks.map { it.id })
        assertEquals(setOf("2"), restored.selected)
        assertEquals(2, restored.totalHits)
        assertEquals(ids(first to "s1", second to "s1"), restored.searchIdsByTrack)
        assertFalse(restored.searching)
        assertEquals(1, fake.requests.size)
    }

    @Test
    fun selectionTogglesIndependentlyAndIsIdempotent() = runBlocking {
        val fake = FakeSearch()
        val model = newModel(fake)
        model.onKeywordChange("x")
        model.search()
        fake.respond(0, page(tracks(1..3)))

        model.setSelected("1", true)
        model.setSelected("1", true)
        model.setSelected("3", true)
        assertEquals(setOf("1", "3"), model.state.value.selected)

        model.setSelected("1", false)
        assertEquals(setOf("3"), model.state.value.selected)
    }

    @Test
    fun aFreshInstanceRestoresThePlatformButNotTheResultPages() {
        var persisted = ""
        val first = newModel(FakeSearch(), persist = { persisted = it })
        first.selectPlatform("qq")
        assertEquals("qq", persisted)

        // A new Activity instance builds a new ViewModel from the stored
        // platform; results are intentionally not persisted to disk.
        val restarted = newModel(FakeSearch(), initialPlatform = persisted)
        assertEquals("qq", restarted.state.value.platform)
        assertEquals("", restarted.state.value.keyword)
        assertTrue(restarted.state.value.tracks.isEmpty())
        assertTrue(restarted.state.value.selected.isEmpty())
    }

    // ---------- process death (SavedStateHandle) ----------

    @Test
    fun savedStateRestoresKeywordPlatformAndSelection() {
        val saved = SavedStateHandle(
            mapOf(
                MusicSearchViewModel.SAVED_KEYWORD to "jay",
                MusicSearchViewModel.SAVED_PLATFORM to "qq",
                MusicSearchViewModel.SAVED_SELECTED to arrayListOf("7", "9"),
            ),
        )

        // What the factory builds after the Activity was killed and restored:
        // the persisted store is the fallback, the saved instance state wins.
        val restored = newModel(FakeSearch(), initialPlatform = "netease", savedState = saved)

        val state = restored.state.value
        assertEquals("jay", state.keyword)
        assertEquals("qq", state.platform)
        assertEquals(setOf("7", "9"), state.selected)
        // Pages are not restored: their search_ids are server-side and expire.
        assertTrue(state.tracks.isEmpty())
        assertTrue(state.searchIdsByTrack.isEmpty())
    }

    @Test
    fun keywordPlatformAndSelectionAreWrittenToSavedState() {
        val saved = SavedStateHandle()
        val model = newModel(FakeSearch(), savedState = saved)

        model.onKeywordChange("jay")
        model.selectPlatform("netease")
        model.setSelected("3", true)
        model.setSelected("5", true)
        model.setSelected("3", false)

        assertEquals("jay", saved.get<String>(MusicSearchViewModel.SAVED_KEYWORD))
        assertEquals("netease", saved.get<String>(MusicSearchViewModel.SAVED_PLATFORM))
        assertEquals(
            listOf("5"),
            saved.get<ArrayList<String>>(MusicSearchViewModel.SAVED_SELECTED),
        )
        // A new search drops the selection from both the state and the handle.
        model.search()
        assertEquals(
            emptyList<String>(),
            saved.get<ArrayList<String>>(MusicSearchViewModel.SAVED_SELECTED),
        )
    }

    // ---------- stale responses ----------

    @Test
    fun lateAnswerOfTheAbandonedSearchCannotOverwriteTheNewList() = runBlocking {
        val fake = FakeSearch()
        val model = newModel(fake)
        model.onKeywordChange("jay")

        model.search()
        // Switching platforms drops the pages and invalidates request 0.
        model.selectPlatform("netease")
        assertFalse(model.state.value.searching)
        assertTrue(model.state.value.tracks.isEmpty())

        model.search()
        fake.respond(1, page(listOf(track("new")), total = 1, searchId = "s2"))
        assertEquals(listOf("new"), model.state.value.tracks.map { it.id })

        // The old request now answers with a longer list; it must be dropped
        // entirely, including its total and search id.
        fake.respond(0, page(tracks(1..5), total = 5, searchId = "s1"))

        val state = model.state.value
        assertEquals(listOf("new"), state.tracks.map { it.id })
        assertEquals(1, state.totalHits)
        assertEquals(ids(track("new") to "s2"), state.searchIdsByTrack)
        assertFalse(state.searching)
    }

    @Test
    fun lateLoadMoreCannotAppendToAResetList() = runBlocking {
        val fake = FakeSearch()
        val model = newModel(fake)
        model.onKeywordChange("jay")
        model.search()
        fake.respond(0, page(tracks(0..19), total = 40, searchId = "p1"))
        assertFalse(model.state.value.endReached)

        model.loadMore()
        assertEquals(20, fake.requests[1].offset)

        // Reset while the page is in flight.
        model.selectPlatform("qq")
        model.search()
        fake.respond(2, page(tracks(0..1, provider = "qq"), total = 2, searchId = "p3"))
        assertEquals(2, model.state.value.tracks.size)

        // The stale page must not resurrect the previous platform's list.
        fake.respond(1, page(tracks(20..39), total = 40, searchId = "p2"))
        assertEquals(listOf("0", "1"), model.state.value.tracks.map { it.id })
        assertEquals(ids(track("0", "qq") to "p3", track("1", "qq") to "p3"), model.state.value.searchIdsByTrack)
        assertFalse(model.state.value.loadingMore)
    }

    // ---------- reset semantics ----------

    @Test
    fun newSearchClearsPagesSelectionAndSearchIdsImmediately() = runBlocking {
        val fake = FakeSearch()
        val model = newModel(fake)
        model.onKeywordChange("jay")
        model.search()
        fake.respond(0, page(tracks(1..3), total = 3, searchId = "old"))
        model.setSelected("1", true)
        model.setSelected("2", true)

        model.search()

        val reset = model.state.value
        assertTrue(reset.searching)
        assertTrue(reset.tracks.isEmpty())
        assertTrue(reset.selected.isEmpty())
        assertTrue(reset.searchIdsByTrack.isEmpty())
        assertEquals(0, reset.totalHits)
        assertFalse(reset.endReached)
        assertFalse(reset.loadMoreFailed)

        fake.respond(1, page(listOf(track("9")), total = 1, searchId = "new"))
        assertEquals(listOf("9"), model.state.value.tracks.map { it.id })
        assertTrue(model.state.value.selected.isEmpty())
        assertEquals(ids(track("9") to "new"), model.state.value.searchIdsByTrack)
    }

    @Test
    fun switchingPlatformClearsResultsAndPersistsTheChoice() = runBlocking {
        val fake = FakeSearch()
        var persisted = ""
        val model = newModel(fake, persist = { persisted = it })
        model.onKeywordChange("jay")
        model.search()
        fake.respond(0, page(tracks(1..2)))
        model.setSelected("1", true)

        model.selectPlatform("qq")

        val state = model.state.value
        assertEquals("qq", state.platform)
        assertEquals("qq", persisted)
        assertTrue(state.tracks.isEmpty())
        assertTrue(state.selected.isEmpty())
        assertTrue(state.searchIdsByTrack.isEmpty())
        assertFalse(state.searching)
        // Selecting the same platform again is a no-op, not a reset.
        val before = state.generation
        model.selectPlatform("qq")
        assertEquals(before, model.state.value.generation)
    }

    @Test
    fun savingTheSamePlatformAgainDoesNotTouchTheStore() {
        var writes = 0
        val model = newModel(FakeSearch(), initialPlatform = "netease", persist = { writes++ })
        model.selectPlatform("netease")
        assertEquals(0, writes)
        model.selectPlatform("qq")
        assertEquals(1, writes)
    }

    // ---------- paging cursor ----------

    @Test
    fun loadMoreResumesAtTheLoadedCountAndAppendsWithoutDuplicates() = runBlocking {
        val fake = FakeSearch()
        val model = newModel(fake)
        model.onKeywordChange("jay")
        model.search()
        fake.respond(0, page(tracks(0..19), total = 60, searchId = "p1"))

        model.loadMore()
        assertEquals(20, fake.requests[1].offset)
        // Overlapping page: id 19 is already loaded.
        fake.respond(1, page(tracks(19..38), total = 60, searchId = "p2"))

        val state = model.state.value
        assertEquals(39, state.tracks.size)
        assertEquals(39, state.tracks.map { it.id }.distinct().size)
        assertEquals(60, state.totalHits)
        assertFalse(state.endReached)
        // Each page keeps its own search id for the re-download; the track at
        // the overlap (19) was returned by both pages, so the later id wins
        // there — both ids resolve it on the server.
        assertEquals("p1", state.searchIdsByTrack[MusicJobResults.trackKey(track("0"))])
        assertEquals("p2", state.searchIdsByTrack[MusicJobResults.trackKey(track("38"))])
        assertEquals("p2", state.searchIdsByTrack[MusicJobResults.trackKey(track("19"))])
    }

    @Test
    fun failedLoadMoreKeepsThePagesAndRetryUsesTheSameOffset() = runBlocking {
        val fake = FakeSearch()
        val model = newModel(fake)
        model.onKeywordChange("jay")
        model.search()
        fake.respond(0, page(tracks(0..19), total = 60, searchId = "p1"))

        model.loadMore()
        fake.fail(1, ApiFailure(ApiErrorCode.NETWORK, "offline"))

        val failed = model.state.value
        assertEquals(20, failed.tracks.size)
        assertTrue(failed.loadMoreFailed)
        assertFalse(failed.loadingMore)
        assertEquals(MusicRequest.LoadMore, failed.errorSource)
        assertEquals(ApiErrorCode.NETWORK, (failed.error as ApiFailure).code)

        model.retry(MusicRequest.LoadMore)
        assertEquals(20, fake.requests[2].offset)
        fake.respond(2, page(tracks(20..39), total = 60, searchId = "p2"))

        val retried = model.state.value
        assertEquals(40, retried.tracks.size)
        assertFalse(retried.loadMoreFailed)
        assertNull(retried.error)
    }

    @Test
    fun aShortPageEndsPagingAndLoadMoreBecomesANoOp() = runBlocking {
        val fake = FakeSearch()
        val model = newModel(fake)
        model.onKeywordChange("jay")
        model.search()
        fake.respond(0, page(tracks(0..4), total = 5, searchId = "p1"))

        assertTrue(model.state.value.endReached)
        model.loadMore()
        assertEquals(1, fake.requests.size)
    }

    @Test
    fun failedSearchReportsItsOwnErrorSourceAndKeepsSearchingAvailable() = runBlocking {
        val fake = FakeSearch()
        val model = newModel(fake)
        model.onKeywordChange("jay")
        model.search()
        fake.fail(0, ApiFailure(ApiErrorCode.HTTP, "boom"))

        val failed = model.state.value
        assertFalse(failed.searching)
        assertEquals(MusicRequest.Search, failed.errorSource)
        assertEquals("boom", (failed.error as ApiFailure).detail)

        // With a keyword the retry repeats the search; with a blank one the
        // UI hides it, because that would be a silent no-op.
        assertEquals(MusicRequest.Search, MusicRetry.target(failed.errorSource, failed.keyword))
        model.onKeywordChange("   ")
        val blank = model.state.value
        assertNull(MusicRetry.target(blank.errorSource, blank.keyword))
    }

    @Test
    fun searchWithoutKeywordOrWhileSearchingSendsNothing() = runBlocking {
        val fake = FakeSearch()
        val model = newModel(fake)

        model.search()
        assertTrue(fake.requests.isEmpty())

        model.onKeywordChange("jay")
        model.search()
        model.search()
        assertEquals(1, fake.requests.size)
    }

    // ---------- platform catalog ----------

    @Test
    fun platformCatalogIsFetchedOncePerViewModel() = runBlocking {
        val platforms = FakePlatforms()
        val model = newModel(FakeSearch(), platforms = platforms)

        model.ensurePlatforms()
        model.ensurePlatforms()

        assertEquals(1, platforms.calls)
        assertEquals(listOf("netease", "qq"), model.state.value.platforms.map { it.key })
        assertTrue(model.state.value.platformsLoaded)
    }

    @Test
    fun failedCatalogLoadIsRetriedFromItsOwnErrorLine() = runBlocking {
        val platforms = FakePlatforms()
        platforms.failure = ApiFailure(ApiErrorCode.NETWORK, "offline")
        val model = newModel(FakeSearch(), platforms = platforms)

        model.ensurePlatforms()
        assertEquals(MusicRequest.Platforms, model.state.value.errorSource)
        assertFalse(model.state.value.platformsLoaded)

        platforms.failure = null
        model.retry(MusicRequest.Platforms)

        val state = model.state.value
        assertEquals(2, platforms.calls)
        assertTrue(state.platformsLoaded)
        assertNull(state.error)
        assertEquals(2, state.platforms.size)
    }

    @Test
    fun aSavedPlatformTheServerDroppedFallsBackToAllAndResetsResults() = runBlocking {
        val fake = FakeSearch()
        var persisted = ""
        val model = newModel(
            fake,
            initialPlatform = "gone",
            persist = { persisted = it },
        )
        model.onKeywordChange("jay")
        model.search()
        fake.respond(0, page(tracks(1..2)))
        assertEquals(2, model.state.value.tracks.size)

        model.ensurePlatforms()

        assertEquals(MUSIC_ALL_PLATFORMS, model.state.value.platform)
        assertEquals(MUSIC_ALL_PLATFORMS, persisted)
        assertTrue(model.state.value.tracks.isEmpty())
        assertTrue(model.state.value.selected.isEmpty())
    }

    // ---------- path backfill ----------

    @Test
    fun jobPathsAreWrittenBackByProviderAndTrackId() = runBlocking {
        val fake = FakeSearch()
        val model = newModel(fake)
        model.onKeywordChange("jay")
        model.search()
        val netease = track("7", provider = "netease")
        val qq = track("8", provider = "qq")
        fake.respond(0, page(listOf(netease, qq), total = 2, searchId = "s1"))

        // The key is provider + id: a path stored under another provider's
        // numeric id must not land on this row, and an unknown key changes
        // nothing at all.
        model.applyPaths(
            mapOf(
                MusicJobResults.trackKey(netease) to "/music/netease-7.mp3",
                "netease\u00008" to "/music/netease-8.mp3",
                "soundcloud\u0000missing" to "/music/none.mp3",
            ),
        )

        val tracks = model.state.value.tracks
        assertEquals("/music/netease-7.mp3", tracks[0].path)
        // Same numeric id as the netease row, different provider: untouched.
        assertEquals("", tracks[1].path)

        model.applyPaths(mapOf(MusicJobResults.trackKey(qq) to "/music/qq-8.mp3"))
        assertEquals("/music/qq-8.mp3", model.state.value.tracks[1].path)
    }

    @Test
    fun aLaterSearchReusesTheStoredPathSoTheListIsExportableAtOnce() = runBlocking {
        val fake = FakeSearch()
        val model = newModel(fake)
        model.onKeywordChange("jay")
        model.search()
        val song = track("1")
        fake.respond(0, page(listOf(song), total = 1, searchId = "s1"))
        model.applyPaths(mapOf(MusicJobResults.trackKey(song) to "/music/1.mp3"))

        // A brand-new search returns the same song from the server (no path in
        // the response); the ViewModel fills it in from the stored map.
        model.onKeywordChange("jay chou")
        model.search()
        fake.respond(1, page(listOf(track("1")), total = 1, searchId = "s2"))

        assertEquals("/music/1.mp3", model.state.value.tracks.single().path)
    }

    @Test
    fun applyingPathsBeforeAnySearchIsRememberedForTheNextPage() = runBlocking {
        val fake = FakeSearch()
        val model = newModel(fake)
        val song = track("1")

        // E.g. the job finished on another tab before this screen was rebuilt.
        model.applyPaths(mapOf(MusicJobResults.trackKey(song) to "/music/1.mp3"))
        model.onKeywordChange("jay")
        model.search()
        fake.respond(0, page(listOf(track("1")), total = 1, searchId = "s1"))

        assertEquals("/music/1.mp3", model.state.value.tracks.single().path)
    }
}
