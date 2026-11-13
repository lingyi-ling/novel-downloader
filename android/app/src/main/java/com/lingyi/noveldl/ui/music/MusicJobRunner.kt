package com.lingyi.noveldl.ui.music

import com.lingyi.noveldl.data.Api
import com.lingyi.noveldl.data.ApiService
import com.lingyi.noveldl.data.MusicJobRequest
import com.lingyi.noveldl.data.MusicJobResult
import com.lingyi.noveldl.data.MusicTrack
import com.lingyi.noveldl.ui.MUSIC_JOB_POLL_INTERVAL_MS
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.withContext

/**
 * Everything one "download selected" run produced, across every batch.
 *
 * [pathsByTrack] is keyed by [MusicJobResults.trackKey] (provider + id), so
 * merging batches never mixes two platforms that reuse a numeric id, and the
 * screen can write the paths back onto whichever result list is visible.
 * [errors] holds client-side failures (job creation / polling) while
 * [jobFailure] is the engine's own reason for a job that ended `failed`
 * (typically an expired `search_id`).
 */
data class MusicJobRun(
    val total: Int,
    val counts: MusicJobResults.Counts,
    val firstFailure: String?,
    val pathsByTrack: Map<String, String>,
    /**
     * Keys of tracks whose result was an explicit `fail`. The persisted path
     * cache removes them, so a stale path from a previous attempt is not
     * exported after the file was replaced/failed.
     */
    val failedTrackKeys: Set<String> = emptySet(),
    val saveFailures: Int,
    val firstSaveFailure: String?,
    /** First successful local path when nothing was downloadable (remote mode). */
    val serverPath: String?,
    val errors: List<Throwable>,
    val jobFailure: String?,
    /**
     * Verified `<stem>.lrc` paths, keyed like [pathsByTrack]. The screen uses
     * this map for the per-row lyric button; it covers `ok` and `skip`
     * results, because the engine keeps the lyric when it skips a track.
     */
    val lyricPathsByTrack: Map<String, String> = emptyMap(),
    /** Results that landed with a lyric file, for the batch summary line. */
    val lyricsSaved: Int = 0,
)

/**
 * Submits a cross-page selection as one job per `search_id` and watches them
 * sequentially.
 *
 * Sequential (not parallel) submission is deliberate: the server keeps one
 * cache entry per search request and each job writes into the same output
 * directory, so bounded load is the safer default on the embedded engine; it
 * also keeps the progress line monotonic. A batch that cannot be created or
 * polled is recorded in [MusicJobRun.errors] and the remaining batches still
 * run — one expired page must not discard the downloads of the others.
 *
 * [awaitForeground] parks the watch while the app is backgrounded; the loop
 * itself ends only by cancellation, which the callers own.
 */
