package com.lingyi.noveldl.ui.tasks

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Card
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import com.lingyi.noveldl.R
import com.lingyi.noveldl.data.Api
import com.lingyi.noveldl.data.Task
import com.lingyi.noveldl.data.TasksResponse
import com.lingyi.noveldl.notifications.TaskNotifier
import com.lingyi.noveldl.ui.taskStatusLabelRes
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun TasksScreen(onMessage: (String) -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var snapshot by remember { mutableStateOf(TasksResponse()) }
    var error by remember { mutableStateOf<String?>(null) }
    var refreshKey by remember { mutableIntStateOf(0) }
    // Distinguishes a user-initiated refresh from the silent 2s polling loop.
    var refreshing by remember { mutableStateOf(false) }
    // Last polled status per task; the first observation is only a baseline.
    val knownStatuses = remember { mutableMapOf<String, String>() }

    LaunchedEffect(refreshKey) {
        while (true) {
            Api.call { Api.service().tasks() }
                .onSuccess { response ->
                    val tasks = response.allTasks()
                    val previous = knownStatuses.toMap()
                    knownStatuses.clear()
                    knownStatuses.putAll(taskStatusSnapshot(tasks))
                    detectTerminalTaskEvents(previous, tasks).forEach { event ->
                        TaskNotifier.notify(context, event)
                    }
                    snapshot = response
                    error = null
                }
                .onFailure { error = it.message }
            refreshing = false
            delay(2000)
        }
    }

    fun refreshNow() {
        if (refreshing) return
        refreshing = true
        refreshKey++
    }

    fun cancel(task: Task) {
        scope.launch {
            Api.call { Api.service().cancelTask(task.task_id) }
                .onSuccess { if (it.ok) onMessage(context.getString(R.string.tasks_cancel_done)) }
                .onFailure { onMessage(context.getString(R.string.state_error, it.message ?: "")) }
        }
    }

    PullToRefreshBox(
        isRefreshing = refreshing,
        onRefresh = { refreshNow() },
        modifier = Modifier.fillMaxSize(),
    ) {
        Column(modifier = Modifier.fillMaxSize().padding(16.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.End,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                TextButton(onClick = { refreshNow() }) {
                    Text(stringResource(R.string.action_refresh))
                }
            }
            error?.let {
                Text(
                    stringResource(R.string.state_error, it),
                    color = MaterialTheme.colorScheme.error,
                    style = MaterialTheme.typography.bodySmall,
                )
                TextButton(onClick = { refreshNow() }) {
                    Text(stringResource(R.string.action_retry))
                }
                Spacer(Modifier.height(8.dp))
            }
            val isEmpty = snapshot.running.isEmpty() &&
                snapshot.pending.isEmpty() &&
                snapshot.completed.isEmpty()
            if (isEmpty && error == null) {
                Text(
                    stringResource(R.string.tasks_empty),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Spacer(Modifier.height(8.dp))
            }
            LazyColumn(
                modifier = Modifier.weight(1f),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                item { SectionHeader(stringResource(R.string.tasks_running), snapshot.running.size) }
                items(snapshot.running) { task ->
                    TaskCard(task, cancellable = true, onCancel = ::cancel)
                }
                item { SectionHeader(stringResource(R.string.tasks_pending), snapshot.pending.size) }
                items(snapshot.pending) { task ->
                    TaskCard(task, cancellable = true, onCancel = ::cancel)
                }
                item {
                    SectionHeader(stringResource(R.string.tasks_completed), snapshot.completed.size)
                }
                items(snapshot.completed) { task ->
                    TaskCard(task, cancellable = false, onCancel = ::cancel)
                }
            }
        }
    }
}

@Composable
private fun SectionHeader(title: String, count: Int) {
    Text(
        "$title ($count)",
        style = MaterialTheme.typography.titleMedium,
        modifier = Modifier.padding(top = 8.dp),
    )
}

@Composable
private fun TaskCard(task: Task, cancellable: Boolean, onCancel: (Task) -> Unit) {
    val totalText = if (task.chapters_total > 0) task.chapters_total.toString() else "?"
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(12.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(task.title, style = MaterialTheme.typography.titleSmall)
                Text(
                    stringResource(taskStatusLabelRes(task.status)),
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.primary,
                )
            }
            Spacer(Modifier.height(4.dp))
            Text(
                "${task.site} · ID ${task.book_id}",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Spacer(Modifier.height(6.dp))
            LinearProgressIndicator(
                progress = { task.progress.toFloat().coerceIn(0f, 1f) },
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(4.dp))
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    stringResource(R.string.tasks_progress, task.chapters_done, totalText),
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                if (cancellable) {
                    TextButton(onClick = { onCancel(task) }) {
                        Text(stringResource(R.string.action_cancel))
                    }
                }
            }
            task.error?.takeIf { it.isNotBlank() }?.let {
                Text(
                    it,
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.error,
                )
            }
        }
    }
}