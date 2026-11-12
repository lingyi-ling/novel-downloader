package com.lingyi.noveldl.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

/**
 * JVM tests for [TaskSnapshotFile]/[TaskStore]: the atomic write replaces the
 * previous snapshot, a missing or corrupt file degrades to an empty history,
 * and the app-facing store reapplies the restart rule on load.
 */
class TaskStoreTest {

    @Rule
    @JvmField
    val temp = TemporaryFolder()

    private fun snapshotFile(): File = File(temp.root, TASK_SNAPSHOT_FILE)

    private fun stored(id: String, status: String = "completed"): StoredTask = StoredTask(
        task_id = id,
        site = "b520",
        book_id = "8_8187",
        title = "书 $id",
        status = status,
        chapters_done = 3,
        chapters_total = 10,
        progress = 0.3,
        updated_at = 5L,
    )

    @Test
    fun saveThenLoadRoundTripsAcrossInstances() {
        val tasks = listOf(stored("a"), stored("b", status = "failed"))
        assertTrue(TaskSnapshotFile(snapshotFile()).save(tasks))

        val loaded = TaskSnapshotFile(snapshotFile()).load()

        assertEquals(capStoredTasks(tasks), loaded)
    }

    @Test
    fun missingFileLoadsAsEmpty() {
        assertTrue(TaskSnapshotFile(snapshotFile()).load().isEmpty())
    }

    @Test
    fun truncatedCorruptFileLoadsAsEmptyInsteadOfThrowing() {
        snapshotFile().writeText("{\"version\":1,\"tasks\":[{\"task_id\":\"a\"")

        assertTrue(TaskSnapshotFile(snapshotFile()).load().isEmpty())
    }

    @Test
    fun corruptFileKeepsItsValidEntries() {
        snapshotFile().writeText(
            """{"version":1,"tasks":[{"task_id":"a","status":"completed"},"junk"]}""",
        )

        assertEquals(listOf("a"), TaskSnapshotFile(snapshotFile()).load().map { it.task_id })
    }

    @Test
    fun savingAgainReplacesThePreviousSnapshot() {
        val file = TaskSnapshotFile(snapshotFile())
        assertTrue(file.save(listOf(stored("old"))))

        assertTrue(file.save(listOf(stored("new"))))

        assertEquals(listOf("new"), file.load().map { it.task_id })
    }

    @Test
    fun saveLeavesNoTemporaryFilesBehind() {
        val file = TaskSnapshotFile(snapshotFile())

        assertTrue(file.save(listOf(stored("a"))))

        val leftovers = temp.root.listFiles()?.filter { it.name.endsWith(".tmp") }.orEmpty()
        assertTrue("unexpected temp files: $leftovers", leftovers.isEmpty())
    }

    @Test
    fun storeLoadAppliesTheRestartRule() {
        val store = TaskStore(snapshotFile())
        assertTrue(store.save(listOf(stored("run", status = "running"))))

        val loaded = store.load(nowMillis = 42L)

        assertEquals(INTERRUPTED_TASK_STATUS, loaded.single().status)
        assertEquals(42L, loaded.single().updated_at)
    }

    @Test
    fun storeLoadOfMissingFileIsEmpty() {
        assertTrue(TaskStore(snapshotFile()).load(nowMillis = 1L).isEmpty())
    }

    @Test
    fun storeSaveCapsTheTerminalHistory() {
        val tasks = (0 until MAX_STORED_TERMINAL_TASKS + 25).map { index ->
            stored("t-$index").copy(updated_at = index.toLong())
        }

        assertTrue(TaskStore(snapshotFile()).save(tasks))
        val loaded = TaskSnapshotFile(snapshotFile()).load()

        assertEquals(MAX_STORED_TERMINAL_TASKS, loaded.size)
        assertFalse(loaded.any { it.task_id == "t-0" })
    }
}
