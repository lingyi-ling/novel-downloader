package com.lingyi.noveldl.ui.tasks

import androidx.compose.runtime.saveable.listSaver
import com.lingyi.noveldl.data.AddTaskRequest
import com.lingyi.noveldl.data.HistoryFile
import com.lingyi.noveldl.data.Task
import com.lingyi.noveldl.data.encodeDownloadPathSegment

/**
 * Pure rules behind the task screen interactions: expand/collapse of the
 * detail panel, retrying failed/cancelled tasks via `POST /api/v1/tasks`,
 * turning `exported_paths` into downloadable files, and locating the task a
 * notification tap asked to reveal.
 *
 * Keeping them out of the composable makes the decisions testable on the JVM
 * (see [TaskActionsTest]).
 */

/**
 * Task statuses the user may re-queue: failed, cancelled, or a task the client
 * marked interrupted after the app/server process died mid-flight.
 */
private val RETRYABLE_STATUSES = setOf("failed", "cancelled", "interrupted")

/** Whether [status] is one a retry button may be attached to. */
fun isRetryableTaskStatus(status: String): Boolean =
    status.trim().lowercase() in RETRYABLE_STATUSES

/** [isRetryableTaskStatus] for a task. */
fun Task.isRetryable(): Boolean = isRetryableTaskStatus(status)

/** Adds [taskId] to [expanded] or removes it; returns a new set. */
fun toggleTaskExpanded(expanded: Set<String>, taskId: String): Set<String> =
    if (taskId in expanded) expanded - taskId else expanded + taskId

/**
 * Persists the set of task ids re-enqueued this session, so navigating to
 * another tab and back (or a config change) does not re-enable the retry
 * button of a task whose fresh copy is already queued — the state behind
 * "one re-queue per task id".
 */
val retriedIdsSaver = listSaver<Set<String>, String>(
    save = { saveRetriedIds(it) },
    restore = { restoreRetriedIds(it) },
)

/** The list form [retriedIdsSaver] stores in the saved-instance state. */
fun saveRetriedIds(ids: Set<String>): List<String> = ids.toList()

/** Rebuilds the retried set from its stored list form. */
fun restoreRetriedIds(saved: List<String>): Set<String> = saved.toSet()

/** Whether the retry button reacts to taps for [taskId] right now. */
fun retryEnabled(taskId: String, retriedIds: Set<String>, retryingIds: Set<String>): Boolean =
    taskId !in retriedIds && taskId !in retryingIds

/**
 * Whether [task] qualifies for a retry button at all: only failed/cancelled
 * tasks that still carry a usable site/book_id identity.
 */
fun Task.retryOffered(): Boolean =
    isRetryable() && site.isNotBlank() && book_id.isNotBlank()

/**
 * Whether [task] shows an enabled retry button: a retryable task that was
 * neither already enqueued again ([retriedIds]) nor currently being re-queued
 * ([retryingIds]).
 */
fun Task.retryAvailable(retriedIds: Set<String>, retryingIds: Set<String>): Boolean =
    retryOffered() && retryEnabled(task_id, retriedIds, retryingIds)

/**
 * The `POST /api/v1/tasks` body that re-queues [task] with its original
 * identity, or `null` when the task is not failed/cancelled or its
 * site/book_id are missing (nothing to post).
 *
 * A blank title is sent as `null` so the server falls back to its
 * `"{site} (id = {book_id})"` naming, matching a fresh enqueue.
 */
fun retryRequest(task: Task): AddTaskRequest? {
    if (!task.isRetryable()) return null
    val site = task.site.trim()
    val bookId = task.book_id.trim()
    if (site.isEmpty() || bookId.isEmpty()) return null
    return AddTaskRequest(
        site = site,
        book_id = bookId,
        title = task.title.trim().takeIf { it.isNotEmpty() },
    )
}

/**
 * Turns `exported_paths` (`format -> server file path`) into downloadable
 * [HistoryFile]s, sorted by format key. Each download points at the same
 * `/downloads/{name}` endpoint the history screen uses, with the file name
 * percent-encoded exactly like the API does. Entries with blank paths are
 * dropped; sizes/mtimes are unknown (0) here.
 */
fun taskExportFiles(task: Task): List<HistoryFile> =
    task.exported_paths.entries
        .filter { it.value.isNotBlank() }
        .sortedBy { it.key }
        .map { (_, path) ->
            val name = path.trim().substringAfterLast('/').substringAfterLast('\\')
            HistoryFile(
                name = name,
                ext = name.substringAfterLast('.', "").lowercase(),
                download_url = "/downloads/" + encodeDownloadPathSegment(name),
            )
        }

/**
 * Flat row index of [taskId] in the tasks list, or `null` when it is not
 * listed.
 *
 * Mirrors the item order of `TasksScreen`'s `LazyColumn`: the optional login
 * request block (one header plus one card per request), then each section
 * header followed by its tasks — running, pending, completed. A wrong offset
 * would scroll to an unrelated card, so the mapping is unit-tested.
 */
fun taskRowIndex(
    loginRequestCount: Int,
    runningIds: List<String>,
    pendingIds: List<String>,
    completedIds: List<String>,
    taskId: String,
): Int? {
    var row = 0
    if (loginRequestCount > 0) row += 1 + loginRequestCount
    row += 1
    runningIds.indexOf(taskId).takeIf { it >= 0 }?.let { return row + it }
    row += runningIds.size
    row += 1
    pendingIds.indexOf(taskId).takeIf { it >= 0 }?.let { return row + it }
    row += pendingIds.size
    row += 1
    completedIds.indexOf(taskId).takeIf { it >= 0 }?.let { return row + it }
    return null
}

/** What the tasks screen should do with a notification-originated request. */
sealed interface TaskFocusDecision {

    /** The list is not authoritative yet (no poll has answered). */
    data object Wait : TaskFocusDecision

    /** The notification carried no usable task id: open the list only. */
    data object NoTaskId : TaskFocusDecision

    /** [taskId] is listed at [row]; expand, highlight and scroll to it. */
    data class Reveal(val taskId: String, val row: Int) : TaskFocusDecision

    /** The server answered and no longer knows [taskId]. */
    data class Missing(val taskId: String) : TaskFocusDecision
}

/**
 * Resolves [taskId] against the currently listed tasks.
 *
 * Found beats "not synced": a persisted snapshot restored from disk is good
 * enough to reveal the card immediately. Only after a poll actually answered
 * ([synced]) may an absent id be reported as [TaskFocusDecision.Missing] —
 * otherwise an unreachable server would claim the task never existed.
 */
fun taskFocusDecision(
    taskId: String?,
    synced: Boolean,
    loginRequestCount: Int,
    runningIds: List<String>,
    pendingIds: List<String>,
    completedIds: List<String>,
): TaskFocusDecision {
    val id = taskId?.trim()?.takeIf { it.isNotEmpty() }
        ?: return TaskFocusDecision.NoTaskId
    val row = taskRowIndex(
        loginRequestCount = loginRequestCount,
        runningIds = runningIds,
        pendingIds = pendingIds,
        completedIds = completedIds,
        taskId = id,
    )
    return when {
        row != null -> TaskFocusDecision.Reveal(id, row)
        synced -> TaskFocusDecision.Missing(id)
        else -> TaskFocusDecision.Wait
    }
}
