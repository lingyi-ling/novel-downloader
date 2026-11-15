package com.lingyi.noveldl.notifications

import com.lingyi.noveldl.data.Task
import com.lingyi.noveldl.ui.tasks.TaskOutcome
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * JVM tests for the group-summary rules. No Android types are involved; the
 * notifier only translates these counts into a notification.
 */
class NotificationGroupStateTest {

    private fun task(id: String, status: String) =
        Task(task_id = id, status = status, title = id)

    @Test
    fun aSingleRunningTaskDoesNotNeedASummary() {
        val state = NotificationGroupState()

        val counts = state.onPoll(listOf(task("a", "running")))

        assertEquals(1, counts.running)
        assertEquals(0, counts.total - counts.running)
        assertFalse(counts.showsSummary)
    }

    @Test
    fun runningAndCompletedTasksProduceTheSummaryCounts() {
        val state = NotificationGroupState()
        state.onPoll(listOf(task("a", "running"), task("b", "pending")))

        val counts = state.onPoll(listOf(task("a", "completed"), task("b", "running")))

        assertEquals(1, counts.running)
        assertEquals(1, counts.completed)
        assertEquals(0, counts.failed)
        assertEquals(2, counts.total)
        assertTrue(counts.showsSummary)
    }

    @Test
    fun failuresAreCountedSeparately() {
        val state = NotificationGroupState()
        state.onPoll(
            listOf(task("a", "running"), task("b", "running"), task("c", "running")),
        )

        val counts = state.onPoll(
            listOf(task("a", "completed"), task("b", "failed"), task("c", "running")),
        )

        assertEquals(NotificationGroupCounts(running = 1, completed = 1, failed = 1), counts)
        assertTrue(counts.showsSummary)
    }

    @Test
    fun theSummaryDisappearsWhenEveryTaskHasFinished() {
        val state = NotificationGroupState()
        state.onPoll(listOf(task("a", "running"), task("b", "running")))

        val counts = state.onPoll(
            listOf(task("a", "completed"), task("b", "failed")),
        )

        assertEquals(0, counts.running)
        assertEquals(1, counts.completed)
        assertEquals(1, counts.failed)
        assertFalse("nothing is left to aggregate", counts.showsSummary)
    }

    @Test
    fun theNextRunningTaskStartsAFreshBatch() {
        val state = NotificationGroupState()
        state.onPoll(listOf(task("a", "running"), task("b", "running")))
        state.onPoll(listOf(task("a", "completed"), task("b", "completed")))

        val counts = state.onPoll(
            listOf(
                task("a", "completed"),
                task("b", "completed"),
                task("c", "running"),
            ),
        )

        // The previous batch's two results must not leak into the new one.
        assertEquals(1, counts.running)
        assertEquals(0, counts.completed)
        assertEquals(0, counts.failed)
    }

    @Test
    fun terminalHistoryThatWasNeverSeenRunningIsIgnored() {
        val state = NotificationGroupState()

        val counts = state.onPoll(
            listOf(task("old", "completed"), task("a", "running")),
        )

        assertEquals(1, counts.total)
        assertEquals(0, counts.completed)
    }

    @Test
    fun terminalEventsJoinTheBatchEvenWithoutAPoll() {
        val state = NotificationGroupState()

        state.onTerminal(task("a", "completed"), TaskOutcome.COMPLETED)
        val counts = state.onTerminal(task("b", "failed"), TaskOutcome.FAILED)

        assertEquals(1, counts.completed)
        assertEquals(1, counts.failed)
        // Both are results and nothing is running: the header is not shown.
        assertFalse(counts.showsSummary)
    }

    @Test
    fun aTerminalEventKeepsTheSummaryAliveWhileOtherTasksRun() {
        val state = NotificationGroupState()
        state.onPoll(listOf(task("a", "running"), task("b", "running")))

        val midway = state.onTerminal(task("a", "completed"), TaskOutcome.COMPLETED)
        assertEquals(1, midway.running)
        assertEquals(1, midway.completed)
        assertTrue(midway.showsSummary)

        val last = state.onTerminal(task("b", "failed"), TaskOutcome.FAILED)
        assertEquals(0, last.running)
        assertFalse(last.showsSummary)
    }

    @Test
    fun cancelledAndInterruptedTasksStopCountingWithoutBecomingFailures() {
        val state = NotificationGroupState()
        state.onPoll(listOf(task("a", "running"), task("b", "running")))

        state.onPoll(listOf(task("a", "cancelled"), task("b", "interrupted")))
        val counts = state.counts()

        assertEquals(0, counts.total)
        assertEquals(0, counts.failed)
    }

    @Test
    fun aTaskMissingFromThePollStopsCountingAsRunning() {
        val state = NotificationGroupState()
        state.onPoll(listOf(task("a", "running"), task("b", "running")))

        val counts = state.onPoll(listOf(task("a", "running")))

        assertEquals(1, counts.running)
    }

    @Test
    fun blankIdsAreIgnored() {
        val state = NotificationGroupState()

        val counts = state.onPoll(
            listOf(task("", "running"), task("   ", "completed")),
        )

        assertEquals(0, counts.total)
    }

    @Test
    fun repeatedPollsAreIdempotent() {
        val state = NotificationGroupState()
        val tasks = listOf(task("a", "running"), task("b", "completed"))

        val first = state.onPoll(tasks)
        val second = state.onPoll(tasks)

        assertEquals(first, second)
        assertEquals(NotificationGroupCounts(running = 1, completed = 0), second)
    }

    @Test
    fun theTrackedBatchIsBounded() {
        val state = NotificationGroupState()
        val tasks = (0 until NotificationGroupState.MAX_GROUP_MEMBERS + 20).map {
            task("t$it", "running")
        }

        val counts = state.onPoll(tasks)

        assertEquals(NotificationGroupState.MAX_GROUP_MEMBERS, counts.running)
    }

    @Test
    fun finishedMembersAreEvictedBeforeRunningOnes() {
        val state = NotificationGroupState()
        state.onPoll(listOf(task("live", "running")))
        repeat(NotificationGroupState.MAX_GROUP_MEMBERS - 1) {
            state.onTerminal(task("done$it", "completed"), TaskOutcome.COMPLETED)
        }

        val counts = state.onTerminal(
            task("extra", "completed"),
            TaskOutcome.COMPLETED,
        )

        // The runner must never be the entry dropped at the cap.
        assertEquals(1, counts.running)
        assertEquals(NotificationGroupState.MAX_GROUP_MEMBERS - 1, counts.completed)
        assertEquals(NotificationGroupState.MAX_GROUP_MEMBERS, counts.total)
    }
}