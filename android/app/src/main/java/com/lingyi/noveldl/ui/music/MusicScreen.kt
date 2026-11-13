package com.lingyi.noveldl.ui.music

import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.selection.toggleable
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.Cancel
import androidx.compose.material.icons.outlined.CheckCircle
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.Checkbox
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.PlainTooltip
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TooltipBox
import androidx.compose.material3.TooltipDefaults
import androidx.compose.material3.rememberTooltipState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import com.lingyi.noveldl.R
import com.lingyi.noveldl.data.Api
import com.lingyi.noveldl.data.ApiErrorCode
import com.lingyi.noveldl.data.EmbeddedEngine
import com.lingyi.noveldl.data.MusicHealthItem
import com.lingyi.noveldl.data.MUSIC_ALL_PLATFORMS
import com.lingyi.noveldl.ui.apiErrorMessage
import com.lingyi.noveldl.ui.progressFraction
import kotlinx.coroutines.delay

/** Per-platform search budget sent to `GET /api/v1/music/health` (seconds). */
private const val MUSIC_HEALTH_TIMEOUT_SECONDS = 5

/** Debounce window before a platform switch actually fires the health probe. */
private const val MUSIC_HEALTH_DEBOUNCE_MS = 1_000L

/** Success tint for the health badge; Material3 has no semantic "ok" color. */
private val HealthOkLight: Color = Color(0xFF2E7D32)
private val HealthOkDark: Color = Color(0xFF81C784)

/**
 * Music search + download screen.
 *
 * All three owners are Activity-scoped ViewModels passed in by the app shell:
 * [searchModel] keeps the keyword, platform, loaded pages, `search_id` map and
 * selection alive across tab switches and recomposition, [jobModel] keeps a
 * running download alive, and [exportModel] keeps a playlist export running and
 * remembers where the last one was written. Nothing is remembered locally
 * except transient UI state (menu, health badge).
 */
