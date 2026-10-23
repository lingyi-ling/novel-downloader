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
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
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
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

private fun statusLabel(status: String): Int = when (status) {
    "queued" -> R.string.tasks_status_queued
    "running" -> R.string.tasks_status_running
    "processing" -> R.string.tasks_status_processing
    "exporting" -> R.string.tasks_status_exporting
    "completed" -> R.string.tasks_status_completed
    "cancelled" -> R.string.tasks_status_cancelled
    else -> R.string.tasks_status_failed
}

@Composable
fun TasksScreen(onMessage: (String) -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var snapshot by remember { mutableStateOf(TasksResponse()) }
    var error by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(Unit) {
        while (true) {
            Api.call { Api.service().tasks() }
                .onSuccess {
                    snapshot = it
                    error = null
                }
                .onFailure { error = it.message }
            delay(2000)
        }
    }

    fun cancel(task: Task) {
        scope.launch {
            Api.call { Api.service().cancelTask(task.task_id) }
                .onSuccess { if (it.ok) onMessage(context.getString(R.string.tasks_cancel_done)) }
                .onFailure { onMessage(context.getString(R.string.state_error, it.message ?: "")) }
        }
    }

    Column(modifier = Modifier.fillMaxSize().padding(16.dp)) {
        error?.let {
            Text(
                stringResource(R.string.state_error, it),
                color = MaterialTheme.colorScheme.error,
                style = MaterialTheme.typography.bodySmall,
            )
            Spacer(Modifier.height(8.dp))
        }
        LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            item { SectionHeader(stringResource(R.string.tasks_running), snapshot.running.size) }
            items(snapshot.running) { task -> TaskCard(task, cancellable = true, onCancel = ::cancel) }
            item { SectionHeader(stringResource(R.string.tasks_pending), snapshot.pending.size) }
            items(snapshot.pending) { task -> TaskCard(task, cancellable = true, onCancel = ::cancel) }
            item { SectionHeader(stringResource(R.string.tasks_completed), snapshot.completed.size) }
            items(snapshot.completed) { task -> TaskCard(task, cancellable = false, onCancel = ::cancel) }
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
                    stringResource(statusLabel(task.status)),
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
