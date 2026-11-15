package com.lingyi.noveldl.notifications

import com.lingyi.noveldl.data.Task
import com.lingyi.noveldl.data.isActiveTaskStatus
import com.lingyi.noveldl.ui.tasks.TaskOutcome

/**
 * One task as seen by the notification group.
 *
 * [RUNNING] covers every server status that can still advance (queued,
 * pending, running, ...); a task moves to [COMPLETED] / [FAILED] once a poll or
 * a terminal event says so. Cancelled and interrupted tasks drop out of the
 * group instead of being reported as a failure.
 */
enum class GroupMemberState { RUNNING, COMPLETED, FAILED }

/**
 * Counts rendered in the group summary.
 *
 * [showsSummary] is the quiet-shade rule: the summary exists only while at
 * least one task is still running *and* at least one result notification is
 * already in the group. A lone finished task therefore keeps exactly the
 * notification it had before grouping, and once the last task stops the
 * summary is canceled (nothing is left to aggregate).
 */
data class NotificationGroupCounts(
    val running: Int = 0,
    val completed: Int = 0,
    val failed: Int = 0,
) {
    val total: Int get() = running + completed + failed

    val finished: Int get() = completed + failed

    val showsSummary: Boolean get() = total >= 2 && running > 0 && finished > 0
}

/**
 * Tracks which tasks belong to the current notification batch and what they
 * report, so the group summary can say "running / completed / failed".
 *
 * Android-free on purpose: the summary rules run in plain JVM tests. The
 * notifier feeds it from two sources, both of which are necessary:
 *
 * - [onPoll] supplies the live task list. Without it the notifier would only
 *   ever learn about finished tasks and could never say how many are running.
 * - [onTerminal] supplies the transition events themselves, so a task that was
 *   never seen in a poll still joins the batch it finished in.
 *
 * Batch lifetime: members are added when a task is seen active or finishes.
 * Completed tasks stay as members while the batch runs (they are the "完成"
 * count). When nothing is running the summary is canceled; the next newly
 * active task starts a fresh batch, so an old result cannot leak its count
 * into the next set of downloads.
 */
class NotificationGroupState {

    private val members = LinkedHashMap<String, GroupMemberState>()

    /**
     * Applies one authoritative poll: tasks that are active join the batch,
     * tracked tasks move to their new state, and tasks that vanished stop
     * counting as running. Already-terminal history is ignored so a newly
     * started app does not summarize the previous 200 downloads.
     */
    @Synchronized
    fun onPoll(tasks: List<Task>): NotificationGroupCounts {
        val liveIds = HashSet<String>(tasks.size)
        for (task in tasks) {
            val id = task.task_id.trim()
            if (id.isEmpty()) continue
            liveIds += id
            val state = stateFor(task.status)
            when {
                // Cancelled, interrupted or unknown: not running, not a failure.
                state == null -> members.remove(id)
                members.containsKey(id) -> members[id] = state
                state == GroupMemberState.RUNNING -> {
                    startNewBatchIfIdle()
                    put(id, state)
                }
                // Terminal before this batch began: history, not an alert.
            }
        }
        // A task that disappeared from the live list can no longer be running.
        // Terminal members stay: their notifications are still in the shade
        // and belong in the "完成/失败" counts until the batch ends.
        members.entries.removeAll {
            it.value == GroupMemberState.RUNNING && it.key !in liveIds
        }
        return counts()
    }

    /**
     * Records a notification-worthy transition. Terminal members are never
     * cleared here: several tasks finishing in the same poll must land in one
     * summary, so stale results are only discarded when new work starts.
     */
    @Synchronized
    fun onTerminal(task: Task, outcome: TaskOutcome): NotificationGroupCounts {
        val id = task.task_id.trim()
        if (id.isEmpty()) return counts()
        val state = when (outcome) {
            TaskOutcome.COMPLETED -> GroupMemberState.COMPLETED
            TaskOutcome.FAILED -> GroupMemberState.FAILED
        }
        put(id, state)
        return counts()
    }

    /** Current counts; safe to call without changing the batch. */
    @Synchronized
    fun counts(): NotificationGroupCounts = NotificationGroupCounts(
        running = members.values.count { it == GroupMemberState.RUNNING },
        completed = members.values.count { it == GroupMemberState.COMPLETED },
        failed = members.values.count { it == GroupMemberState.FAILED },
    )

    private fun startNewBatchIfIdle() {
        val idle = members.isNotEmpty() && members.values.none {
            it == GroupMemberState.RUNNING
        }
        if (idle) members.clear()
    }

    /**
     * Inserts or updates one member, keeping the map bounded. Finished entries
     * are evicted before running ones, so a long result history cannot push an
     * active task out of the counts.
     */
    private fun put(id: String, state: GroupMemberState) {
        if (members.size >= MAX_GROUP_MEMBERS && !members.containsKey(id)) {
            val oldest = members.entries.firstOrNull {
                it.value != GroupMemberState.RUNNING
            } ?: members.entries.firstOrNull()
            if (oldest != null) members.remove(oldest.key)
        }
        members[id] = state
    }

    private fun stateFor(status: String): GroupMemberState? {
        val normalized = status.trim().lowercase()
        return when {
            isActiveTaskStatus(normalized) -> GroupMemberState.RUNNING
            normalized == "completed" -> GroupMemberState.COMPLETED
            normalized == "failed" -> GroupMemberState.FAILED
            else -> null
        }
    }

    companion object {
        /** Bound on tracked tasks; far above any realistic concurrent batch. */
        const val MAX_GROUP_MEMBERS: Int = 64
    }
}