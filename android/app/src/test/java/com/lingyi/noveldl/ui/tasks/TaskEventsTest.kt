package com.lingyi.noveldl.ui.tasks

import com.lingyi.noveldl.data.Task
import com.lingyi.noveldl.data.TasksResponse
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** JVM tests for the terminal-transition rules that drive notifications. */
class TaskEventsTest {

    private fun task(id: String, status: String) = Task(
        task_id = id,
        title = "book-$id",
        status = status,
    )

    @Test
    fun terminalStatusesAreRecognized() {
        assertTrue(isTerminalTaskStatus("completed"))
        assertTrue(isTerminalTaskStatus("failed"))
        assertTrue(isTerminalTaskStatus("cancelled"))
        assertTrue(isTerminalTaskStatus(" COMPLETED "))
        assertFalse(isTerminalTaskStatus("queued"))
        assertFalse(isTerminalTaskStatus("running"))
        assertFalse(isTerminalTaskStatus("processing"))
        assertFalse(isTerminalTaskStatus("exporting"))
        assertFalse(isTerminalTaskStatus(""))
    }

    @Test
    fun firstObservationIsOnlyABaseline() {
        val events = detectTerminalTaskEvents(
            previous = emptyMap(),
            tasks = listOf(task("t1", "completed"), task("t2", "failed")),
        )

        assertTrue(events.isEmpty())
    }

    @Test
    fun runningToCompletedProducesACompletedEvent() {
        val events = detectTerminalTaskEvents(
            previous = mapOf("t1" to "running"),
            tasks = listOf(task("t1", "completed")),
        )

        assertEquals(1, events.size)
        assertEquals(TaskOutcome.COMPLETED, events.single().outcome)
        assertEquals("t1", events.single().task.task_id)
    }

    @Test
    fun queuedToFailedProducesAFailedEvent() {
        val events = detectTerminalTaskEvents(
            previous = mapOf("t1" to "queued"),
            tasks = listOf(task("t1", "failed")),
        )

        assertEquals(1, events.size)
        assertEquals(TaskOutcome.FAILED, events.single().outcome)
    }

    @Test
    fun processingAndExportingAreNonTerminalBaselines() {
        val events = detectTerminalTaskEvents(
            previous = mapOf("t1" to "processing", "t2" to "exporting"),
            tasks = listOf(task("t1", "completed"), task("t2", "failed")),
        )

        assertEquals(listOf(TaskOutcome.COMPLETED, TaskOutcome.FAILED), events.map { it.outcome })
    }

    @Test
    fun noEventWhenTheStatusDoesNotReachATerminalState() {
        val events = detectTerminalTaskEvents(
            previous = mapOf("t1" to "queued"),
            tasks = listOf(task("t1", "running")),
        )

        assertTrue(events.isEmpty())
    }

    @Test
    fun terminalToTerminalDoesNotNotifyTwice() {
        val events = detectTerminalTaskEvents(
            previous = mapOf("t1" to "completed", "t2" to "failed", "t3" to "cancelled"),
            tasks = listOf(
                task("t1", "completed"),
                task("t2", "failed"),
                task("t3", "completed"),
            ),
        )

        assertTrue(events.isEmpty())
    }

    @Test
    fun cancellationIsTerminalButNeverNotifies() {
        val events = detectTerminalTaskEvents(
            previous = mapOf("t1" to "running"),
            tasks = listOf(task("t1", "cancelled")),
        )

        assertTrue(events.isEmpty())
    }

    @Test
    fun unknownPreviousStatusStillNotifiesOnCompletion() {
        val events = detectTerminalTaskEvents(
            previous = mapOf("t1" to "something-new"),
            tasks = listOf(task("t1", "completed")),
        )

        assertEquals(1, events.size)
        assertEquals(TaskOutcome.COMPLETED, events.single().outcome)
    }

    @Test
    fun snapshotKeepsTheLatestStatusPerTask() {
        val snapshot = taskStatusSnapshot(
            listOf(task("t1", "running"), task("t2", "completed")),
        )

        assertEquals(mapOf("t1" to "running", "t2" to "completed"), snapshot)
    }

    @Test
    fun allTasksFlattensSectionsAndDropsDuplicates() {
        val response = TasksResponse(
            pending = listOf(task("t1", "queued")),
            running = listOf(task("t2", "running")),
            completed = listOf(task("t3", "completed"), task("t3", "completed")),
        )

        val tasks = response.allTasks()

        assertEquals(listOf("t1", "t2", "t3"), tasks.map { it.task_id })
    }
}