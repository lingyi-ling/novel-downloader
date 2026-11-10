package com.lingyi.noveldl.ui.music

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.lingyi.noveldl.data.Api
import com.lingyi.noveldl.data.ApiErrorCode
import com.lingyi.noveldl.data.ApiFailure
import com.lingyi.noveldl.data.ApiService
import com.lingyi.noveldl.data.MusicExportRequest
import com.lingyi.noveldl.data.MusicTrack
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.receiveAsFlow
import kotlinx.coroutines.launch
import retrofit2.HttpException

/** Result of one "export playlist" attempt, mapped to strings by the screen. */
sealed interface MusicExportOutcome {
    /** The server wrote the playlist and reported its path. */
    data class Success(
        val path: String,
        val written: Int,
        val skipped: Int,
    ) : MusicExportOutcome

    /** There are no search results to export. */
    data object NoResults : MusicExportOutcome

    /** Results exist, but none of them was downloaded (no local path). */
    data object NoLocalPaths : MusicExportOutcome

    /**
     * The engine/API is not running or lacks music support (HTTP 503). The
     * failure is carried as-is so the screen localizes its
     * [com.lingyi.noveldl.data.ApiErrorCode].
     */
    data class Unavailable(val error: Throwable) : MusicExportOutcome

    /** The request reached the engine but the playlist could not be written. */
    data class Failed(val error: Throwable) : MusicExportOutcome
}

/**
 * Observable state of the playlist export, owned by the Activity-scoped
 * [MusicExportViewModel].
 *
 * Previously `exporting` and `lastExportPath` were local `remember`s inside the
 * music composable: leaving the tab cancelled the request (its scope was the
 * composition) and dropped the path the user still needed to find the file.
 */
data class MusicExportUiState(
    /** A request is in flight; it keeps running while another tab is shown. */
    val running: Boolean = false,
    /** Path of the most recent successful export, kept until the Activity ends. */
    val lastPath: String? = null,
    val lastWritten: Int = 0,
    val lastSkipped: Int = 0,
    /**
     * Failure of the most recent attempt ([MusicExportOutcome.Unavailable] or
     * [MusicExportOutcome.Failed]), or null. The screen localizes it and offers
     * a retry; a later attempt clears it.
     */
    val failure: MusicExportOutcome? = null,
)

/**
 * Activity-scoped owner of the playlist export.
 *
 * [service] is injectable so unit tests can point it at a MockWebServer-backed
 * Retrofit service; the production default resolves the configured singleton.
 *
 * [start] runs the request in [viewModelScope], which the music screen does not
 * own, so a tab switch neither cancels the write nor loses its result: the
 * outcome is kept in [state] and buffered in [incoming] for the snackbar.
 */
class MusicExportViewModel @JvmOverloads constructor(
    private val service: () -> ApiService = { Api.service() },
) : ViewModel() {

    private data class Request(val platform: String, val tracks: List<MusicTrack>)

    private val _state = MutableStateFlow(MusicExportUiState())
    val state: StateFlow<MusicExportUiState> = _state.asStateFlow()

    private val events = Channel<MusicExportOutcome>(Channel.BUFFERED)

    /**
     * One-shot outcomes for the snackbar. Buffered, so a success or failure
     * that happens while another tab is visible is still reported when the
     * music screen comes back.
     */
    val incoming: Flow<MusicExportOutcome> = events.receiveAsFlow()

    private var lastRequest: Request? = null

    /**
     * Exports [tracks] (the visible search results) as an M3U8 playlist.
     * Returns false when an export is already running (no duplicate request).
     *
     * The list is snapshotted: the export must describe what the user saw, not
     * whatever a concurrent search replaces it with mid-request.
     */
    fun start(platform: String, tracks: List<MusicTrack>): Boolean {
        if (_state.value.running) return false
        val request = Request(platform, tracks.toList())
        lastRequest = request
        _state.value = _state.value.copy(running = true, failure = null)
        viewModelScope.launch {
            val outcome = export(request.platform, request.tracks)
            _state.value = when (outcome) {
                is MusicExportOutcome.Success -> _state.value.copy(
                    running = false,
                    lastPath = outcome.path,
                    lastWritten = outcome.written,
                    lastSkipped = outcome.skipped,
                    failure = null,
                )
                is MusicExportOutcome.Unavailable, is MusicExportOutcome.Failed ->
                    _state.value.copy(running = false, failure = outcome)
                MusicExportOutcome.NoResults, MusicExportOutcome.NoLocalPaths ->
                    _state.value.copy(running = false)
            }
            events.trySend(outcome)
        }
        return true
    }

    /** Repeats the last export after a failure; false when nothing to repeat. */
    fun retry(): Boolean {
        val request = lastRequest ?: return false
        return start(request.platform, request.tracks)
    }

    /**
     * Performs one export and reports its outcome; no state is touched, which
     * keeps it directly callable from tests.
     *
     * Tracks without a local path are filtered out before the request; when
     * that leaves nothing, no request is sent and [MusicExportOutcome.NoLocalPaths]
     * is returned. The playlist defaults to the first track's directory.
     */
    suspend fun export(platform: String, tracks: List<MusicTrack>): MusicExportOutcome {
        if (tracks.isEmpty()) return MusicExportOutcome.NoResults
        val plan = MusicExport.plan(tracks) ?: return MusicExportOutcome.NoLocalPaths

        val result = Api.call {
            service().musicExport(
                MusicExportRequest(
                    platform = platform,
                    tracks = plan.tracks,
                    out_path = plan.outPath,
                ),
            )
        }
        return result.fold(
            onSuccess = { response ->
                MusicExportOutcome.Success(
                    path = response.path.ifBlank { plan.outPath.orEmpty() },
                    written = response.writtenCount,
                    // The client already dropped pathless tracks, so the
                    // server's count is normally 0; report the larger one.
                    skipped = maxOf(plan.skippedTracks, response.skipped),
                )
            },
            onFailure = { error ->
                if (isUnavailable(error)) {
                    MusicExportOutcome.Unavailable(error)
                } else {
                    MusicExportOutcome.Failed(error)
                }
            },
        )
    }

    /**
     * The API not being configured and an engine without music extras (503)
     * both mean "export is unavailable", as opposed to a write failure.
     */
    private fun isUnavailable(error: Throwable): Boolean {
        if ((error as? ApiFailure)?.code == ApiErrorCode.NOT_CONFIGURED) return true
        val http = error.cause as? HttpException ?: error as? HttpException
        return http?.code() == 503
    }
}
