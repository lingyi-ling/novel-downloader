package com.lingyi.noveldl.ui.tasks

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.expandVertically
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.shrinkVertically
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
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
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.lingyi.noveldl.R
import com.lingyi.noveldl.data.Api
import com.lingyi.noveldl.data.HistoryDownloader
import com.lingyi.noveldl.data.HistoryFile
import com.lingyi.noveldl.data.Task
import com.lingyi.noveldl.data.TasksResponse
import com.lingyi.noveldl.data.downloadFileName
import com.lingyi.noveldl.notifications.TaskNotifier
import com.lingyi.noveldl.ui.taskStatusLabelRes
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun TasksScreen(onMessage: (String) -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val clipboard = LocalClipboardManager.current
    var snapshot by remember { mutableStateOf(TasksResponse()) }
    var error by remember { mutableStateOf<String?>(null) }
    var refreshKey by remember { mutableIntStateOf(0) }
    // Distinguishes a user-initiated refresh from the silent 2s polling loop.
    var refreshing by remember { mutableStateOf(false) }
    // Last polled status per task; the first observation is only a baseline.
    val knownStatuses = remember { mutableMapOf<String, String>() }
    // Task ids whose detail panel is open.
    var expandedIds by remember { mutableStateOf(setOf<String>()) }
    // Task ids re-enqueued this session (retry stays disabled for them) and
    // the ones with a retry request currently in flight.
    var retriedIds by remember { mutableStateOf(setOf<String>()) }
    var retryingIds by remember { mutableStateOf(setOf<String>()) }

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

    fun toggleExpand(task: Task) {
        expandedIds = toggleTaskExpanded(expandedIds, task.task_id)
    }

    /** Re-queues a failed/cancelled task with its original site/book_id/title. */
    fun retry(task: Task) {
        if (!retryEnabled(task.task_id, retriedIds, retryingIds)) return
        val target = retryRequest(task) ?: return
        retryingIds = retryingIds + task.task_id
        scope.launch {
            val outcome = Api.call { Api.service().addTask(target) }
            retryingIds = retryingIds - task.task_id
            outcome
                .onSuccess {
                    // One re-queue per task_id; the fresh copy shows up in
                    // the pending section after the refresh below.
                    retriedIds = retriedIds + task.task_id
                    onMessage(
                        context.getString(R.string.tasks_retry_queued, task.title),
                    )
                    refreshNow()
                }
                .onFailure {
                    onMessage(
                        context.getString(R.string.tasks_retry_failed, it.message ?: ""),
                    )
                }
        }
    }

    /** Downloads an exported file through the same path the history screen uses. */
    fun downloadExport(file: HistoryFile) {
        val displayName = file.name.ifBlank {
            downloadFileName(file.name, file.download_url, file.ext)
        }
        HistoryDownloader.enqueue(context, file)
            .onSuccess {
                onMessage(context.getString(R.string.history_download_started, displayName))
            }
            .onFailure {
                onMessage(
                    context.getString(
                        R.string.history_download_failed,
                        displayName,
                        it.message?.takeIf { message -> message.isNotBlank() }
                            ?: context.getString(R.string.history_download_unknown_error),
                    ),
                )
            }
    }

    fun copyError(message: String) {
        clipboard.setText(AnnotatedString(message))
        onMessage(context.getString(R.string.tasks_error_copied))
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
                    TaskCard(
                        task,
                        cancellable = true,
                        expanded = task.task_id in expandedIds,
                        retryable = task.retryOffered(),
                        retryEnabled = task.retryAvailable(retriedIds, retryingIds),
                        onToggleExpand = { toggleExpand(task) },
                        onCancel = ::cancel,
                        onRetry = ::retry,
                        onDownloadFile = ::downloadExport,
                        onCopyError = ::copyError,
                    )
                }
                item { SectionHeader(stringResource(R.string.tasks_pending), snapshot.pending.size) }
                items(snapshot.pending) { task ->
                    TaskCard(
                        task,
                        cancellable = true,
                        expanded = task.task_id in expandedIds,
                        retryable = task.retryOffered(),
                        retryEnabled = task.retryAvailable(retriedIds, retryingIds),
                        onToggleExpand = { toggleExpand(task) },
                        onCancel = ::cancel,
                        onRetry = ::retry,
                        onDownloadFile = ::downloadExport,
                        onCopyError = ::copyError,
                    )
                }
                item {
                    SectionHeader(stringResource(R.string.tasks_completed), snapshot.completed.size)
                }
                items(snapshot.completed) { task ->
                    TaskCard(
                        task,
                        cancellable = false,
                        expanded = task.task_id in expandedIds,
                        retryable = task.retryOffered(),
                        retryEnabled = task.retryAvailable(retriedIds, retryingIds),
                        onToggleExpand = { toggleExpand(task) },
                        onCancel = ::cancel,
                        onRetry = ::retry,
                        onDownloadFile = ::downloadExport,
                        onCopyError = ::copyError,
                    )
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

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun TaskCard(
    task: Task,
    cancellable: Boolean,
    expanded: Boolean,
    retryable: Boolean,
    retryEnabled: Boolean,
    onToggleExpand: () -> Unit,
    onCancel: (Task) -> Unit,
    onRetry: (Task) -> Unit,
    onDownloadFile: (HistoryFile) -> Unit,
    onCopyError: (String) -> Unit,
) {
    val totalText = if (task.chapters_total > 0) task.chapters_total.toString() else "?"
    Card(onClick = onToggleExpand, modifier = Modifier.fillMaxWidth()) {
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
                Row(verticalAlignment = Alignment.CenterVertically) {
                    if (retryable) {
                        TextButton(
                            onClick = { onRetry(task) },
                            enabled = retryEnabled,
                        ) {
                            Text(stringResource(R.string.action_retry))
                        }
                    }
                    if (cancellable) {
                        TextButton(onClick = { onCancel(task) }) {
                            Text(stringResource(R.string.action_cancel))
                        }
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
            AnimatedVisibility(
                visible = expanded,
                enter = expandVertically() + fadeIn(),
                exit = shrinkVertically() + fadeOut(),
            ) {
                TaskDetailPanel(
                    task,
                    onDownloadFile = onDownloadFile,
                    onCopyError = onCopyError,
                )
            }
        }
    }
}

@Composable
private fun TaskDetailPanel(
    task: Task,
    onDownloadFile: (HistoryFile) -> Unit,
    onCopyError: (String) -> Unit,
) {
    Column(Modifier.fillMaxWidth().padding(top = 8.dp)) {
        DetailRow(stringResource(R.string.tasks_detail_site), task.site)
        DetailRow(stringResource(R.string.tasks_detail_book_id), task.book_id)
        task.error?.takeIf { it.isNotBlank() }?.let { message ->
            Spacer(Modifier.height(8.dp))
            Text(
                stringResource(R.string.tasks_detail_error),
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Text(
                message,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.error,
            )
            TextButton(onClick = { onCopyError(message) }) {
                Text(stringResource(R.string.action_copy))
            }
        }
        Spacer(Modifier.height(8.dp))
        Text(
            stringResource(R.string.tasks_detail_files),
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        val files = taskExportFiles(task)
        if (files.isEmpty()) {
            Text(
                stringResource(R.string.tasks_detail_no_files),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        } else {
            files.forEach { file -> ExportFileRow(file, onDownload = onDownloadFile) }
        }
    }
}

@Composable
private fun DetailRow(label: String, value: String) {
    Column(Modifier.padding(top = 4.dp)) {
        Text(
            label,
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Text(
            value.ifBlank { "-" },
            style = MaterialTheme.typography.bodySmall,
        )
    }
}

@Composable
private fun ExportFileRow(file: HistoryFile, onDownload: (HistoryFile) -> Unit) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .clickable { onDownload(file) }
            .padding(vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            file.name.ifBlank { downloadFileName(file.name, file.download_url, file.ext) },
            style = MaterialTheme.typography.bodySmall,
            maxLines = 1,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.weight(1f),
        )
        Spacer(Modifier.width(8.dp))
        Text(
            stringResource(R.string.action_download),
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.primary,
        )
    }
}