package com.lingyi.noveldl.data

import android.content.Context
import androidx.annotation.VisibleForTesting
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.nio.file.AtomicMoveNotSupportedException
import java.nio.file.Files
import java.nio.file.StandardCopyOption

/**
 * Reads and writes the task snapshot file atomically.
 *
 * Kept separate from [TaskStore] so JVM unit tests can drive it with a plain
 * [File] (no Android Context). Every failure is contained: a corrupt file
 * loads as an empty list and a failed write returns `false`; losing local
 * history must never crash the Tasks screen.
 */
class TaskSnapshotFile(
    private val file: File,
    private val codec: TaskSnapshotCodec = TaskSnapshotCodec(),
) {

    /** Persisted records, empty when the file is missing or unusable. */
    @Synchronized
    fun load(): List<StoredTask> {
        if (!file.isFile) return emptyList()
        // A corrupt/huge file must not allocate unbounded memory. The normal
        // file is a few tens of KB even with 200 finished tasks.
        if (file.length() > MAX_TASK_SNAPSHOT_BYTES) return emptyList()
        return runCatching {
            codec.decode(file.readText(Charsets.UTF_8))
        }.getOrDefault(emptyList())
    }

    /**
     * Rewrites the snapshot in one step: the content is written to a unique
     * temporary file in the same directory, fsynced, then moved over the
     * target. A process death mid-write therefore leaves either the old file
     * or the complete new one, never a truncated mix.
     *
     * @return whether the file now holds [tasks].
     */
    @Synchronized
    fun save(tasks: List<StoredTask>): Boolean = runCatching {
        val parent = file.absoluteFile.parentFile
            ?: throw IOException("snapshot has no parent directory")
        if (!parent.isDirectory && !parent.mkdirs() && !parent.isDirectory) {
            throw IOException("cannot create $parent")
        }
        val bytes = codec.encode(tasks).toByteArray(Charsets.UTF_8)
        val tmp = File.createTempFile(".${file.name}.", ".tmp", parent)
        try {
            FileOutputStream(tmp).use { out ->
                out.write(bytes)
                out.flush()
                // fsync before the rename so the name never points at data
                // that only exists in the page cache.
                out.fd.sync()
            }
            moveIntoPlace(tmp, file)
        } finally {
            tmp.delete()
        }
        true
    }.getOrDefault(false)

    private fun moveIntoPlace(tmp: File, target: File) {
        try {
            Files.move(
                tmp.toPath(),
                target.toPath(),
                StandardCopyOption.REPLACE_EXISTING,
                StandardCopyOption.ATOMIC_MOVE,
            )
        } catch (_: AtomicMoveNotSupportedException) {
            // Some external/SD filesystems cannot rename atomically; replacing
            // the target is still better than leaving a half-written file.
            Files.move(tmp.toPath(), target.toPath(), StandardCopyOption.REPLACE_EXISTING)
        }
    }
}

/**
 * App-facing task snapshot store backed by the app's private files directory.
 *
 * The file name deliberately differs from the embedded engine's
 * `engine_home/tasks.json` (owned by the Python task manager); the two must
 * not overwrite each other.
 */
class TaskStore private constructor(private val snapshot: TaskSnapshotFile) {

    constructor(context: Context) : this(
        TaskSnapshotFile(File(context.applicationContext.filesDir, TASK_SNAPSHOT_FILE)),
    )

    /** Test seam: drive the store with a plain [File] on the JVM. */
    @VisibleForTesting
    internal constructor(file: File) : this(TaskSnapshotFile(file))

    /** Loads and applies the restart rule (active -> interrupted). */
    fun load(nowMillis: Long = System.currentTimeMillis()): List<StoredTask> =
        restoreTaskSnapshot(snapshot.load(), nowMillis)

    /** Caps and atomically persists [tasks]; returns whether it was written. */
    fun save(tasks: List<StoredTask>): Boolean = snapshot.save(capStoredTasks(tasks))
}
