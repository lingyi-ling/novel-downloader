package com.lingyi.noveldl.ui.music

import androidx.lifecycle.ViewModel
import com.lingyi.noveldl.data.Api
import com.lingyi.noveldl.data.ApiService
import com.lingyi.noveldl.data.MusicExportRequest
import com.lingyi.noveldl.data.MusicTrack
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

    /** The engine/API is not running or lacks music support (HTTP 503). */
    data class Unavailable(val reason: String) : MusicExportOutcome

    /** The request reached the engine but the playlist could not be written. */
    data class Failed(val reason: String) : MusicExportOutcome
}

/**
 * ViewModel for the music screen's playlist export.
 *
 * [service] is injectable so unit tests can point it at a MockWebServer-backed
 * Retrofit service; the production default resolves the configured singleton.
 */
class MusicExportViewModel @JvmOverloads constructor(
    private val service: () -> ApiService = { Api.service() },
) : ViewModel() {

    /**
     * Exports [tracks] (the current search results) to an M3U8 playlist.
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
                val reason = error.message ?: error.javaClass.simpleName
                if (isUnavailable(error)) {
                    MusicExportOutcome.Unavailable(reason)
                } else {
                    MusicExportOutcome.Failed(reason)
                }
            },
        )
    }

    /**
     * The API not being configured and an engine without music extras (503)
     * both mean "export is unavailable", as opposed to a write failure.
     */
    private fun isUnavailable(error: Throwable): Boolean {
        if (error.message == Api.NOT_CONFIGURED_MESSAGE) return true
        val http = error.cause as? HttpException ?: error as? HttpException
        return http?.code() == 503
    }
}
