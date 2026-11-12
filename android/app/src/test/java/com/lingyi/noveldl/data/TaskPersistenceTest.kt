package com.lingyi.noveldl.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * JVM tests for the persisted task snapshot: JSON round-trip, per-entry
 * corruption tolerance, the terminal-history cap, and the status corrections
 * applied after a process restart or when the server stops reporting a task.
 */
class TaskPersistenceTest {

    private val codec = TaskSnapshotCodec()

    private fun stored(
        id: String,
        status: String = "running",
        updatedAt: Long = 1L,
        site: String = "b520",
        bookId: String = "8_8187",
        error: String? = null,
    ): StoredTask = StoredTask(
        task_id = id,
        site = site,
        book_id = bookId,
        title = "书 $id",
        status = status,
        chapters_done = 3,
        chapters_total = 10,
        progress = 0.3,
        error = error,
        updated_at = updatedAt,
    )

    private fun live(id: String, status: String): Task = Task(
        task_id = id,
        title = "书 $id",
        site = "b520",
        book_id = "8_8187",
        status = status,
        chapters_done = 4,
        chapters_total = 10,
        progress = 0.4,
    )

    // ---- save / load round-trip ----

    @Test
    fun encodeThenDecodeKeepsEveryPersistedField() {
        val tasks = listOf(
            stored("a", status = "running", updatedAt = 20L, error = "network timeout"),
            stored("b", status = "completed", updatedAt = 10L).copy(
                chapters_done = 10,
                progress = 1.0,
                exported_paths = mapOf("epub" to "/out/书.epub"),
            ),
        )

        val decoded = codec.decode(codec.encode(tasks))

        assertEquals(capStoredTasks(tasks), decoded)
        assertEquals("network timeout", decoded.single { it.task_id == "a" }.error)
        assertEquals(
            mapOf("epub" to "/out/书.epub"),
            decoded.single { it.task_id == "b" }.exported_paths,
        )
    }

    @Test
    fun encodedFileCarriesTheSchemaVersion() {
        val text = codec.encode(listOf(stored("a", status = "completed")))

        assertTrue(text.contains("\"version\":$TASK_SNAPSHOT_VERSION"))
        assertEquals(listOf("a"), codec.decode(text).map { it.task_id })
    }

    // ---- corrupt files / entries ----

    @Test
    fun garbageFileDecodesAsEmpty() {
        assertTrue(codec.decode("").isEmpty())
        assertTrue(codec.decode("not json at all").isEmpty())
        assertTrue(codec.decode("{}").isEmpty())
        assertTrue(codec.decode("[]").isEmpty())
    }

    @Test
    fun futureSchemaVersionIsIgnored() {
        val text = """{"version":${TASK_SNAPSHOT_VERSION + 1},"tasks":[
            {"task_id":"a","status":"completed"}
        ]}"""

        assertTrue(codec.decode(text).isEmpty())
    }

    @Test
    fun malformedEntriesAreDroppedWithoutLosingTheGoodOnes() {
        val text = """
            {"version":1,"tasks":[
              {"task_id":"good","status":"completed","chapters_done":2,"chapters_total":5},
              {"task_id":"","status":"running"},
              {"task_id":"wrong-type","status":"completed","chapters_total":"oops"},
              "not-an-object"
            ]}
        """.trimIndent()

        val decoded = codec.decode(text)

        assertEquals(listOf("good"), decoded.map { it.task_id })
        assertEquals(2, decoded.single().chapters_done)
    }

    @Test
    fun recordsAreSanitizedOnRead() {
        val text = """
            {"version":1,"tasks":[
              {"task_id":" a ","site":" b520 ","status":" RUNNING ",
               "chapters_done":99,"chapters_total":10,"progress":3.5,
               "error":"  boom  ","exported_paths":{"txt":"   ","epub":"/out/a.epub"}}
            ]}
        """.trimIndent()

        val task = codec.decode(text).single()

        assertEquals("a", task.task_id)
        assertEquals("b520", task.site)
        assertEquals("running", task.status)
        assertEquals(10, task.chapters_done)
        assertEquals(1.0, task.progress, 0.0)
        assertEquals("boom", task.error)
        assertEquals(mapOf("epub" to "/out/a.epub"), task.exported_paths)
    }

    @Test
    fun errorSummariesAreBounded() {
        val longError = "x".repeat(MAX_STORED_ERROR_CHARS * 3)

        val merged = mergeTaskSnapshot(
            stored = emptyList(),
            live = listOf(Task(task_id = "t", status = "failed", error = longError)),
            nowMillis = 1L,
        )

        assertEquals(MAX_STORED_ERROR_CHARS, merged.single().error!!.length)
    }

    // ---- terminal-history cap ----

