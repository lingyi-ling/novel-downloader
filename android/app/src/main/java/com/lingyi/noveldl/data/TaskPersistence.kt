package com.lingyi.noveldl.data

import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive

/**
 * Client-side snapshot of the task list, persisted across process restarts.
 *
 * The server (including the embedded engine) keeps tasks in memory for the
 * lifetime of its process. When Android kills the app, that memory — and with
 * it every queued/running task, plus the finished-task list — is gone. This
 * file keeps a small, versioned record on disk so the Tasks screen can show
 * what was there before and mark in-flight work as interrupted/retryable
 * instead of silently losing it.
 *
 * Only [StoredTask] data is persisted; the server stays authoritative for
 * live progress. A record carries just enough to rebuild the card: identity,
 * title, status, chapter counters, a short error summary and the last update
 * time.
 */

/** Schema version of the snapshot file; bumped when the layout changes. */
const val TASK_SNAPSHOT_VERSION: Int = 1

/** File name under the app's private files directory. */
const val TASK_SNAPSHOT_FILE: String = "task_snapshot.json"

/**
 * Client-only status for a task that was active when the app process died (or
 * when the server stopped reporting it). It is never produced by the API; the
 * UI shows it as "interrupted" and keeps the retry entry point available.
 */
const val INTERRUPTED_TASK_STATUS: String = "interrupted"

/** Upper bound for finished/interrupted records kept on disk. */
const val MAX_STORED_TERMINAL_TASKS: Int = 200

/** Error summaries are one or two lines in the UI; keep the file bounded. */
const val MAX_STORED_ERROR_CHARS: Int = 500

/** Titles are rendered as a single line; this is far beyond any real one. */
const val MAX_STORED_TITLE_CHARS: Int = 300

/** Refuse absurdly large snapshot files instead of trying to parse them. */
const val MAX_TASK_SNAPSHOT_BYTES: Long = 4L * 1024 * 1024

/** Statuses that mean the server may still be working on the task. */
private val ACTIVE_TASK_STATUSES = setOf(
    "queued",
    "pending",
    "running",
    "processing",
    "exporting",
)

/** Whether [status] is a state the server can still advance. */
fun isActiveTaskStatus(status: String): Boolean =
    status.trim().lowercase() in ACTIVE_TASK_STATUSES

/** Whether [task] is the client-only "process died" marker. */
fun isInterruptedTaskStatus(status: String): Boolean =
    status.trim().lowercase() == INTERRUPTED_TASK_STATUS

/**
 * One persisted task record. Field names are part of the on-disk schema; keep
 * them stable and add defaults for new fields so older files keep decoding.
 */
@Serializable
data class StoredTask(
    val task_id: String = "",
    val site: String = "",
    val book_id: String = "",
    val title: String = "",
    val status: String = "",
    val chapters_done: Int = 0,
    val chapters_total: Int = 0,
    val progress: Double = 0.0,
    val error: String? = null,
    val exported_paths: Map<String, String> = emptyMap(),
    val created_at: Double = 0.0,
    val finished_at: Double? = null,
    /** Epoch millis of the last write; orders the capped history. */
    val updated_at: Long = 0L,
)

/** Whole-file layout: a version plus the records. */
@Serializable
data class TaskSnapshot(
    val version: Int = TASK_SNAPSHOT_VERSION,
    val tasks: List<StoredTask> = emptyList(),
)

/**
 * JSON codec for [TaskSnapshot].
 *
 * Decoding is deliberately per-entry: a hand-edited or half-written file with
 * one bad record must not discard the rest. Entries without a usable task id,
 * with wrong types or that otherwise fail to decode are silently dropped.
 */
class TaskSnapshotCodec(private val json: Json = SNAPSHOT_JSON) {

    /** Serialises [tasks], applying the terminal-history cap first. */
    fun encode(tasks: List<StoredTask>): String =
        json.encodeToString(
            TaskSnapshot.serializer(),
            TaskSnapshot(version = TASK_SNAPSHOT_VERSION, tasks = capStoredTasks(tasks)),
        )

