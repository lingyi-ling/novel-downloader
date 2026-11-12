package com.lingyi.noveldl.ui.music

import android.content.Context
import androidx.lifecycle.SavedStateHandle
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.createSavedStateHandle
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.lingyi.noveldl.data.Api
import com.lingyi.noveldl.data.MUSIC_ALL_PLATFORMS
import com.lingyi.noveldl.data.MusicPlatform
import com.lingyi.noveldl.data.MusicPlatformsResponse
import com.lingyi.noveldl.data.MusicSearchRequest
import com.lingyi.noveldl.data.MusicSearchResponse
import com.lingyi.noveldl.data.MusicTrack
import com.lingyi.noveldl.data.SelectionStore
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

/**
 * Everything the music search screen renders, owned by an Activity-scoped
 * ViewModel instead of the composable.
 *
 * Why it lives here: a tab switch (or any recomposition that drops the music
 * entry from composition) used to discard the keyword, the platform, the
 * loaded pages, the `search_id` mapping and the checkbox selection, forcing the
 * user to search again for the same list. The state is a [StateFlow], so the
 * screen only reads it; the requests own the same Activity lifetime and keep
 * running across navigation.
 *
 * [generation] is bumped by every result-discarding action (new search,
 * platform switch, platform falling out of the server's list). A response only
 * lands when its generation is still current, so a slow answer of an old
 * request can never overwrite a freshly reset list.
 *
 * The paging cursor is the loaded track count: the next page is requested with
 * `offset = tracks.size` (see [MusicPaging]). [searchIdsByTrack] remembers one
 * `search_id` per page, because the server caches the provider objects per
 * search request.
 */
data class MusicSearchUiState(
    val keyword: String = "",
    val platform: String = MUSIC_ALL_PLATFORMS,
    /** Platform catalog for the picker; loaded once per ViewModel lifetime. */
    val platforms: List<MusicPlatform> = emptyList(),
    val platformsLoading: Boolean = false,
    /** True once the catalog arrived; a tab switch then skips the request. */
    val platformsLoaded: Boolean = false,
    val tracks: List<MusicTrack> = emptyList(),
    /** `provider\u0000id` of each track -> the `search_id` of its page. */
    val searchIdsByTrack: Map<String, String> = emptyMap(),
    /** Hit total reported by the platform; 0 when unknown. */
    val totalHits: Int = 0,
    /** No further page is requested, either because the end was reached or
     * the last page was short/empty. */
    val endReached: Boolean = false,
    /**
     * `supports_offset` of the last response: `false` means the provider
     * cannot page (musicdl platforms, aggregate searches) and `endReached` is
     * set after the first page. `null` means the server predates the flag.
     */
    val supportsOffset: Boolean? = null,
    val loadingMore: Boolean = false,
    val loadMoreFailed: Boolean = false,
    /**
     * Checked tracks, keyed by [MusicJobResults.trackKey] (`provider\u0000id`),
     * kept across tab switches; cleared by new searches. The key includes the
     * provider because two platforms routinely use the same numeric song id —
     * a bare-id set would check/download both rows together.
     */
    val selectedKeys: Set<String> = emptySet(),
    val searching: Boolean = false,
    /**
     * Failure of the request named by [errorSource]; the screen localizes the
     * [com.lingyi.noveldl.data.ApiErrorCode] it carries.
     */
    val error: Throwable? = null,
    val errorSource: MusicRequest = MusicRequest.Search,
    /** Stale-response guard; bumped by every result-discarding action. */
    val generation: Int = 0,
)

/**
 * Activity-scoped owner of the music search state.
 *
 * [searchPage] and [platformList] are injectable so JVM tests can suspend each
 * request at a chosen point and answer them out of order; the production
 * defaults call the configured API. [persistPlatform] writes the chosen
 * platform through [SelectionStore], [initialPlatform] is read from it when the
 * ViewModel is created (Activity retention keeps that one instance).
 *
 * [savedState] additionally carries the keyword, platform and checkbox
 * selection through process death (the system restores the saved instance
 * state, so the user does not have to retype/reselect after the Activity was
 * killed in the background). The loaded pages are deliberately not saved: they
 * hold server-side `search_id`s that expire, so the list always comes from a
 * fresh search.
 *
 * Nothing here polls: requests are only made for user actions (search, load
 * more, platform retry) plus the single platform-catalog fetch the visible
 * screen asks for.
 */