@OptIn(ExperimentalLayoutApi::class)
@Composable
fun MusicScreen(
    jobModel: MusicJobViewModel,
    searchModel: MusicSearchViewModel,
    exportModel: MusicExportViewModel,
    onMessage: (String) -> Unit,
) {
    val context = LocalContext.current
    val jobState by jobModel.state.collectAsState()
    val search by searchModel.state.collectAsState()
    val exportState by exportModel.state.collectAsState()
    var menuExpanded by remember { mutableStateOf(false) }
    // Health probe per platform, keyed by platform id; the selected platform's
    // entry drives the badge next to the picker.
    var healthByPlatform by remember { mutableStateOf<Map<String, MusicHealthItem>>(emptyMap()) }
    var healthChecking by remember { mutableStateOf(false) }
    val allPlatformLabel = stringResource(R.string.music_platform_all)
    val platformLabels = remember(search.platforms) {
        search.platforms.associate { it.key to it.label }
    }

    fun downloadSelected() {
        // Checked rows are keyed by provider + id: two platforms reusing the
        // same numeric song id stay independent.
        val chosen = MusicJobResults.selectedTracks(search.tracks, search.selectedKeys)
        if (chosen.isEmpty()) {
            onMessage(context.getString(R.string.music_no_selection))
            return
        }
        // The ViewModel guards against a second tap while a job runs and
        // splits the selection per page `search_id`: a cross-page selection
        // becomes several jobs instead of one job whose later tracks fail
        // with "search result expired". It also owns the job, so the watch
        // is not cancelled when this screen leaves composition.
        if (!jobModel.start(search.platform, chosen, search.searchIdsByTrack)) return
    }

    /**
     * Starts the playlist export of the current search results that carry a
     * local path. Every edge case reports a snackbar message instead of failing
     * silently: no results, no downloaded tracks, engine not started or
     * without music support, and write errors.
     *
     * The request itself lives in [MusicExportViewModel] (Activity scope), so
     * switching tabs does not cancel it; its outcome is buffered and reported
     * when the user comes back, and the path stays visible in [MusicExportUiState].
     */
    fun exportPlaylist() {
        if (exportState.running) return
        if (!Api.isConfigured()) {
            onMessage(
                context.getString(
                    R.string.music_export_unavailable,
                    apiErrorMessage(context, ApiErrorCode.NOT_CONFIGURED),
                ),
            )
            return
        }
        if (Api.baseUrl == EmbeddedEngine.BASE_URL) {
            val engineError = EmbeddedEngine.lastError
            if (engineError != null) {
                onMessage(context.getString(R.string.music_export_engine_failed, engineError))
                return
            }
            // `isStarted` only means the bootstrap was scheduled; until it has
            // finished the loopback port is not listening yet.
            if (!EmbeddedEngine.isReady) {
                onMessage(context.getString(R.string.music_export_engine_unavailable))
                return
            }
        }
        // The ViewModel snapshots the list: the export describes what the user
        // sees now, not whatever a concurrent search replaces it with.
        exportModel.start(search.platform, search.tracks)
    }

    // Export outcomes are buffered by the Activity-scoped ViewModel, so one
    // that finishes while another tab is shown is still reported on return.
    LaunchedEffect(exportModel) {
        exportModel.incoming.collect { outcome ->
            when (outcome) {
                MusicExportOutcome.NoResults ->
                    onMessage(context.getString(R.string.music_export_no_results))
                MusicExportOutcome.NoLocalPaths ->
                    onMessage(context.getString(R.string.music_export_no_local_paths))
                is MusicExportOutcome.Unavailable ->
                    onMessage(
                        context.getString(
                            R.string.music_export_unavailable,
                            apiErrorMessage(context, outcome.error),
                        ),
                    )
                is MusicExportOutcome.Failed ->
                    onMessage(
                        context.getString(
                            R.string.music_export_failed,
                            apiErrorMessage(context, outcome.error),
                        ),
                    )
                is MusicExportOutcome.Success ->
                    onMessage(
                        context.getString(
                            R.string.music_export_done,
                            outcome.path,
                            outcome.written,
                            outcome.skipped,
                        ),
                    )
            }
        }
    }

    // The job is owned by the Activity-scoped ViewModel and its events are
    // buffered, so a run that finishes while the user is on another tab still
    // reports here when they come back.
    LaunchedEffect(jobModel) {
        jobModel.incoming.collect { event ->
            when (event) {
                is MusicJobEvent.Finished -> {
                    // The lyric count belongs in the batch summary: it is the
                    // only place the user learns how many tracks got a `.lrc`
                    // without opening every song. The zero case keeps the old
                    // wording, so runs without lyrics read the same as before.
                    onMessage(
                        if (event.lyricsSaved > 0) {
                            context.getString(
                                R.string.music_job_done_lyrics,
                                event.counts.ok,
                                event.counts.skipped,
                                event.counts.failed,
                                event.lyricsSaved,
                            )
                        } else {
                            context.getString(
                                R.string.music_job_done,
                                event.counts.ok,
                                event.counts.skipped,
                                event.counts.failed,
                            )
                        },
                    )
                    // Counts alone do not explain a failure; surface the
                    // engine's own reason (e.g. an expired search_id asks the
                    // user to search again).
                    if (event.counts.failed > 0) {
                        event.firstFailure?.let { reason ->
                            onMessage(
                                context.getString(R.string.music_job_fail_reason, reason),
                            )
                        }
                    }
                    when {
                        event.saveFailures > 0 -> onMessage(
                            context.getString(
                                R.string.music_job_save_failed,
                                event.saveFailures,
                                event.firstSaveFailure.orEmpty(),
                            ),
                        )
                        // A non-null path with no downloadable result means the
                        // job ran on a remote server: the files stay there.
                        event.counts.ok > 0 && event.serverPath != null -> onMessage(
                            context.getString(R.string.music_job_server_files, event.serverPath),
                        )
                    }
                }
                is MusicJobEvent.Failed -> onMessage(
                    context.getString(R.string.music_job_failed, event.reason),
                )
            }
        }
    }

    // Write finished downloads' local paths onto the visible rows (export
    // needs them). Keyed by provider + id, so a later search of the same song
    // is populated too instead of being re-downloaded. The ViewModel merges,
    // which is why a list restored after a tab switch is exportable at once.
    LaunchedEffect(jobState.paths) {
        searchModel.applyPaths(jobState.paths)
    }

    // One catalog fetch per ViewModel, i.e. only while the screen is visible.
    LaunchedEffect(searchModel) {
        searchModel.ensurePlatforms()
    }

    // Checks the selected platform on entry and again on every switch. The
    // 1s delay debounces rapid switching: restarting this effect cancels the
    // pending probe before the request is sent. The badge is transient UI
    // state, intentionally not part of the saved search state.
    LaunchedEffect(search.platform) {
        val platform = search.platform
        // The health endpoint only knows concrete platform keys; probing
        // "all" would be rejected (REST) or fan out to every platform.
        if (platform.isEmpty() || platform == MUSIC_ALL_PLATFORMS) {
            healthChecking = false
            return@LaunchedEffect
        }
        healthChecking = true
        delay(MUSIC_HEALTH_DEBOUNCE_MS)
        val outcome = Api.call {
            Api.service().musicHealth(platform, MUSIC_HEALTH_TIMEOUT_SECONDS)
        }
        // The platform may have changed while the probe was in flight.
        if (searchModel.state.value.platform != platform) return@LaunchedEffect
        outcome
            .onSuccess { probes ->
                probes.firstOrNull { it.platform == platform }?.let {
                    healthByPlatform = healthByPlatform + (platform to it)
                }
            }
            .onFailure {
                healthByPlatform = healthByPlatform + (
                    platform to MusicHealthItem(
                        platform = platform,
                        ok = false,
                        error = apiErrorMessage(context, it),
                    )
                    )
            }
        healthChecking = false
    }

    // Header and result rows share one LazyColumn. With a fixed header in a
    // Column and a weighted list, a landscape phone (or a large font scale)
    // could spend the whole height on the controls and leave the results
    // clipped and unreachable.
    LazyColumn(
        modifier = Modifier.fillMaxSize().padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        item(key = "music-header") {
            Column {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Box {
                Button(onClick = { menuExpanded = true }) {
                    val label = when (search.platform) {
                        MUSIC_ALL_PLATFORMS -> allPlatformLabel
                        else -> search.platforms.firstOrNull { it.key == search.platform }?.label
                            ?: stringResource(R.string.music_platform)
                    }
                    Text(label)
                }
                DropdownMenu(expanded = menuExpanded, onDismissRequest = { menuExpanded = false }) {
                    DropdownMenuItem(
                        text = { Text(allPlatformLabel) },
                        onClick = {
                            menuExpanded = false
                            searchModel.selectPlatform(MUSIC_ALL_PLATFORMS)
                        },
                    )
                    search.platforms.forEach { platform ->
                        DropdownMenuItem(
                            text = { Text(platform.label) },
                            onClick = {
                                menuExpanded = false
                                searchModel.selectPlatform(platform.key)
                            },
                        )
                    }
                }
            }
            Spacer(Modifier.width(12.dp))
            PlatformHealthBadge(
                checking = healthChecking,
                result = healthByPlatform[search.platform],
            )
        }
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(
            value = search.keyword,
            onValueChange = searchModel::onKeywordChange,
            label = { Text(stringResource(R.string.music_keyword_hint)) },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(8.dp))
        FlowRow(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Button(onClick = searchModel::search, enabled = !search.searching) {
                Text(stringResource(R.string.music_search_button))
            }
            Button(onClick = { downloadSelected() }, enabled = !jobState.running) {
                Text(stringResource(R.string.music_download_selected))
            }
        }
        Spacer(Modifier.height(8.dp))
        Button(
            onClick = { exportPlaylist() },
            enabled = !exportState.running,
            modifier = Modifier.fillMaxWidth(),
        ) {
            Text(
                stringResource(
                    if (exportState.running) {
                        R.string.music_exporting
                    } else {
                        R.string.music_export_playlist
                    },
                ),
            )
        }
        // The last export path lives in the Activity-scoped ViewModel, so it is
        // still shown after the snackbar faded or the tab was switched away.
        exportState.lastPath?.let { path ->
            Spacer(Modifier.height(4.dp))
            Text(
                stringResource(R.string.music_export_last_path, path),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        // Export failures stay visible (and retryable) instead of only flashing
        // by in a snackbar that is gone after a tab switch.
        exportState.failure?.let { failure ->
            val message = when (failure) {
                is MusicExportOutcome.Unavailable -> stringResource(
                    R.string.music_export_unavailable,
                    apiErrorMessage(context, failure.error),
                )
                is MusicExportOutcome.Failed -> stringResource(
                    R.string.music_export_failed,
                    apiErrorMessage(context, failure.error),
                )
                else -> null
            }
            if (message != null) {
                Spacer(Modifier.height(8.dp))
                Text(
                    message,
                    color = MaterialTheme.colorScheme.error,
                    style = MaterialTheme.typography.bodySmall,
                )
                TextButton(onClick = { exportModel.retry() }) {
                    Text(stringResource(R.string.action_retry))
                }
            }
        }
        if (jobState.running) {
            Spacer(Modifier.height(8.dp))
            Text(
                stringResource(
                    R.string.music_job_progress,
                    jobState.done,
                    jobState.total,
                ),
                style = MaterialTheme.typography.bodySmall,
            )
            Spacer(Modifier.height(4.dp))
            LinearProgressIndicator(
                progress = { progressFraction(jobState.done, jobState.total) },
                modifier = Modifier.fillMaxWidth(),
            )
        }
        // Job failures are shown next to the search errors: the job may end
        // while the user is on another tab, but the state outlives the screen.
        jobState.failure?.let { failure ->
            Spacer(Modifier.height(8.dp))
            Text(
                stringResource(R.string.state_error, apiErrorMessage(context, failure)),
                color = MaterialTheme.colorScheme.error,
                style = MaterialTheme.typography.bodySmall,
            )
            TextButton(onClick = { jobModel.retry() }) {
                Text(stringResource(R.string.action_retry))
            }
        }
        search.error?.let { error ->
            Spacer(Modifier.height(8.dp))
            Text(
                stringResource(R.string.state_error, apiErrorMessage(context, error)),
                color = MaterialTheme.colorScheme.error,
                style = MaterialTheme.typography.bodySmall,
            )
            // Hidden when repeating the request would be a silent no-op
            // (search / load-more without a keyword).
            val retryTarget = MusicRetry.target(search.errorSource, search.keyword)
            if (retryTarget != null) {
                TextButton(onClick = { searchModel.retry(retryTarget) }) {
                    Text(stringResource(R.string.action_retry))
                }
            }
        }
        if (search.tracks.isNotEmpty()) {
            Spacer(Modifier.height(8.dp))
            // "Select all" works on the currently loaded rows only; a later
            // page starts unchecked. Identity is provider + id, so duplicate
            // numeric ids across platforms are distinct entries.
            val allSelected = search.tracks.all {
                MusicJobResults.trackKey(it) in search.selectedKeys
            }
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .heightIn(min = 48.dp)
                    .toggleable(
                        value = allSelected,
                        role = Role.Checkbox,
                        onValueChange = searchModel::setAllSelected,
                    )
                    .padding(vertical = 4.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Checkbox(
                    checked = allSelected,
                    onCheckedChange = null,
                )
                Spacer(Modifier.width(8.dp))
                Text(
                    stringResource(R.string.music_select_all),
                    style = MaterialTheme.typography.bodySmall,
                )
                Spacer(Modifier.weight(1f))
                Text(
                    if (search.totalHits > 0) {
                        stringResource(
                            R.string.music_search_progress,
                            search.tracks.size,
                            search.totalHits,
                        )
                    } else {
                        stringResource(R.string.music_tracks, search.tracks.size)
                    },
                    style = MaterialTheme.typography.bodySmall,
                )
            }
        }
            }
        }
        items(search.tracks) { track ->
            val trackKey = MusicJobResults.trackKey(track)
            val checked = trackKey in search.selectedKeys
            // Verified by [MusicJobRunner]: absent until a finished download
            // really wrote `<stem>.lrc` next to the audio inside the app's
            // private music directory.
            val lyricPath = jobState.lyricPaths[trackKey]
            Card(modifier = Modifier.fillMaxWidth()) {
                Row(
                    // The whole card selects the track: a 24 dp checkbox alone
                    // is a small target, and merging the title/artist text into
                    // the toggle node lets TalkBack read "song name, checkbox".
                    modifier = Modifier
                        .fillMaxWidth()
                        .heightIn(min = 48.dp)
                        .toggleable(
                            value = checked,
                            role = Role.Checkbox,
                            onValueChange = { searchModel.setSelected(track, it) },
                        )
                        .padding(8.dp),
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Checkbox(
                        checked = checked,
                        onCheckedChange = null,
                    )
                        Column(Modifier.weight(1f)) {
                            Text(track.name, style = MaterialTheme.typography.titleSmall)
                            Text(
                                "${track.artists.ifBlank { stringResource(R.string.music_unknown_artist) }} · ${track.album}",
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                            // Aggregate rows list every platform that carries
                            // the song; single-platform rows fall back to provider.
                            val sourceLabel = MusicSources.label(track, platformLabels)
                            if (sourceLabel.isNotEmpty()) {
                                Text(
                                    stringResource(R.string.music_sources, sourceLabel),
                                    style = MaterialTheme.typography.labelSmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                )
                            }
                            // Only present when a lyric was really saved: a
                            // stale path would make the button a silent no-op.
                            if (lyricPath != null) {
                                val openLabel = stringResource(
                                    R.string.music_lyric_open_named,
                                    track.name,
                                )
                                TextButton(
                                    onClick = {
                                        when (MusicLyricOpener.open(context, lyricPath)) {
                                            MusicLyricOpenResult.Missing -> onMessage(
                                                context.getString(R.string.music_lyric_missing),
                                            )
                                            MusicLyricOpenResult.NoApp -> onMessage(
                                                context.getString(R.string.music_lyric_no_app),
                                            )
                                            MusicLyricOpenResult.Opened -> Unit
                                        }
                                    },
                                    modifier = Modifier.semantics {
                                        contentDescription = openLabel
                                    },
                                ) {
                                    Text(
                                        stringResource(R.string.music_lyrics),
                                        style = MaterialTheme.typography.labelSmall,
                                    )
                                }
                            }
                        }
                        if (track.vip) {
                            Text(
                                stringResource(R.string.music_vip),
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.primary,
                            )
                        }
                    }
                }
            }
            // "Load more" footer: once it scrolls into view the next page is
            // fetched automatically; it disappears entirely when the end of
            // the result list was reached or the provider cannot page at all
            // (`supports_offset = false`: musicdl platforms, aggregate
            // searches). Requesting another page there would only return the
            // first page again or an empty one.
            if (search.tracks.isNotEmpty() &&
                !search.endReached &&
                search.supportsOffset != false
            ) {
                item(key = "music-load-more") {
                    MusicLoadMoreFooter(
                        loadingMore = search.loadingMore,
                        failed = search.loadMoreFailed,
                        onLoadMore = searchModel::loadMore,
                    )
                }
            }
        }
    }

/**
 * Auto-trigger + status row for paged music search: fires [onLoadMore] once
 * it is composed (i.e. the user reached the bottom), shows a spinner while
 * the page is in flight and a retry button when the fetch failed. The
 * "X / Y" progress lives in the count line above the list.
 */
@Composable
private fun MusicLoadMoreFooter(
    loadingMore: Boolean,
    failed: Boolean,
    onLoadMore: () -> Unit,
) {
    LaunchedEffect(loadingMore, failed) {
        if (!loadingMore && !failed) onLoadMore()
    }
    Row(
        modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
        horizontalArrangement = Arrangement.Center,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        if (loadingMore) {
            val desc = stringResource(R.string.music_loading_more)
            CircularProgressIndicator(
                modifier = Modifier
                    .size(16.dp)
                    .semantics { contentDescription = desc },
                strokeWidth = 2.dp,
            )
            Spacer(Modifier.width(8.dp))
            Text(
                desc,
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        } else if (failed) {
            TextButton(onClick = onLoadMore) {
                Text(stringResource(R.string.action_retry))
            }
        }
    }
}

/**
 * Connectivity badge next to the platform picker: a spinner while the probe
 * runs, a green check when the platform answers (contentDescription carries
 * the elapsed ms) or a red cross with the error in a long-press tooltip.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun PlatformHealthBadge(
    checking: Boolean,
    result: MusicHealthItem?,
) {
    when {
        checking -> {
            val desc = stringResource(R.string.music_health_checking)
            CircularProgressIndicator(
                modifier = Modifier
                    .size(20.dp)
                    .semantics { contentDescription = desc },
                strokeWidth = 2.dp,
            )
        }
        result != null && result.ok -> {
            val okText = stringResource(R.string.music_health_ok, result.elapsed_ms)
            // #2E7D32 on white is ~5:1, but the same green on the dark navy
            // surface drops to ~2:1; the lighter tone keeps the badge legible.
            val okTint = if (isSystemInDarkTheme()) HealthOkDark else HealthOkLight
            HealthTooltip(tooltip = okText) {
                // 48 dp touch target around the 24 dp icon: the tooltip is
                // opened with a long press, so the hit area must be tappable.
                Box(
                    modifier = Modifier.size(48.dp),
                    contentAlignment = Alignment.Center,
                ) {
                    Icon(
                        Icons.Outlined.CheckCircle,
                        contentDescription = okText,
                        tint = okTint,
                    )
                }
            }
        }
        result != null -> {
            val failedText = stringResource(
                R.string.music_health_failed,
                result.error.ifBlank { "-" },
            )
            HealthTooltip(tooltip = failedText) {
                Box(
                    modifier = Modifier.size(48.dp),
                    contentAlignment = Alignment.Center,
                ) {
                    Icon(
                        Icons.Outlined.Cancel,
                        contentDescription = failedText,
                        tint = MaterialTheme.colorScheme.error,
                    )
                }
            }
        }
    }
}

/** Long-press (or click) reveals [tooltip] text over the badge. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun HealthTooltip(tooltip: String, content: @Composable () -> Unit) {
    TooltipBox(
        positionProvider = TooltipDefaults.rememberPlainTooltipPositionProvider(),
        tooltip = { PlainTooltip { Text(tooltip) } },
        state = rememberTooltipState(),
        content = content,
    )
}