    /**
     * Parses a snapshot. Returns an empty list for a missing/garbage file, an
     * unreadable root, or a schema version this build does not understand.
     * Individual malformed entries are skipped.
     */
    fun decode(text: String): List<StoredTask> {
        if (text.isBlank()) return emptyList()
        val root = runCatching { json.parseToJsonElement(text) }.getOrNull()
        val entries: List<kotlinx.serialization.json.JsonElement> = when (root) {
            is JsonObject -> {
                val version = (root["version"] as? JsonPrimitive)?.content?.toIntOrNull()
                // A newer writer may have changed the meaning of fields; do not
                // guess. A missing version is treated as the current layout.
                if (version != null && version > TASK_SNAPSHOT_VERSION) return emptyList()
                (root["tasks"] as? JsonArray) ?: return emptyList()
            }
            // Tolerate a bare array from a hand-written file.
            is JsonArray -> root
            else -> return emptyList()
        }
        val decoded = entries.mapNotNull { element ->
            runCatching {
                json.decodeFromJsonElement(StoredTask.serializer(), element)
            }.getOrNull()
        }
        return capStoredTasks(decoded)
    }

    private companion object {
        val SNAPSHOT_JSON: Json = Json {
            ignoreUnknownKeys = true
            explicitNulls = false
            encodeDefaults = true
        }
    }
}

/**
 * Normalises a record read from disk or taken from the wire: trims identity
 * fields, clamps counters/progress and bounds free text. A blank status cannot
 * be a live state, so it becomes [INTERRUPTED_TASK_STATUS] (honest and
 * retryable) rather than pretending the task is fine.
 */
private fun StoredTask.normalized(): StoredTask {
    val total = chapters_total.coerceAtLeast(0)
    val done = chapters_done.coerceAtLeast(0).let { if (total > 0) minOf(it, total) else it }
    val cleanStatus = status.trim().lowercase()
    return copy(
        task_id = task_id.trim(),
        site = site.trim(),
        book_id = book_id.trim(),
        title = title.take(MAX_STORED_TITLE_CHARS),
        status = cleanStatus.ifEmpty { INTERRUPTED_TASK_STATUS },
        chapters_done = done,
        chapters_total = total,
        progress = if (progress.isFinite()) progress.coerceIn(0.0, 1.0) else 0.0,
        error = error
            ?.trim()
            ?.take(MAX_STORED_ERROR_CHARS)
            ?.takeIf { it.isNotEmpty() },
        exported_paths = exported_paths.filterValues { it.isNotBlank() },
    )
}

/** Converts a live API task into the persisted shape. */
fun Task.toStoredTask(nowMillis: Long): StoredTask = StoredTask(
    task_id = task_id,
    site = site,
    book_id = book_id,
    title = title,
    status = status,
    chapters_done = chapters_done,
    chapters_total = chapters_total,
    progress = progress,
    error = error,
    exported_paths = exported_paths,
    created_at = created_at,
    finished_at = finished_at,
    updated_at = nowMillis,
).normalized()

/** Rebuilds the API-facing task shape for rendering. */
fun StoredTask.toTask(): Task = Task(
    task_id = task_id,
    title = title,
    site = site,
    book_id = book_id,
    status = status,
    chapters_done = chapters_done,
    chapters_total = chapters_total,
    progress = progress,
    error = error,
    exported_paths = exported_paths,
    created_at = created_at,
    finished_at = finished_at,
)

/** True when every field except [StoredTask.updated_at] is equal. */
private fun StoredTask.sameContentAs(other: StoredTask): Boolean =
    copy(updated_at = 0L) == other.copy(updated_at = 0L)

/**
 * De-duplicates by task id and caps the finished/interrupted history at
 * [maxTerminal] entries. Active records are never dropped: they are the ones
 * that still need progress and retry affordances.
 */
