package com.lingyi.noveldl.ui.tasks

import com.lingyi.noveldl.data.AddTaskRequest
import com.lingyi.noveldl.data.HistoryFile
import com.lingyi.noveldl.data.Task
import com.lingyi.noveldl.data.encodeDownloadPathSegment

/**
 * Pure rules behind the task screen interactions: expand/collapse of the
 * detail panel, retrying failed/cancelled tasks via `POST /api/v1/tasks`,
 * and turning `exported_paths` into downloadable files.
 *
 * Keeping them out of the composable makes the decisions testable on the JVM
 * (see [TaskActionsTest]).
 */

/** Task statuses the user may re-queue: only failed or cancelled tasks. */
private val RETRYABLE_STATUSES = setOf("failed", "cancelled")

/** Whether [status] is one a retry button may be attached to. */
fun isRetryableTaskStatus(status: String): Boolean =
    status.trim().lowercase() in RETRYABLE_STATUSES

/** [isRetryableTaskStatus] for a task. */
fun Task.isRetryable(): Boolean = isRetryableTaskStatus(status)

/** Adds [taskId] to [expanded] or removes it; returns a new set. */
fun toggleTaskExpanded(expanded: Set<String>, taskId: String): Set<String> =
    if (taskId in expanded) expanded - taskId else expanded + taskId

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
