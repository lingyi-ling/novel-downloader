package com.lingyi.noveldl.ui.music

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
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
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import com.lingyi.noveldl.R
import com.lingyi.noveldl.data.Api
import com.lingyi.noveldl.data.MusicHealthItem
import com.lingyi.noveldl.data.MusicJobProgress
import com.lingyi.noveldl.data.MusicJobRequest
import com.lingyi.noveldl.data.MusicPlatform
import com.lingyi.noveldl.data.MusicSearchRequest
import com.lingyi.noveldl.data.MusicTrack
import com.lingyi.noveldl.data.SelectionStore
import com.lingyi.noveldl.ui.progressFraction
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/** Per-platform search budget sent to `GET /api/v1/music/health` (seconds). */
private const val MUSIC_HEALTH_TIMEOUT_SECONDS = 5

/** Debounce window before a platform switch actually fires the health probe. */
private const val MUSIC_HEALTH_DEBOUNCE_MS = 1_000L

/** Success tint for the health badge; Material3 has no semantic "ok" color. */
private val HealthOkColor: Color = Color(0xFF388E3C)

@Composable
fun MusicScreen(onMessage: (String) -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val selectionStore = remember(context) { SelectionStore(context) }
    var platforms by remember { mutableStateOf<List<MusicPlatform>>(emptyList()) }
    // Restored from the last session; the API list refreshes it if needed.
    var selectedPlatform by rememberSaveable {
        mutableStateOf(selectionStore.selectedPlatform())
    }
    var menuExpanded by remember { mutableStateOf(false) }
    var keyword by remember { mutableStateOf("") }
    var tracks by remember { mutableStateOf<List<MusicTrack>>(emptyList()) }
    // Paged search state: platform-reported hit total, whether the end of the
    // result list was reached, and whether the last "load more" attempt failed.
    var totalHits by remember { mutableIntStateOf(0) }
    var endReached by remember { mutableStateOf(false) }
    var loadingMore by remember { mutableStateOf(false) }
    var loadMoreFailed by remember { mutableStateOf(false) }
    var selected by remember { mutableStateOf<Set<String>>(emptySet()) }
    var searching by remember { mutableStateOf(false) }
    // Bumped on every new search / platform switch so responses of stale
    // requests cannot merge into a freshly reset list.
    var searchGeneration by remember { mutableIntStateOf(0) }
    var error by remember { mutableStateOf<String?>(null) }
    var jobRunning by remember { mutableStateOf(false) }
    var jobProgress by remember { mutableStateOf(MusicJobProgress()) }
    var platformKey by remember { mutableIntStateOf(0) }
    // Health probe per platform, keyed by platform id; the selected platform's
    // entry drives the badge next to the picker.
    var healthByPlatform by remember { mutableStateOf<Map<String, MusicHealthItem>>(emptyMap()) }
    var healthChecking by remember { mutableStateOf(false) }

    // Checks the selected platform on entry and again on every switch. The
    // 1s delay debounces rapid switching: restarting this effect cancels the
    // pending probe before the request is sent.
    LaunchedEffect(selectedPlatform) {
        val platform = selectedPlatform
        if (platform.isEmpty()) return@LaunchedEffect
        healthChecking = true
        delay(MUSIC_HEALTH_DEBOUNCE_MS)
        val outcome = Api.call {
            Api.service().musicHealth(platform, MUSIC_HEALTH_TIMEOUT_SECONDS)
        }
        if (platform != selectedPlatform) return@LaunchedEffect
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
                        error = it.message ?: "",
                    )
                    )
            }
        healthChecking = false
    }

    LaunchedEffect(platformKey) {
        Api.call { Api.service().musicPlatforms() }
            .onSuccess {
                platforms = it.platforms
                error = null
                val keys = it.platforms.map { platform -> platform.key }
                if (selectedPlatform.isEmpty() || selectedPlatform !in keys) {
                    selectedPlatform = it.platforms.firstOrNull()?.key.orEmpty()
                    selectionStore.saveSelectedPlatform(selectedPlatform)
                }
            }
            .onFailure { error = it.message }
    }

    /** Drops the current result page: new searches and platform switches start over. */
    fun resetSearchResults() {
        searchGeneration++
        tracks = emptyList()
        totalHits = 0
        endReached = false
        loadingMore = false
        loadMoreFailed = false
        selected = emptySet()
    }

    fun search() {
        val query = keyword.trim()
        if (query.isEmpty() || selectedPlatform.isEmpty() || searching) return
        searching = true
        error = null
        resetSearchResults()
        val generation = searchGeneration
        val platform = selectedPlatform
        scope.launch {
            Api.call {
                Api.service().musicSearch(
                    MusicSearchRequest(
                        platform = platform,
                        keyword = query,
                        limit = MUSIC_PAGE_SIZE,
                        offset = 0,
                    ),
                )
            }
                .onSuccess {
                    if (generation != searchGeneration) return@onSuccess
                    tracks = MusicPaging.merge(emptyList(), it.tracks)
                    totalHits = it.total
                    endReached =
                        !MusicPaging.hasMore(tracks.size, it.total, it.tracks.size, 0, MUSIC_PAGE_SIZE)
                }
                .onFailure {
                    if (generation == searchGeneration) error = it.message
                }
            searching = false
        }
    }

    /**
     * Loads the next page with `offset = 当前结果数` and appends it (deduped by
     * track id). Guarded so a page is only ever requested when the previous
     * search finished, no fetch is in flight and the end was not reached.
     */
    fun loadMore() {
        val query = keyword.trim()
        if (query.isEmpty() || selectedPlatform.isEmpty()) return
        if (searching || loadingMore || endReached) return
        loadingMore = true
        loadMoreFailed = false
        val generation = searchGeneration
        val platform = selectedPlatform
        val offset = tracks.size
        scope.launch {
            Api.call {
                Api.service().musicSearch(
                    MusicSearchRequest(
                        platform = platform,
                        keyword = query,
                        limit = MUSIC_PAGE_SIZE,
                        offset = offset,
                    ),
                )
            }
                .onSuccess { page ->
                    if (generation != searchGeneration) return@onSuccess
                    val previous = tracks.size
                    tracks = MusicPaging.merge(tracks, page.tracks)
                    totalHits = page.total
                    endReached = !MusicPaging.hasMore(
                        tracks.size,
                        page.total,
                        page.tracks.size,
                        previous,
                        MUSIC_PAGE_SIZE,
                    )
                }
                .onFailure {
                    if (generation == searchGeneration) {
                        loadMoreFailed = true
                        error = it.message
                    }
                }
            if (generation == searchGeneration) loadingMore = false
        }
    }

    fun downloadSelected() {
        val chosen = tracks.filter { selected.contains(it.id) }
        if (chosen.isEmpty()) {
            onMessage(context.getString(R.string.music_no_selection))
            return
        }
        if (jobRunning) return
        jobRunning = true
        error = null
        scope.launch {
            val created = Api.call {
                Api.service().createMusicJob(
                    MusicJobRequest(platform = selectedPlatform, tracks = chosen),
                )
            }.getOrElse {
                error = it.message
                jobRunning = false
                return@launch
            }
            while (true) {
                delay(1500)
                val job = Api.call { Api.service().musicJob(created.job_id) }
                    .getOrElse {
                        error = it.message
                        jobRunning = false
                        return@launch
                    }
                jobProgress = job.progress
                when (job.status) {
                    "done" -> {
                        val ok = job.results.count { it.status == "ok" }
                        val skip = job.results.count { it.status == "skip" }
                        val fail = job.results.count { it.status == "fail" }
                        onMessage(context.getString(R.string.music_job_done, ok, skip, fail))
                        jobRunning = false
                        return@launch
                    }
                    "failed" -> {
                        onMessage(
                            context.getString(
                                R.string.music_job_failed,
                                job.error ?: job.status,
                            ),
                        )
                        jobRunning = false
                        return@launch
                    }
                }
            }
        }
    }

    Column(modifier = Modifier.fillMaxSize().padding(16.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Box {
                Button(onClick = { menuExpanded = true }) {
                    val label = platforms.firstOrNull { it.key == selectedPlatform }?.label
                        ?: stringResource(R.string.music_platform)
                    Text(label)
                }
                DropdownMenu(expanded = menuExpanded, onDismissRequest = { menuExpanded = false }) {
                    platforms.forEach { platform ->
                        DropdownMenuItem(
                            text = { Text(platform.label) },
                            onClick = {
                                menuExpanded = false
                                if (platform.key != selectedPlatform) {
                                    selectedPlatform = platform.key
                                    selectionStore.saveSelectedPlatform(platform.key)
                                    // Paged results belong to the old platform.
                                    resetSearchResults()
                                }
                            },
                        )
                    }
                }
            }
            Spacer(Modifier.width(12.dp))
            PlatformHealthBadge(
                checking = healthChecking,
                result = healthByPlatform[selectedPlatform],
            )
        }
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(
            value = keyword,
            onValueChange = { keyword = it },
            label = { Text(stringResource(R.string.music_keyword_hint)) },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(8.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = { search() }, enabled = !searching) {
                Text(stringResource(R.string.music_search_button))
            }
            Button(onClick = { downloadSelected() }, enabled = !jobRunning) {
                Text(stringResource(R.string.music_download_selected))
            }
        }
        if (jobRunning) {
            Spacer(Modifier.height(8.dp))
            Text(
                stringResource(
                    R.string.music_job_progress,
                    jobProgress.done,
                    jobProgress.total,
                ),
                style = MaterialTheme.typography.bodySmall,
            )
            Spacer(Modifier.height(4.dp))
            LinearProgressIndicator(
                progress = { progressFraction(jobProgress.done, jobProgress.total) },
                modifier = Modifier.fillMaxWidth(),
            )
        }
        error?.let {
            Spacer(Modifier.height(8.dp))
            Text(
                stringResource(R.string.state_error, it),
                color = MaterialTheme.colorScheme.error,
                style = MaterialTheme.typography.bodySmall,
            )
            TextButton(
                onClick = {
                    if (platforms.isEmpty()) {
                        platformKey++
                    } else {
                        search()
                    }
                },
            ) {
                Text(stringResource(R.string.action_retry))
            }
        }
        if (tracks.isNotEmpty()) {
            Spacer(Modifier.height(8.dp))
            Text(
                if (totalHits > 0) {
                    stringResource(R.string.music_search_progress, tracks.size, totalHits)
                } else {
                    stringResource(R.string.music_tracks, tracks.size)
                },
                style = MaterialTheme.typography.bodySmall,
            )
        }
        Spacer(Modifier.height(8.dp))
        LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            items(tracks) { track ->
                Card(modifier = Modifier.fillMaxWidth()) {
                    Row(
                        modifier = Modifier.fillMaxWidth().padding(8.dp),
                        verticalAlignment = Alignment.CenterVertically,
                    ) {
                        Checkbox(
                            checked = selected.contains(track.id),
                            onCheckedChange = { checked ->
                                selected = if (checked) {
                                    selected + track.id
                                } else {
                                    selected - track.id
                                }
                            },
                        )
                        Column(Modifier.weight(1f)) {
                            Text(track.name, style = MaterialTheme.typography.titleSmall)
                            Text(
                                "${track.artists.ifBlank { stringResource(R.string.music_unknown_artist) }} · ${track.album}",
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
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
            // the result list was reached.
            if (tracks.isNotEmpty() && !endReached) {
                item(key = "music-load-more") {
                    MusicLoadMoreFooter(
                        loadingMore = loadingMore,
                        failed = loadMoreFailed,
                        onLoadMore = { loadMore() },
                    )
                }
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
            HealthTooltip(tooltip = okText) {
                Icon(
                    Icons.Outlined.CheckCircle,
                    contentDescription = okText,
                    tint = HealthOkColor,
                )
            }
        }
        result != null -> {
            val failedText = stringResource(
                R.string.music_health_failed,
                result.error.ifBlank { "-" },
            )
            HealthTooltip(tooltip = failedText) {
                Icon(
                    Icons.Outlined.Cancel,
                    contentDescription = failedText,
                    tint = MaterialTheme.colorScheme.error,
                )
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
