package com.lingyi.noveldl.notifications

/**
 * Integer ids for per-task notifications and their PendingIntents.
 *
 * `NotificationManager` identifies a notification by an int: posting the same
 * id updates the existing entry instead of stacking a duplicate, which is what
 * makes "only alert once" meaningful. `String.hashCode()` is stable but only
 * 32 bits, so unrelated task ids collide in practice as soon as enough tasks
 * exist; two tasks sharing an id would overwrite each other's alert.
 *
 * [stableTaskId] is a deterministic 31-bit FNV-1a hash (same id across
 * processes and reinstalls-free restarts). [TaskNotificationIds] additionally
 * resolves collisions against the ids handed out so far in this process, so a
 * collision cannot drop a notification.
 */
object NotificationIds {

    /** Lowest id handed out; keeps clear of any future summary/fixed ids. */
    const val TASK_ID_FLOOR: Int = 1_000

    const val TASK_ID_SPAN: Int = Int.MAX_VALUE - TASK_ID_FLOOR

    private val processAllocator = TaskNotificationIds()

    /** Deterministic id for [taskId], independent of allocation order. */
    fun stableTaskId(taskId: String): Int {
        val hash = fnv1a64(taskId) ushr 1
        return TASK_ID_FLOOR + (hash % TASK_ID_SPAN).toInt()
    }

    /** Stable, process-unique id used for both the notification and its intent. */
    fun taskNotificationId(taskId: String): Int = processAllocator.idFor(taskId)

    /** Next id in the reserved range, wrapping at the top. */
    internal fun nextCandidate(candidate: Int): Int {
        val offset = candidate - TASK_ID_FLOOR
        return TASK_ID_FLOOR + ((offset + 1) % TASK_ID_SPAN)
    }

    /** FNV-1a over UTF-16 code units; charset independent and deterministic. */
    private fun fnv1a64(value: String): Long {
        var hash = -0x340d631b7bdddcdbL
        for (char in value) {
            hash = hash xor char.code.toLong()
            hash *= 0x100000001b3L
        }
        return hash
    }
}

/**
 * Assigns at most one id per task key. Re-asking for the same key returns the
 * same id (so updates replace instead of stacking); a new key that hashes onto
 * an id already in use is moved to the next free slot.
 */
class TaskNotificationIds {

    private val assigned = HashMap<String, Int>()
    private val used = HashSet<Int>()

    @Synchronized
    fun idFor(taskId: String): Int {
        assigned[taskId]?.let { return it }
        var candidate = NotificationIds.stableTaskId(taskId)
        while (!used.add(candidate)) {
            candidate = NotificationIds.nextCandidate(candidate)
        }
        assigned[taskId] = candidate
        return candidate
    }
}
