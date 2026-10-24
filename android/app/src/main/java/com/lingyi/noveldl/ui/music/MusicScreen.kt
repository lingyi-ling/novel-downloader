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
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.Checkbox
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
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
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import com.lingyi.noveldl.R
import com.lingyi.noveldl.data.Api
import com.lingyi.noveldl.data.MusicJobProgress
import com.lingyi.noveldl.data.MusicJobRequest
import com.lingyi.noveldl.data.MusicPlatform
import com.lingyi.noveldl.data.MusicSearchRequest
import com.lingyi.noveldl.data.MusicTrack
import com.lingyi.noveldl.data.SelectionStore
import com.lingyi.noveldl.ui.progressFraction
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

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
    var selected by remember { mutableStateOf<Set<String>>(emptySet()) }
    var searching by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    var jobRunning by remember { mutableStateOf(false) }
    var jobProgress by remember { mutableStateOf(MusicJobProgress()) }
    var platformKey by remember { mutableIntStateOf(0) }

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

    fun search() {
        val query = keyword.trim()
        if (query.isEmpty() || selectedPlatform.isEmpty() || searching) return
        searching = true
        error = null
        scope.launch {
            Api.call {
                Api.service().musicSearch(
                    MusicSearchRequest(platform = selectedPlatform, keyword = query),
                )
            }
                .onSuccess {
                    tracks = it.tracks
                    selected = emptySet()
                }
                .onFailure { error = it.message }
            searching = false
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
                            selectedPlatform = platform.key
                            selectionStore.saveSelectedPlatform(platform.key)
                            menuExpanded = false
                        },
                    )
                }
            }
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
                stringResource(R.string.music_tracks, tracks.size),
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
        }
    }
}