class MusicSearchViewModel @JvmOverloads constructor(
    private val searchPage: suspend (MusicSearchRequest) -> MusicSearchResponse = { request ->
        Api.service().musicSearch(request)
    },
    private val platformList: suspend () -> MusicPlatformsResponse = {
        Api.service().musicPlatforms()
    },
    initialPlatform: String = MUSIC_ALL_PLATFORMS,
    private val persistPlatform: (String) -> Unit = {},
    private val savedState: SavedStateHandle? = null,
) : ViewModel() {

    private val _state = MutableStateFlow(
        MusicSearchUiState(
            keyword = savedState?.get<String>(SAVED_KEYWORD).orEmpty(),
            // The saved instance state is fresher than the persisted store
            // (that write is asynchronous), so it wins when present.
            platform = normalizePlatform(
                savedState?.get<String>(SAVED_PLATFORM)?.takeIf { it.isNotBlank() }
                    ?: initialPlatform,
            ),
            selectedKeys = MusicJobResults.sanitizeSelection(
                // Older builds saved bare ids; sanitizeSelection drops them
                // instead of letting a numeric id match another provider.
                savedState?.get<ArrayList<String>>(SAVED_SELECTED),
            ),
        ),
    )
    val state: StateFlow<MusicSearchUiState> = _state.asStateFlow()

    /**
     * Local paths reported by [MusicJobViewModel], keyed like
     * [MusicJobResults.trackKey]. Kept apart from the visible list so a later
     * search of the same song is populated again instead of re-downloaded,
     * and merged onto every page as it arrives.
     */
    private var paths: Map<String, String> = emptyMap()

    fun onKeywordChange(value: String) {
        savedState?.set(SAVED_KEYWORD, value)
        _state.value = _state.value.copy(keyword = value)
    }

    /**
     * Switches the platform: pages and selection belong to the old one, so the
     * list is dropped and the in-flight answer invalidated. The choice is
     * persisted for the next launch and saved for process restoration.
     */
    fun selectPlatform(key: String) {
        val platform = normalizePlatform(key)
        if (platform == _state.value.platform) return
        persistPlatform(platform)
        savedState?.set(SAVED_PLATFORM, platform)
        _state.value = _state.value.copy(platform = platform, searching = false)
        // Searching is reset above before the guard bump, so the spinner of an
        // abandoned request cannot keep the new platform's search disabled.
        resetResults()
    }

    /**
     * Fetches the platform catalog once; returning to the tab reuses it.
     * Called from the screen's `LaunchedEffect` (i.e. only while visible).
     */
    fun ensurePlatforms() {
        val current = _state.value
        if (current.platformsLoaded || current.platformsLoading) return
        loadPlatforms()
    }

    /** (Re)loads the platform catalog; also the retry for a failed load. */
    fun loadPlatforms() {
        if (_state.value.platformsLoading) return
        _state.value = _state.value.copy(platformsLoading = true)
        viewModelScope.launch {
            val outcome = Api.call { platformList() }
            outcome.fold(
                onSuccess = { response ->
                    val keys = response.platforms.mapTo(HashSet()) { it.key }
                    val current = _state.value
                    _state.value = current.copy(
                        platforms = response.platforms,
                        platformsLoading = false,
                        platformsLoaded = true,
                        // A successful retry only clears its own error line.
                        error = if (current.errorSource == MusicRequest.Platforms) {
                            null
                        } else {
                            current.error
                        },
                    )
                    val platform = _state.value.platform
                    // Keep "all" (or a still-valid saved key); fall back to the
                    // aggregate default rather than an arbitrary first platform.
                    if (platform != MUSIC_ALL_PLATFORMS && platform !in keys) {
                        persistPlatform(MUSIC_ALL_PLATFORMS)
                        _state.value = _state.value.copy(
                            platform = MUSIC_ALL_PLATFORMS,
                            searching = false,
                        )
                        // The results came from a platform the server no longer
                        // offers; keeping them would send that key on export or
                        // download, which the server now rejects.
                        resetResults()
                    }
                },
                onFailure = { error ->
                    _state.value = _state.value.copy(
                        platformsLoading = false,
                        error = error,
                        errorSource = MusicRequest.Platforms,
                    )
                },
            )
        }
    }

    /**
     * Runs the first page for the current keyword/platform. No-op while the
     * keyword is blank or another search is in flight.
     */
    fun search() {
        val current = _state.value
        val query = current.keyword.trim()
        if (query.isEmpty() || current.platform.isEmpty() || current.searching) return
        val platform = current.platform
        val generation = resetResults()
        _state.value = _state.value.copy(
            searching = true,
            error = null,
            errorSource = MusicRequest.Search,
        )
        viewModelScope.launch {
            val outcome = Api.call {
                searchPage(
                    MusicSearchRequest(
                        platform = platform,
                        keyword = query,
                        limit = MUSIC_PAGE_SIZE,
                        offset = 0,
                    ),
                )
            }
            val stale = _state.value.generation != generation
            outcome.fold(
                onSuccess = { page ->
                    if (stale) return@fold
                    // Keep any path a previous job already wrote for these
                    // tracks, so export works without downloading again.
                    val loaded = MusicPaging.merge(emptyList(), page.tracks)
                    _state.value = _state.value.copy(
                        tracks = MusicJobResults.applyPaths(loaded, paths),
                        searchIdsByTrack = MusicJobResults.rememberSearchIds(
                            emptyMap(),
                            page.tracks,
                            page.search_id,
                        ),
                        totalHits = page.total,
                        supportsOffset = page.supports_offset,
                        endReached = !MusicPaging.hasMore(
                            loaded.size,
                            page.total,
                            page.tracks.size,
                            0,
                            MUSIC_PAGE_SIZE,
                            page.supports_offset,
                        ),
                        searching = false,
                    )
                },
                onFailure = { error ->
                    if (stale) return@fold
                    _state.value = _state.value.copy(
                        searching = false,
                        error = error,
                        errorSource = MusicRequest.Search,
                    )
                },
            )
        }
    }

    /**
     * Loads the next page at `offset = tracks.size` and appends it (deduped by
     * `provider + id`, see [MusicPaging.merge]). Guarded so a page is only ever
     * requested when the previous search finished, no fetch is in flight and
     * the end was not reached.
     */
    fun loadMore() {
        val current = _state.value
        val query = current.keyword.trim()
        if (query.isEmpty() || current.platform.isEmpty()) return
        // A provider that cannot page (supports_offset = false) would answer
        // with the first page or an empty one; never ask it again.
        if (current.supportsOffset == false) return
        if (current.searching || current.loadingMore || current.endReached) return
        val generation = current.generation
        val platform = current.platform
        val offset = current.tracks.size
        _state.value = _state.value.copy(
            loadingMore = true,
            loadMoreFailed = false,
            error = null,
        )
        viewModelScope.launch {
            val outcome = Api.call {
                searchPage(
                    MusicSearchRequest(
                        platform = platform,
                        keyword = query,
                        limit = MUSIC_PAGE_SIZE,
                        offset = offset,
                    ),
                )
            }
            val stale = _state.value.generation != generation
            outcome.fold(
                onSuccess = { page ->
                    if (stale) return@fold
                    val previous = _state.value.tracks.size
                    val merged = MusicPaging.merge(_state.value.tracks, page.tracks)
                    _state.value = _state.value.copy(
                        tracks = MusicJobResults.applyPaths(merged, paths),
                        // Results from this page are only resolvable with this
                        // page's search id, so remember it for these tracks.
                        searchIdsByTrack = MusicJobResults.rememberSearchIds(
                            _state.value.searchIdsByTrack,
                            page.tracks,
                            page.search_id,
                        ),
                        totalHits = page.total,
                        supportsOffset = page.supports_offset ?: _state.value.supportsOffset,
                        endReached = !MusicPaging.hasMore(
                            merged.size,
                            page.total,
                            page.tracks.size,
                            previous,
                            MUSIC_PAGE_SIZE,
                            page.supports_offset,
                        ),
                        loadingMore = false,
                    )
                },
                onFailure = { error ->
                    if (stale) return@fold
                    _state.value = _state.value.copy(
                        loadingMore = false,
                        loadMoreFailed = true,
                        error = error,
                        errorSource = MusicRequest.LoadMore,
                    )
                },
            )
        }
    }

    /** Repeats the request the visible error came from. */
    fun retry(target: MusicRequest) {
        when (target) {
            MusicRequest.Platforms -> loadPlatforms()
            MusicRequest.Search -> search()
            MusicRequest.LoadMore -> loadMore()
        }
    }

    /**
     * Writes [track]'s checkbox state; the selection survives tab switches.
     * The key is `provider + id`, so two rows that share a numeric id on
     * different platforms toggle independently.
     */
    fun setSelected(track: MusicTrack, checked: Boolean) {
        val key = MusicJobResults.trackKey(track)
        val selected = _state.value.selectedKeys
        if ((key in selected) == checked) return
        val updated = if (checked) selected + key else selected - key
        savedState?.set(SAVED_SELECTED, ArrayList(updated))
        _state.value = _state.value.copy(selectedKeys = updated)
    }

    /**
     * Checks or unchecks every currently loaded row (the "select all" box).
     * Keys are per provider + id, so equal numeric ids on two platforms count
     * as two entries. Rows added by a later page are not checked retroactively.
     */
    fun setAllSelected(checked: Boolean) {
        val tracks = _state.value.tracks
        if (tracks.isEmpty()) return
        val keys = tracks.mapTo(LinkedHashSet()) { MusicJobResults.trackKey(it) }
        val selected = _state.value.selectedKeys
        val updated = if (checked) selected + keys else selected - keys
        if (updated == selected) return
        savedState?.set(SAVED_SELECTED, ArrayList(updated))
        _state.value = _state.value.copy(selectedKeys = updated)
    }

    /**
     * Merges job-produced local paths onto the visible rows, by
     * [MusicJobResults.trackKey] (`provider` + `id`). Called with the
     * [MusicJobViewModel] state whenever the screen is (re)composed and after
     * every path update, so a restored list is directly exportable.
     */
    fun applyPaths(newPaths: Map<String, String>) {
        if (newPaths.isEmpty()) return
        paths = paths + newPaths
        val current = _state.value.tracks
        val updated = MusicJobResults.applyPaths(current, paths)
        if (updated !== current) _state.value = _state.value.copy(tracks = updated)
    }

    /**
     * Drops the current result pages: new searches and platform switches start
     * over. The search ids go too — they belong to the discarded pages — and
     * the bump makes answers of requests started before this point stale.
     */
    private fun resetResults(): Int {
        if (_state.value.selectedKeys.isNotEmpty()) {
            savedState?.set(SAVED_SELECTED, ArrayList<String>())
        }
        val next = _state.value.copy(
            tracks = emptyList(),
            searchIdsByTrack = emptyMap(),
            totalHits = 0,
            endReached = false,
            supportsOffset = null,
            loadingMore = false,
            loadMoreFailed = false,
            selectedKeys = emptySet(),
            generation = _state.value.generation + 1,
        )
        _state.value = next
        return next.generation
    }

    companion object {
        /**
         * Saved-state keys. The selection is stored as an `ArrayList<String>`
         * of [MusicJobResults.trackKey] values (`provider\u0000id`) because
         * that is what `Bundle` can carry across process death; bare ids
         * written by older builds are discarded on restore.
         */
        internal const val SAVED_KEYWORD = "music.keyword"
        internal const val SAVED_PLATFORM = "music.platform"
        internal const val SAVED_SELECTED = "music.selected"

        /**
         * Activity-scoped factory: restores the persisted platform choice and
         * the saved instance state (keyword, platform, selection).
         */
        fun factory(context: Context): ViewModelProvider.Factory {
            val appContext = context.applicationContext
            return viewModelFactory {
                initializer {
                    val store = SelectionStore(appContext)
                    MusicSearchViewModel(
                        initialPlatform = store.selectedPlatform().ifBlank {
                            MUSIC_ALL_PLATFORMS
                        },
                        persistPlatform = store::saveSelectedPlatform,
                        savedState = createSavedStateHandle(),
                    )
                }
            }
        }
    }
}

/** A blank platform means "search every platform", matching the server default. */
private fun normalizePlatform(key: String): String =
    key.trim().ifBlank { MUSIC_ALL_PLATFORMS }
