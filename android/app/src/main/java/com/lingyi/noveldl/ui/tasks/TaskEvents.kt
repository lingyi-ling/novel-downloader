package com.lingyi.noveldl.ui.tasks

import com.lingyi.noveldl.data.Task
import com.lingyi.noveldl.data.TasksResponse

/**
 * Pure rules deciding which poll results deserve a local notification.
 *
 * Keeping this out of the composable makes the state-transition contract
 * testable on the JVM: a task notifies only when it moves from a non-terminal
 * status to `completed`/`failed`, and a task observed for the first time never
 * notifies (its earlier history is unknown).
 */

/** Terminal statuses reported by the API. */
private val TERMINAL_STATUSES = setOf("completed", "failed", "cancelled")

enum class TaskOutcome { COMPLETED, FAILED }

data class TaskTerminalEvent(val task: Task, val outcome: TaskOutcome)

fun isTerminalTaskStatus(status: String): Boolean =
    status.trim().lowercase() in TERMINAL_STATUSES

/** Flattens the three task sections into one de-duplicated list. */
fun TasksResponse.allTasks(): List<Task> =
    (pending + running + completed).distinctBy { it.task_id }

/** Snapshot of `task_id -> status` used as the baseline for the next poll. */
fun taskStatusSnapshot(tasks: List<Task>): Map<String, String> =
    tasks.associate { it.task_id to it.status }

/**
 * Returns the transitions from [previous] to the statuses in [tasks] that
 * should produce a notification.
 */
fun detectTerminalTaskEvents(
    previous: Map<String, String>,
    tasks: List<Task>,
): List<TaskTerminalEvent> {
    val events = mutableListOf<TaskTerminalEvent>()
    for (task in tasks) {
        val outcome = when (task.status.trim().lowercase()) {
            "completed" -> TaskOutcome.COMPLETED
            "failed" -> TaskOutcome.FAILED
            else -> continue
        }
        val before = previous[task.task_id] ?: continue
        if (isTerminalTaskStatus(before)) continue
        events += TaskTerminalEvent(task, outcome)
    }
    return events
}