fun capStoredTasks(
    tasks: List<StoredTask>,
    maxTerminal: Int = MAX_STORED_TERMINAL_TASKS,
): List<StoredTask> {
    val byId = LinkedHashMap<String, StoredTask>()
    for (task in tasks) {
        val normalized = task.normalized()
        if (normalized.task_id.isBlank()) continue
        // Later records win (a fresh copy of the same id replaces an older view).
        byId.remove(normalized.task_id)
        byId[normalized.task_id] = normalized
    }
    val active = byId.values.filter { isActiveTaskStatus(it.status) }
    val history = byId.values
        .filterNot { isActiveTaskStatus(it.status) }
        .sortedWith(
            compareByDescending<StoredTask> { it.updated_at }
                .thenByDescending { it.created_at },
        )
        .take(maxTerminal.coerceAtLeast(0))
    return (active + history).sortedWith(
        compareByDescending<StoredTask> { it.updated_at }.thenBy { it.task_id },
    )
}

/**
 * Applies the restart rule to a freshly loaded snapshot: nothing can still be
 * running in a process that has just started. Every active record becomes
 * [INTERRUPTED_TASK_STATUS] until the first successful poll proves otherwise.
 */
fun restoreTaskSnapshot(tasks: List<StoredTask>, nowMillis: Long): List<StoredTask> =
    capStoredTasks(
        tasks.map { task ->
            if (isActiveTaskStatus(task.status)) {
                task.copy(status = INTERRUPTED_TASK_STATUS, updated_at = nowMillis)
            } else {
                task
            }
        },
    )

/**
 * Merges the authoritative live task list into the persisted history.
 *
 * - A live task replaces its stored record (content-only equality keeps the
 *   old `updated_at`, so an unchanged poll does not trigger a write).
 * - A stored *active* task that the server no longer reports cannot be
 *   running anywhere: it is marked [INTERRUPTED_TASK_STATUS] instead of being
 *   shown as in-progress forever.
 * - Stored terminal records are retained when the server forgot them (its
 *   in-memory list died with its process), capped by [capStoredTasks].
 */
fun mergeTaskSnapshot(
    stored: List<StoredTask>,
    live: List<Task>,
    nowMillis: Long,
): List<StoredTask> {
    val storedById = stored
        .filter { it.task_id.isNotBlank() }
        .associateBy { it.task_id.trim() }
    val liveIds = live.mapNotNull { it.task_id.trim().takeIf(String::isNotEmpty) }.toSet()
    val merged = mutableListOf<StoredTask>()
    val seen = mutableSetOf<String>()

    for (task in live) {
        val id = task.task_id.trim()
        if (id.isEmpty() || !seen.add(id)) continue
        val incoming = task.toStoredTask(nowMillis)
        val previous = storedById[id]
        merged += if (previous != null && previous.sameContentAs(incoming)) previous else incoming
    }
    for (task in stored) {
        val id = task.task_id.trim()
        if (id.isEmpty() || id in liveIds || !seen.add(id)) continue
        merged += if (isActiveTaskStatus(task.status)) {
            task.copy(status = INTERRUPTED_TASK_STATUS, updated_at = nowMillis)
        } else {
            task
        }
    }
    return capStoredTasks(merged)
}

/**
 * Groups a merged snapshot into the three sections the screen renders, using
 * the same status vocabulary as the API (plus [INTERRUPTED_TASK_STATUS] in the
 * finished section, where failed/cancelled tasks already live).
 */
fun storedTasksToResponse(tasks: List<StoredTask>): TasksResponse {
    val pending = mutableListOf<Task>()
    val running = mutableListOf<Task>()
    val completed = mutableListOf<Task>()
    for (stored in tasks) {
        val task = stored.toTask()
        when (stored.status.trim().lowercase()) {
            "queued", "pending" -> pending += task
            "running", "processing", "exporting" -> running += task
            else -> completed += task
        }
    }
    return TasksResponse(pending = pending, running = running, completed = completed)
}
