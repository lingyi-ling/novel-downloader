package com.lingyi.noveldl.ui.music

import android.content.Context
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.lingyi.noveldl.data.Api
import com.lingyi.noveldl.data.ApiService
import com.lingyi.noveldl.data.MusicTrack
import com.lingyi.noveldl.ui.MUSIC_JOB_POLL_INTERVAL_MS
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.receiveAsFlow
import kotlinx.coroutines.launch

/**
 * Observable state of the music download job.
 *
 * The job outlives the music composable: it is owned by an Activity-scoped
 * ViewModel, so switching tabs no longer cancels the watch or loses the
 * progress line, and a second "download selected" tap cannot submit the same
 * job twice.
 */
data class MusicJobUiState(
    val running: Boolean = false,
    val done: Int = 0,
    val total: Int = 0,
    /**
     * Local paths written so far, keyed by `provider\u0000id` (see
     * [MusicJobResults.trackKey]), so they can be written back onto whichever
     * result list is visible.
     */
    val paths: Map<String, String> = emptyMap(),
    /**
     * Client-side failure of the last run (job creation or polling). The
     * screen localizes the [com.lingyi.noveldl.data.ApiErrorCode] it carries.
     */
    val failure: Throwable? = null,
)

/** One-shot notifications of a finished/failed run, mapped to strings by the UI. */
sealed interface MusicJobEvent {

    /** At least one batch produced results; carries the aggregated counts. */
    data class Finished(
        val counts: MusicJobResults.Counts,
        val firstFailure: String?,
        val saveFailures: Int,
        val firstSaveFailure: String?,
        val serverPath: String?,
    ) : MusicJobEvent

    /** The engine reported `failed` for a job; [reason] is its own text. */
    data class Failed(val reason: String) : MusicJobEvent
}

/**
 * Owns the music download job so it survives navigation.
 *
 * [service], [saver] and [pollIntervalMs] are injectable for JVM tests; the
 * production factory binds the configured API and the DownloadManager saver.
 * Events are buffered in a channel: a job that finishes while the user is on
 * another tab still reports its result when they come back.
 */
class MusicJobViewModel @JvmOverloads constructor(
    private val service: () -> ApiService = { Api.service() },
    private val saver: MusicFileSaver = MusicFileSaver.None,
    private val pollIntervalMs: Long = MUSIC_JOB_POLL_INTERVAL_MS,
) : ViewModel() {

    private data class Request(
        val platform: String,
        val tracks: List<MusicTrack>,
        val idsByTrack: Map<String, String>,
    )

    private val _state = MutableStateFlow(MusicJobUiState())
    val state: StateFlow<MusicJobUiState> = _state.asStateFlow()

    private val events = Channel<MusicJobEvent>(Channel.BUFFERED)
    val incoming: Flow<MusicJobEvent> = events.receiveAsFlow()

    /** Foreground gate: the watch parks while the app is in the background. */
    private val foreground = MutableStateFlow(true)

    private var lastRequest: Request? = null

    /** Called by the host with the Activity lifecycle state. */
    fun setForeground(active: Boolean) {
        foreground.value = active
    }

    /**
     * Submits [chosen] as one job per `search_id` page. Returns false when
     * nothing was selected or a job is already running (no duplicate jobs).
     */
    fun start(
        platform: String,
        chosen: List<MusicTrack>,
        idsByTrack: Map<String, String>,
    ): Boolean {
        if (chosen.isEmpty() || _state.value.running) return false
        val request = Request(platform, chosen.toList(), idsByTrack.toMap())
        lastRequest = request
        launch(request)
        return true
    }

    /** Repeats the last "download selected" request after a failure. */
    fun retry(): Boolean {
        val request = lastRequest ?: return false
        if (_state.value.running) return false
        launch(request)
        return true
    }

    private fun launch(request: Request) {
        _state.value = _state.value.copy(
            running = true,
            done = 0,
            total = request.tracks.size,
            failure = null,
        )
        viewModelScope.launch {
            val runner = MusicJobRunner(
                service = service,
                saver = saver,
                pollIntervalMs = pollIntervalMs,
                awaitForeground = { foreground.first { it } },
            )
            val run = runner.run(
                platform = request.platform,
                chosen = request.tracks,
                idsByTrack = request.idsByTrack,
            ) { done, total ->
                _state.value = _state.value.copy(running = true, done = done, total = total)
            }
            _state.value = _state.value.copy(
                running = false,
                done = run.total,
                total = run.total,
                paths = _state.value.paths + run.pathsByTrack,
                failure = run.errors.firstOrNull(),
            )
            run.jobFailure?.let { events.trySend(MusicJobEvent.Failed(it)) }
            if (run.counts.ok + run.counts.skipped + run.counts.failed > 0) {
                events.trySend(
                    MusicJobEvent.Finished(
                        counts = run.counts,
                        firstFailure = run.firstFailure,
                        saveFailures = run.saveFailures,
                        firstSaveFailure = run.firstSaveFailure,
                        serverPath = run.serverPath,
                    ),
                )
            }
        }
    }

    companion object {
        /** Activity-scoped factory used by the app shell. */
        fun factory(context: Context): ViewModelProvider.Factory {
            val appContext = context.applicationContext
            return viewModelFactory {
                initializer {
                    MusicJobViewModel(saver = DownloadManagerMusicSaver(appContext))
                }
            }
        }
    }
}