    @Test
    fun terminalHistoryIsCappedButActiveTasksSurvive() {
        val finished = (0 until MAX_STORED_TERMINAL_TASKS + 50).map { index ->
            stored("done-$index", status = "completed", updatedAt = index.toLong())
        }
        val active = listOf(
            stored("run-1", status = "running", updatedAt = 0L),
            stored("run-2", status = "queued", updatedAt = 0L),
            stored("run-3", status = "processing", updatedAt = 0L),
        )

        val capped = capStoredTasks(finished + active)
        val ids = capped.map { it.task_id }.toSet()

        assertEquals(MAX_STORED_TERMINAL_TASKS + active.size, capped.size)
        assertTrue(active.all { it.task_id in ids })
        // The oldest finished records are the ones dropped.
        assertFalse("done-0" in ids)
        assertFalse("done-49" in ids)
        assertTrue("done-${MAX_STORED_TERMINAL_TASKS + 49}" in ids)
    }

    @Test
    fun duplicateTaskIdsCollapseToTheNewestRecord() {
        val capped = capStoredTasks(
            listOf(
                stored("t", status = "completed", updatedAt = 1L),
                stored("t", status = "running", updatedAt = 2L),
            ),
        )

        assertEquals(1, capped.size)
        assertEquals("running", capped.single().status)
    }

    // ---- status correction ----

    @Test
    fun restoreMarksActiveTasksInterruptedAfterRestart() {
        val restored = restoreTaskSnapshot(
            tasks = listOf(
                stored("run", status = "running"),
                stored("queued", status = "queued"),
                stored("done", status = "completed"),
                stored("already", status = INTERRUPTED_TASK_STATUS),
            ),
            nowMillis = 77L,
        )
        val byId = restored.associateBy { it.task_id }

        assertEquals(INTERRUPTED_TASK_STATUS, byId.getValue("run").status)
        assertEquals(INTERRUPTED_TASK_STATUS, byId.getValue("queued").status)
        assertEquals(77L, byId.getValue("run").updated_at)
        assertEquals("completed", byId.getValue("done").status)
        assertEquals(INTERRUPTED_TASK_STATUS, byId.getValue("already").status)
        assertTrue(isInterruptedTaskStatus(byId.getValue("run").status))
    }

    @Test
    fun mergeMarksAnActiveTaskMissingFromTheLiveListInterrupted() {
        val merged = mergeTaskSnapshot(
            stored = listOf(
                stored("gone", status = "running", updatedAt = 1L),
                stored("still", status = "processing", updatedAt = 1L),
                stored("done", status = "completed", updatedAt = 1L),
            ),
            live = listOf(live("still", status = "running"), live("new", status = "queued")),
            nowMillis = 100L,
        )
        val byId = merged.associateBy { it.task_id }

        assertEquals(INTERRUPTED_TASK_STATUS, byId.getValue("gone").status)
        assertEquals(100L, byId.getValue("gone").updated_at)
        // A task the server still reports keeps (and updates) its live state.
        assertEquals("running", byId.getValue("still").status)
        assertEquals(4, byId.getValue("still").chapters_done)
        // New live tasks are added; finished history is retained.
        assertEquals("queued", byId.getValue("new").status)
        assertEquals("completed", byId.getValue("done").status)
        assertEquals(4, merged.size)
    }

    @Test
    fun mergeKeepsTheOldTimestampWhenNothingChanged() {
        val original = stored("t", status = "running", updatedAt = 5L)
        val unchangedLive = Task(
            task_id = "t",
            title = original.title,
            site = original.site,
            book_id = original.book_id,
            status = original.status,
            chapters_done = original.chapters_done,
            chapters_total = original.chapters_total,
            progress = original.progress,
            created_at = original.created_at,
        )

        val merged = mergeTaskSnapshot(listOf(original), listOf(unchangedLive), nowMillis = 500L)

        assertEquals(5L, merged.single().updated_at)
        assertEquals(listOf(original), merged)
    }

    @Test
    fun interruptedRecordsStayInTheFinishedSectionAndAreStillRendered() {
        val response = storedTasksToResponse(
            listOf(
                stored("run", status = "running"),
                stored("queued", status = "queued"),
                stored("gone", status = INTERRUPTED_TASK_STATUS),
                stored("done", status = "completed"),
            ),
        )

        assertEquals(listOf("queued"), response.pending.map { it.task_id })
        assertEquals(listOf("run"), response.running.map { it.task_id })
        assertEquals(
            setOf("gone", "done"),
            response.completed.map { it.task_id }.toSet(),
        )
        assertEquals("b520", response.completed.first { it.task_id == "gone" }.site)
    }

    @Test
    fun blankStoredStatusIsTreatedAsInterruptedNotRunning() {
        val decoded = codec.decode("""{"version":1,"tasks":[{"task_id":"t"}]}""")

        assertEquals(INTERRUPTED_TASK_STATUS, decoded.single().status)
        assertFalse(isActiveTaskStatus(decoded.single().status))
        assertNull(decoded.single().error)
    }
}
