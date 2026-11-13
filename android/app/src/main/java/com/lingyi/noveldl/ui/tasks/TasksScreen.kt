package com.lingyi.noveldl.ui.tasks

import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.expandVertically
import androidx.compose.animation.fadeIn
import androidx.compose.animation.fadeOut
import androidx.compose.animation.shrinkVertically
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
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
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.ProgressBarRangeInfo
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.progressBarRangeInfo
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.text.input.VisualTransformation
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.LocalLifecycleOwner
import com.lingyi.noveldl.R
import com.lingyi.noveldl.data.Api
import com.lingyi.noveldl.data.HistoryDownloader
import com.lingyi.noveldl.data.HistoryFile
import com.lingyi.noveldl.data.LoginRequestDto
import com.lingyi.noveldl.data.LoginRequestsResponse
import com.lingyi.noveldl.data.LoginSubmitRequest
import com.lingyi.noveldl.data.StoredTask
import com.lingyi.noveldl.data.Task
import com.lingyi.noveldl.data.TaskStore
import com.lingyi.noveldl.data.TasksResponse
import com.lingyi.noveldl.data.downloadFileName
import com.lingyi.noveldl.data.isInterruptedTaskStatus
import com.lingyi.noveldl.data.mergeTaskSnapshot
import com.lingyi.noveldl.data.storedTasksToResponse
import com.lingyi.noveldl.notifications.TaskNotifier
import com.lingyi.noveldl.ui.TASKS_POLL_INTERVAL_MS
import com.lingyi.noveldl.ui.awaitResumed
import com.lingyi.noveldl.ui.apiErrorMessage
import com.lingyi.noveldl.ui.listPlaceholderRes
import com.lingyi.noveldl.ui.pollWhileComposed
import com.lingyi.noveldl.ui.taskStatusLabelRes
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** How long a task stays highlighted after a notification tap. */
private const val TASK_HIGHLIGHT_MS = 1_600L

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun TasksScreen(
    onMessage: (String) -> Unit,
    // Notification-driven focus request (see TaskOpen); null while none is
    // pending. `focusToken` changes on every new tap, so the same task can be
    // revealed again, and `onFocusHandled` acknowledges exactly that tap.
    focusTaskId: String? = null,
    focusToken: Int? = null,
    onFocusHandled: (Int) -> Unit = {},
) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val clipboard = LocalClipboardManager.current
    // Local snapshot of the task list, written atomically on every meaningful
    // change. The API (and the embedded engine) only keeps tasks in process
    // memory, so without this a killed process loses the queue and its history.
    val taskStore = remember(context) { TaskStore(context) }
    var storedTasks by remember { mutableStateOf(emptyList<StoredTask>()) }
    var snapshotRestored by remember { mutableStateOf(false) }
    // True once the first poll has answered, so an empty screen can tell
    // "still loading" from "no tasks at all".
    var loaded by remember { mutableStateOf(false) }
    var snapshot by remember { mutableStateOf(TasksResponse()) }
    var loginReqs by remember { mutableStateOf(LoginRequestsResponse()) }
    // Credential request currently opened in the submit dialog.
    var editingLogin by remember { mutableStateOf<LoginRequestDto?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var refreshKey by remember { mutableIntStateOf(0) }
    // Distinguishes a user-initiated refresh from the silent 2s polling loop.
    var refreshing by remember { mutableStateOf(false) }
    // Last polled status per task; the first observation is only a baseline.
    val knownStatuses = remember { mutableMapOf<String, String>() }
    // Task ids whose detail panel is open.
    var expandedIds by remember { mutableStateOf(setOf<String>()) }
    // Task ids re-enqueued this session (retry stays disabled for them) and
    // the ones with a retry request currently in flight. The retried set is
    // saved: navigating to another tab and back must not re-enable retry on
    // a task whose fresh copy is already queued (plain `remember` would
    // forget it and allow a duplicate re-queue). In-flight ids stay
    // volatile: the request dies with the screen's scope anyway.
    var retriedIds by rememberSaveable(stateSaver = retriedIdsSaver) {
        mutableStateOf(setOf<String>())
    }
    var retryingIds by remember { mutableStateOf(setOf<String>()) }
    // Credential submission currently in flight; the dialog's button waits.
    var submittingLogin by remember { mutableStateOf(false) }
    // Notifications are off: explain the first skipped alert once instead of
    // letting the task finish silently.
    var notificationHintShown by rememberSaveable { mutableStateOf(false) }
    // True once a poll actually answered. Only then may a notification tap for
    // an unlisted task be reported as missing; an unreachable server must not
    // look like the task was deleted.
    var synced by remember { mutableStateOf(false) }
    // Task flashing after a notification tap.
    var highlightedId by remember { mutableStateOf<String?>(null) }
    val listState = rememberLazyListState()

    val lifecycleOwner = LocalLifecycleOwner.current
    LaunchedEffect(refreshKey) {
        // Restore the on-disk snapshot before the first poll. `TaskStore.load`
        // already reapplies the restart rule: nothing can still be running in
        // a process that has just started, so active records come back as
        // "interrupted" (retryable) until the server proves otherwise. The
        // normalized list is written back so the correction survives another
        // kill before the first successful poll.
        if (!snapshotRestored) {
            val restored = withContext(Dispatchers.IO) { taskStore.load() }
            storedTasks = restored
            if (restored.isNotEmpty()) {
                snapshot = storedTasksToResponse(restored)
                withContext(Dispatchers.IO) { taskStore.save(restored) }
            }
            snapshotRestored = true
        }
        // Navigating away cancels this effect (Api.call rethrows
        // CancellationException, so cancellation stays clean). Composition
        // alone survives the activity being STOPPED, so the loop also parks
        // in the foreground gate while the app sits in the background
        // instead of polling every 2 s forever.
        pollWhileComposed(
            intervalMs = TASKS_POLL_INTERVAL_MS,
            awaitForeground = { lifecycleOwner.lifecycle.awaitResumed() },
        ) {
            val tasksResult = Api.call { Api.service().tasks() }
            val response = tasksResult.getOrNull()
            if (response != null) {
                // From here on the list is authoritative enough to call a
                // notification's task id missing.
                synced = true
                val tasks = response.allTasks()
                val previous = knownStatuses.toMap()
                knownStatuses.clear()
                knownStatuses.putAll(taskStatusSnapshot(tasks))
                detectTerminalTaskEvents(previous, tasks).forEach { event ->
                    val posted = TaskNotifier.notify(context, event)
                    // Silently dropping the alert would look like a lost
                    // task; tell the user once per screen why nothing
                    // popped up (POST_NOTIFICATIONS denied, or the channel
                    // blocked in system settings).
                    if (!posted && !notificationHintShown) {
                        notificationHintShown = true
                        onMessage(context.getString(R.string.tasks_notification_off_hint))
                    }
                }
                // Merge the authoritative live list into the persisted
                // history. A stored task that is active but absent from the
                // response is marked interrupted (and therefore retryable)
                // instead of being shown as in progress forever. The write
                // only happens when the merged content actually changed, so
                // the 2 s poll does not churn the disk.
                val merged = mergeTaskSnapshot(
                    stored = storedTasks,
                    live = tasks,
                    nowMillis = System.currentTimeMillis(),
                )
                val changed = merged != storedTasks
                storedTasks = merged
                snapshot = storedTasksToResponse(merged)
                if (changed) {
                    withContext(Dispatchers.IO) { taskStore.save(merged) }
                }
                error = null
            } else {
                error = apiErrorMessage(context, tasksResult.exceptionOrNull())
            }
            // Login-required sites park their task until credentials arrive;
            // the pending-request list rides along with the task poll.
            Api.call { Api.service().loginRequests() }
                .onSuccess { loginReqs = it }
            loaded = true
            refreshing = false
        }
    }

    // A tap on a task notification asks for one specific card. Resolving it in
    // composition (not inside the effect) keeps the effect from restarting on
    // every 2 s poll while the task list merely changes progress.
    val focusDecision = if (focusToken == null) {
        null
    } else {
        taskFocusDecision(
            taskId = focusTaskId,
            synced = synced,
            loginRequestCount = loginReqs.requests.size,
            runningIds = snapshot.running.map { it.task_id },
            pendingIds = snapshot.pending.map { it.task_id },
            completedIds = snapshot.completed.map { it.task_id },
        )
    }

    LaunchedEffect(focusToken, focusDecision) {
        val token = focusToken ?: return@LaunchedEffect
        when (val decision = focusDecision) {
            // Still waiting for the first answer: keep the request pending so
            // a later poll can still reveal the task (or call it missing).
            null, TaskFocusDecision.Wait -> return@LaunchedEffect
            TaskFocusDecision.NoTaskId -> onFocusHandled(token)
            is TaskFocusDecision.Missing -> {
                // The task is gone (finished and pruned, or never existed):
                // stay on the task page and say so instead of silently
                // dropping the tap.
                onFocusHandled(token)
                onMessage(context.getString(R.string.tasks_open_missing))
            }
            is TaskFocusDecision.Reveal -> {
                expandedIds = expandedIds + decision.taskId
                highlightedId = decision.taskId
                onFocusHandled(token)
                // Acknowledging the request re-keys this effect and would
                // cancel a scroll started here; the screen scope outlives it.
                scope.launch { listState.animateScrollToItem(decision.row) }
            }
        }
    }

    // Fade the highlight out on its own; the id check keeps a newer tap lit.
    LaunchedEffect(highlightedId) {
        val id = highlightedId ?: return@LaunchedEffect
        delay(TASK_HIGHLIGHT_MS)
        if (highlightedId == id) highlightedId = null
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
                .onFailure { onMessage(context.getString(R.string.state_error, apiErrorMessage(context, it))) }
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
                        context.getString(R.string.tasks_retry_failed, apiErrorMessage(context, it)),
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

    /** Submits credential values for [req] and closes the dialog on success. */
    fun submitLogin(req: LoginRequestDto, values: Map<String, String>) {
        // Guarded like every other screen action so a double-tap on the
        // dialog's submit button cannot fire two POSTs.
        if (submittingLogin) return
        submittingLogin = true
        scope.launch {
            Api.call { Api.service().submitLogin(req.req_id, LoginSubmitRequest(values)) }
                .onSuccess {
                    onMessage(context.getString(R.string.login_submitted))
                    editingLogin = null
                    refreshNow()
                }
                .onFailure {
                    onMessage(
                        context.getString(
                            R.string.login_submit_failed,
                            apiErrorMessage(context, it),
                        ),
                    )
                }
            submittingLogin = false
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
                    modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite },
                )
                TextButton(onClick = { refreshNow() }) {
                    Text(stringResource(R.string.action_retry))
                }
                Spacer(Modifier.height(8.dp))
            }
            val isEmpty = snapshot.running.isEmpty() &&
                snapshot.pending.isEmpty() &&
                snapshot.completed.isEmpty() &&
                loginReqs.requests.isEmpty()
            if (isEmpty) {
                listPlaceholderRes(
                    loading = !loaded || refreshing,
                    hasError = error != null,
                    emptyRes = R.string.tasks_empty,
                )?.let { placeholder ->
                    Text(
                        stringResource(placeholder),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    Spacer(Modifier.height(8.dp))
                }
            }
            LazyColumn(
                modifier = Modifier.weight(1f),
                state = listState,
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                if (loginReqs.requests.isNotEmpty()) {
                    item {
                        Column {
                            SectionHeader(
                                stringResource(R.string.login_section_title),
                                loginReqs.requests.size,
                            )
                            Text(
                                stringResource(R.string.login_section_hint),
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                    }
                    items(loginReqs.requests, key = { it.req_id }) { req ->
                        LoginRequestCard(req, onProvide = { editingLogin = req })
                    }
                }
                item { SectionHeader(stringResource(R.string.tasks_running), snapshot.running.size) }
                items(snapshot.running) { task ->
                    TaskCard(
                        task,
                        cancellable = true,
                        expanded = task.task_id in expandedIds,
                        highlighted = task.task_id == highlightedId,
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
                        highlighted = task.task_id == highlightedId,
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
                        highlighted = task.task_id == highlightedId,
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
    editingLogin?.let { req ->
        LoginRequestDialog(
            req = req,
            submitting = submittingLogin,
            onDismiss = { if (!submittingLogin) editingLogin = null },
            onSubmit = ::submitLogin,
        )
    }
}

@Composable
private fun LoginRequestCard(
    req: LoginRequestDto,
    onProvide: () -> Unit,
) {
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(modifier = Modifier.fillMaxWidth().padding(12.dp)) {
            Text(
                req.title.ifBlank { req.req_id },
                style = MaterialTheme.typography.titleSmall,
            )
            Spacer(Modifier.height(4.dp))
            Text(
                stringResource(R.string.login_section_hint),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Spacer(Modifier.height(8.dp))
            TextButton(onClick = onProvide) {
                Text(stringResource(R.string.login_provide))
            }
        }
    }
}

@Composable
private fun LoginRequestDialog(
    req: LoginRequestDto,
    submitting: Boolean,
    onDismiss: () -> Unit,
    onSubmit: (LoginRequestDto, Map<String, String>) -> Unit,
) {
    val context = LocalContext.current
    // One observable buffer per field, seeded with prefill/default values
    // clamped to a layout-safe length (server input is untrusted).
    val buffers = remember(req.req_id) {
        mutableStateMapOf<String, String>().apply {
            req.fields.forEach { field ->
                put(field.name, initialLoginFieldValue(field, req.prefill))
            }
        }
    }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(stringResource(R.string.login_dialog_title)) },
        text = {
            // A site plugin may ask for several fields; scrolling keeps the
            // submit button reachable at a 2.0 font scale or with the keyboard
            // open instead of clipping the lower fields.
            Column(Modifier.verticalScroll(rememberScrollState())) {
                if (req.fields.isEmpty()) {
                    Text(stringResource(R.string.login_dialog_hint))
                }
                req.fields.forEach { field ->
                    val supporting = buildString {
                        if (field.required) append(context.getString(R.string.login_field_required))
                        if (field.description.isNotBlank()) {
                            if (isNotEmpty()) append(" · ")
                            append(field.description)
                        }
                    }
                    OutlinedTextField(
                        value = buffers[field.name].orEmpty(),
                        onValueChange = { buffers[field.name] = clampLoginFieldValue(it) },
                        label = { Text(field.label.ifBlank { field.name }) },
                        placeholder = {
                            if (field.placeholder.isNotBlank()) Text(field.placeholder)
                        },
                        singleLine = true,
                        visualTransformation =
                            if (field.type == "password" || field.type == "cookie") {
                                PasswordVisualTransformation()
                            } else {
                                VisualTransformation.None
                            },
                        supportingText = {
                            if (supporting.isNotBlank()) Text(supporting)
                        },
                        modifier = Modifier.fillMaxWidth(),
                    )
                }
            }
        },
        confirmButton = {
            TextButton(
                enabled = loginSubmitReady(req.fields, buffers.toMap()) && !submitting,
                onClick = {
                    onSubmit(req, loginSubmitValues(req.fields, buffers.toMap()))
                },
            ) {
                Text(stringResource(R.string.login_submit))
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text(stringResource(R.string.action_cancel))
            }
        },
    )
}

@Composable
private fun SectionHeader(title: String, count: Int) {
    Text(
        stringResource(R.string.section_count, title, count),
        style = MaterialTheme.typography.titleMedium,
        modifier = Modifier.padding(top = 8.dp),
    )
}

@OptIn(ExperimentalMaterial3Api::class, ExperimentalLayoutApi::class)
@Composable
private fun TaskCard(
    task: Task,
    cancellable: Boolean,
    expanded: Boolean,
    highlighted: Boolean,
    retryable: Boolean,
    retryEnabled: Boolean,
    onToggleExpand: () -> Unit,
    onCancel: (Task) -> Unit,
    onRetry: (Task) -> Unit,
    onDownloadFile: (HistoryFile) -> Unit,
    onCopyError: (String) -> Unit,
) {
    val totalText = if (task.chapters_total > 0) task.chapters_total.toString() else "?"
    // Every card's action buttons carry the same visible word; naming the
    // task in the description tells a screen reader user which card they are
    // on (and survives an empty server title).
    val taskLabel = task.title.ifBlank {
        task.book_id.ifBlank { task.task_id }
    }
    val detailAction = stringResource(
        if (expanded) R.string.tasks_detail_collapse else R.string.tasks_detail_expand,
    )
    val detailLabel = stringResource(
        if (expanded) {
            R.string.tasks_detail_collapse_named
        } else {
            R.string.tasks_detail_expand_named
        },
        taskLabel,
    )
    val retryLabel = stringResource(R.string.tasks_retry_named, taskLabel)
    val cancelLabel = stringResource(R.string.tasks_cancel_named, taskLabel)
    // A bar without a range would be announced as an unlabelled progress
    // control; the fraction also guards against a NaN reported by an old
    // server, which would make TalkBack read "not available".
    val progressValue = task.progress
        .takeIf { it.isFinite() }
        ?.toFloat()
        ?.coerceIn(0f, 1f)
        ?: 0f
    val progressState = stringResource(R.string.tasks_progress_state)
    // The card itself is not clickable: a clickable ancestor would merge the
    // retry/cancel/download buttons into one node and make them unreachable
    // for TalkBack. The detail button below owns the expand action instead.
    // A notification tap briefly tints and outlines its card so the user sees
    // *which* task the alert was about after the automatic scroll.
    val cardColors = if (highlighted) {
        CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.secondaryContainer)
    } else {
        CardDefaults.cardColors()
    }
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = cardColors,
        border = if (highlighted) {
            BorderStroke(2.dp, MaterialTheme.colorScheme.primary)
        } else {
            null
        },
    ) {
        Column(Modifier.padding(12.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                // Weighted so a long title wraps inside the row instead of
                // squeezing the status label off-screen at a 2.0 font scale.
                Text(
                    taskLabel,
                    style = MaterialTheme.typography.titleSmall,
                    modifier = Modifier.weight(1f),
                )
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
                progress = { progressValue },
                modifier = Modifier
                    .fillMaxWidth()
                    .semantics {
                        contentDescription = progressState
                        progressBarRangeInfo = ProgressBarRangeInfo(progressValue, 0f..1f)
                    },
            )
            Spacer(Modifier.height(4.dp))
            Text(
                stringResource(R.string.tasks_progress, task.chapters_done, totalText),
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            // Actions wrap onto a second line on a narrow screen or at a 2.0
            // font scale instead of clipping the cancel button.
            FlowRow(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp, Alignment.End),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                TextButton(
                    onClick = onToggleExpand,
                    modifier = Modifier.semantics { contentDescription = detailLabel },
                ) {
                    Text(detailAction)
                }
                if (retryable) {
                    TextButton(
                        onClick = { onRetry(task) },
                        enabled = retryEnabled,
                        modifier = Modifier.semantics {
                            contentDescription = retryLabel
                        },
                    ) {
                        Text(stringResource(R.string.action_retry))
                    }
                }
                if (cancellable) {
                    TextButton(
                        onClick = { onCancel(task) },
                        modifier = Modifier.semantics {
                            contentDescription = cancelLabel
                        },
                    ) {
                        Text(stringResource(R.string.action_cancel))
                    }
                }
            }
            val errorText = task.error?.takeIf { it.isNotBlank() }
            if (errorText != null) {
                Text(
                    errorText,
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.error,
                    modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite },
                )
            } else if (isInterruptedTaskStatus(task.status)) {
                // The process died (or the server stopped reporting the task)
                // while it was in flight. Say so instead of leaving a stale
                // progress bar that looks healthy; retry is offered above.
                Text(
                    stringResource(R.string.tasks_interrupted_hint),
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.error,
                    modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite },
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
            // 48 dp minimum so the row clears the recommended touch-target
            // size; the labelSmall text alone is far shorter.
            .heightIn(min = 48.dp)
            .clickable(role = Role.Button) { onDownload(file) }
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