class MusicJobRunner(
    private val service: () -> ApiService,
    private val saver: MusicFileSaver = MusicFileSaver.None,
    /**
     * Resolves the `.lrc` the engine wrote beside a finished audio file. The
     * default finds nothing (unit tests, transports without local files); the
     * app uses [PrivateMusicLyricLookup].
     */
    private val lyrics: MusicLyricLookup = MusicLyricLookup.None,
    private val pollIntervalMs: Long = MUSIC_JOB_POLL_INTERVAL_MS,
    private val awaitForeground: suspend () -> Unit = {},
) {

    private sealed interface Watch {
        data class Done(val results: List<MusicJobResult>) : Watch
        data class Failed(val reason: String) : Watch
        data class Broken(val error: Throwable) : Watch
    }

    suspend fun run(
        platform: String,
        chosen: List<MusicTrack>,
        idsByTrack: Map<String, String>,
        onProgress: (done: Int, total: Int) -> Unit = { _, _ -> },
    ): MusicJobRun {
        val total = chosen.size
        val results = mutableListOf<MusicJobResult>()
        val paths = LinkedHashMap<String, String>()
        val lyricPaths = LinkedHashMap<String, String>()
        val failedKeys = LinkedHashSet<String>()
        val errors = mutableListOf<Throwable>()
        var jobFailure: String? = null
        var saveFailures = 0
        var firstSaveFailure: String? = null
        var completed = 0
        onProgress(completed, total)

        MusicJobBatching.batches(chosen, idsByTrack).forEach { batch ->
            val created = Api.call {
                service().createMusicJob(
                    MusicJobRequest(
                        platform = platform,
                        tracks = batch.tracks,
                        search_id = batch.searchId,
                    ),
                )
            }

            val createdFailure = created.exceptionOrNull()
            if (createdFailure != null) {
                errors += createdFailure
            } else {
                val jobId = created.getOrThrow().job_id
                when (
                    val watch = watch(
                        jobId = jobId,
                        batchSize = batch.tracks.size,
                        completedBefore = completed,
                        total = total,
                        onProgress = onProgress,
                    )
                ) {
                    is Watch.Done -> {
                        // The engine already wrote `<stem>.lrc` beside the
                        // audio; only verify it exists inside the app sandbox.
                        // Probes touch the filesystem, so keep them off the
                        // caller's thread even though a batch is small.
                        val enriched = withContext(Dispatchers.IO) {
                            watch.results.map { result ->
                                val lyric = if (
                                    result.status == "ok" || result.status == "skip"
                                ) {
                                    lyrics.find(result.path)
                                } else {
                                    null
                                }
                                // Always replace, never trust a value that
                                // came over the wire: only a path this client
                                // verified may drive the lyric button.
                                result.copy(lyricPath = lyric)
                            }
                        }
                        results += enriched
                        paths.putAll(MusicJobResults.pathsByTrack(batch.tracks, enriched))
                        lyricPaths.putAll(MusicJobResults.lyricsByTrack(batch.tracks, enriched))
                        failedKeys += MusicJobResults.failedTrackKeys(batch.tracks, enriched)
                        MusicJobResults.downloadable(enriched).forEach { result ->
                            saver.save(result).onFailure { failure ->
                                saveFailures++
                                if (firstSaveFailure == null) {
                                    firstSaveFailure = failure.message
                                }
                            }
                        }
                    }
                    is Watch.Failed -> jobFailure = jobFailure ?: watch.reason
                    is Watch.Broken -> errors += watch.error
                }
            }
            completed += batch.tracks.size
            onProgress(completed, total)
        }

        val downloadable = MusicJobResults.downloadable(results)
        return MusicJobRun(
            total = total,
            counts = MusicJobResults.counts(results),
            firstFailure = MusicJobResults.firstFailureReason(results),
            pathsByTrack = paths,
            failedTrackKeys = failedKeys,
            saveFailures = saveFailures,
            firstSaveFailure = firstSaveFailure,
            // No loopback URLs means the job ran on a remote server: the files
            // stay there and nothing can be copied into the phone.
            serverPath = if (downloadable.isEmpty()) {
                results.firstOrNull { it.status == "ok" && it.path.isNotBlank() }?.path
            } else {
                null
            },
            errors = errors,
            jobFailure = jobFailure,
            lyricPathsByTrack = lyricPaths,
            lyricsSaved = MusicLyrics.savedCount(results),
        )
    }

    /** Polls [jobId] until it finishes, reporting this batch's progress. */
    private suspend fun watch(
        jobId: String,
        batchSize: Int,
        completedBefore: Int,
        total: Int,
        onProgress: (done: Int, total: Int) -> Unit,
    ): Watch {
        while (true) {
            awaitForeground()
            val polled = Api.call { service().musicJob(jobId) }
            polled.exceptionOrNull()?.let { return Watch.Broken(it) }
            val job = polled.getOrThrow()
            when (job.status) {
                "done" -> return Watch.Done(job.results)
                "failed" -> return Watch.Failed(job.error ?: job.status)
            }
            // The batch's own count is the most this job can add; progress
            // never jumps past the chosen total.
            val inBatch = job.progress.done.coerceIn(0, batchSize)
            onProgress(completedBefore + inBatch, total)
            delay(pollIntervalMs)
        }
    }
}
