package com.lingyi.noveldl.notifications

import com.lingyi.noveldl.data.Task
import com.lingyi.noveldl.ui.tasks.TaskOutcome
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.joinAll
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

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

    // ---- status transitions ----

    @Test
    fun everyActiveStatusCountsAsRunningRegardlessOfCaseOrPadding() {
        val state = NotificationGroupState()
        val tasks = listOf(
            task("a", "queued"),
            task("b", "PENDING"),
            task("c", " Running "),
            task("d", "PROCESSING"),
            task("e", "exporting"),
        )

        val counts = state.onPoll(tasks)

        assertEquals(5, counts.running)
        assertEquals(0, counts.finished)
        assertFalse("only running tasks: nothing to aggregate", counts.showsSummary)
    }

    @Test
    fun terminalStatusesAreNormalizedTheSameWay() {
        val state = NotificationGroupState()
        state.onPoll(listOf(task("a", "running"), task("b", "running")))

        val counts = state.onPoll(
            listOf(task("a", " COMPLETED "), task("b", "Failed")),
        )

        assertEquals(1, counts.completed)
        assertEquals(1, counts.failed)
    }

    @Test
    fun anUnknownStatusDropsTheMemberInsteadOfGuessing() {
        val state = NotificationGroupState()
        state.onPoll(listOf(task("a", "running"), task("b", "running")))

        val counts = state.onPoll(
            listOf(task("a", "some-new-server-state"), task("b", "running")),
        )

        assertEquals(1, counts.total)
        assertEquals(1, counts.running)
        assertEquals(0, counts.failed)
    }

    @Test
    fun aFinishedTaskThatRunsAgainRejoinsAsRunning() {
        // A retry keeps the same task id, so the member must move back rather
        // than stay counted as a result.
        val state = NotificationGroupState()
        state.onPoll(listOf(task("a", "running"), task("b", "running")))
        state.onTerminal(task("a", "completed"), TaskOutcome.COMPLETED)

        val counts = state.onPoll(
            listOf(task("a", "running"), task("b", "running")),
        )

        assertEquals(NotificationGroupCounts(running = 2), counts)
        assertFalse(counts.showsSummary)
    }

    @Test
    fun theWholeLifecycleMovesThroughTheCounts() {
        val state = NotificationGroupState()

        val started = state.onPoll(
            listOf(task("a", "running"), task("b", "queued")),
        )
        assertEquals(NotificationGroupCounts(running = 2), started)
        assertFalse("no result yet, so no header", started.showsSummary)

        val halfway = state.onTerminal(task("a", "completed"), TaskOutcome.COMPLETED)
        assertEquals(NotificationGroupCounts(running = 1, completed = 1), halfway)
        assertTrue(halfway.showsSummary)

        val done = state.onTerminal(task("b", "failed"), TaskOutcome.FAILED)
        assertEquals(NotificationGroupCounts(completed = 1, failed = 1), done)
        assertFalse("last task ended: the header is canceled", done.showsSummary)
    }

    // ---- de-duplication and repeated results ----

    @Test
    fun theSameTaskListedTwiceCountsOnce() {
        val state = NotificationGroupState()

        val counts = state.onPoll(
            listOf(task("a", "running"), task("a", "running")),
        )

        assertEquals(NotificationGroupCounts(running = 1), counts)
    }

    @Test
    fun idsAreMatchedAfterTrimming() {
        val state = NotificationGroupState()
        state.onPoll(listOf(task(" a ", "running"), task("b", "running")))

        val counts = state.onTerminal(task("a", "completed"), TaskOutcome.COMPLETED)

        assertEquals(1, counts.running)
        assertEquals(1, counts.completed)
        assertEquals(2, counts.total)
    }

    @Test
    fun repeatedCompletionEventsDoNotDoubleCount() {
        val state = NotificationGroupState()
        state.onPoll(listOf(task("a", "running"), task("b", "running")))

        repeat(5) {
            state.onTerminal(task("a", "completed"), TaskOutcome.COMPLETED)
        }
        val counts = state.onTerminal(task("a", "completed"), TaskOutcome.COMPLETED)

        assertEquals(1, counts.completed)
        assertEquals(1, counts.running)
        assertEquals(2, counts.total)
    }

    @Test
    fun aLaterOutcomeReplacesTheEarlierOneWithoutInflatingTheTotal() {
        val state = NotificationGroupState()
        state.onPoll(listOf(task("a", "running"), task("b", "running")))

        state.onTerminal(task("a", "completed"), TaskOutcome.COMPLETED)
        val counts = state.onTerminal(task("a", "failed"), TaskOutcome.FAILED)

        assertEquals(0, counts.completed)
        assertEquals(1, counts.failed)
        assertEquals(2, counts.total)
    }

    @Test
    fun repeatedPollsAfterACompletionKeepTheCountStable() {
        val state = NotificationGroupState()
        state.onPoll(listOf(task("a", "running"), task("b", "running")))
        state.onTerminal(task("a", "completed"), TaskOutcome.COMPLETED)

        val polled = listOf(task("a", "completed"), task("b", "running"))
        val first = state.onPoll(polled)
        val second = state.onPoll(polled)

        assertEquals(NotificationGroupCounts(running = 1, completed = 1), first)
        assertEquals(first, second)
    }

    // ---- cancellation ----

    @Test
    fun aCancelledTaskNeverJoinsTheBatch() {
        val state = NotificationGroupState()

        val counts = state.onPoll(
            listOf(task("a", "cancelled"), task("b", "running")),
        )

        assertEquals(NotificationGroupCounts(running = 1), counts)
    }

    @Test
    fun cancellingTheOnlyRunningTaskRemovesTheSummary() {
        val state = NotificationGroupState()
        state.onPoll(listOf(task("a", "running"), task("b", "running")))
        state.onTerminal(task("a", "completed"), TaskOutcome.COMPLETED)
        assertTrue(state.counts().showsSummary)

        val counts = state.onPoll(listOf(task("a", "completed"), task("b", "cancelled")))

        assertEquals(0, counts.running)
        assertEquals("a cancel is not a failure", 0, counts.failed)
        assertFalse(counts.showsSummary)
    }

    @Test
    fun aCancelledMemberDropsItsEarlierResultToo() {
        // Polls are authoritative: a task the server now reports as cancelled
        // is out of the batch entirely, including any result counted earlier.
        val state = NotificationGroupState()
        state.onPoll(listOf(task("a", "running"), task("b", "running")))
        state.onTerminal(task("a", "completed"), TaskOutcome.COMPLETED)

        val counts = state.onPoll(listOf(task("a", "cancelled"), task("b", "running")))

        assertEquals(NotificationGroupCounts(running = 1), counts)
    }

    @Test
    fun repeatedCancellationsNeverProduceNegativeCounts() {
        val state = NotificationGroupState()
        state.onPoll(listOf(task("a", "running")))

        repeat(20) {
            state.onPoll(listOf(task("a", "cancelled")))
        }
        val counts = state.counts()

        assertEquals(0, counts.running)
        assertEquals(0, counts.total)
        assertFalse(counts.showsSummary)
    }

    // ---- empty and restored state ----

    @Test
    fun anEmptyGroupHasNoCountsAndNoSummary() {
        val state = NotificationGroupState()

        val counts = state.counts()

        assertEquals(NotificationGroupCounts(), counts)
        assertEquals(0, counts.total)
        assertFalse(counts.showsSummary)
    }

    @Test
    fun aVanishedRunningTaskStopsCounting() {
        val state = NotificationGroupState()
        state.onPoll(listOf(task("a", "running")))

        val counts = state.onPoll(emptyList())

        assertEquals(0, counts.total)
        assertFalse(counts.showsSummary)
    }

    @Test
    fun aFreshStateAfterAProcessRestartDoesNotSummarizeHistory() {
        // The previous process ended with results in the shade; its in-memory
        // counts died with it. A restart must not repost a header for them.
        val restarted = NotificationGroupState()

        val history = restarted.onPoll(
            listOf(
                task("a", "completed"),
                task("b", "failed"),
                task("old", "completed"),
            ),
        )

        assertEquals(0, history.total)
        assertFalse(history.showsSummary)
    }

    @Test
    fun theFirstTaskAfterARestartStartsACleanBatch() {
        val restarted = NotificationGroupState()
        restarted.onPoll(listOf(task("old", "completed"), task("older", "failed")))

        val counts = restarted.onPoll(
            listOf(task("old", "completed"), task("new", "running")),
        )

        assertEquals(NotificationGroupCounts(running = 1), counts)
    }

    @Test
    fun aStateSeededWithTerminalEventsOnlyAlsoStartsClean() {
        // onTerminal joins a batch even without a poll, so a restart that only
        // sees terminal events has finished members and no runner; the next
        // running task must clear them.
        val state = NotificationGroupState()
        state.onTerminal(task("x", "completed"), TaskOutcome.COMPLETED)
        state.onTerminal(task("y", "failed"), TaskOutcome.FAILED)
        assertEquals(2, state.counts().total)

        val counts = state.onPoll(
            listOf(task("x", "completed"), task("y", "failed"), task("z", "running")),
        )

        assertEquals(NotificationGroupCounts(running = 1), counts)
    }

    // ---- counts model ----

    @Test
    fun theSummaryNeedsTwoMembersAndBothPhases() {
        // total >= 2 && running > 0 && finished > 0, checked exhaustively so a
        // relaxed rule cannot silently start posting a header for one task.
        for (running in 0..3) {
            for (completed in 0..3) {
                for (failed in 0..3) {
                    val counts = NotificationGroupCounts(running, completed, failed)
                    val expected = counts.total >= 2 && running > 0 &&
                        (completed + failed) > 0
                    assertEquals(
                        "running=$running completed=$completed failed=$failed",
                        expected,
                        counts.showsSummary,
                    )
                }
            }
        }
    }

    @Test
    fun countsDoNotDependOnInsertionOrder() {
        val forward = NotificationGroupState()
        val backward = NotificationGroupState()
        val tasks = listOf(task("a", "running"), task("b", "running"), task("c", "running"))

        forward.onPoll(tasks)
        backward.onPoll(tasks.reversed())

        assertEquals(forward.counts(), backward.counts())
        assertEquals(NotificationGroupCounts(running = 3), forward.counts())
    }

    @Test
    fun theGroupTagAndSummaryIdAreStableLiterals() {
        // Both are persisted by the system: a change would orphan every row
        // already in the shade and could collide with a task notification id.
        assertEquals(
            "com.lingyi.noveldl.notifications.TASK_GROUP",
            TaskNotifier.GROUP_KEY,
        )
        assertEquals(1, TaskNotifier.SUMMARY_ID)
        assertEquals(NotificationIds.SUMMARY_ID, TaskNotifier.SUMMARY_ID)
    }

    // ---- concurrency ----

    @Test
    fun concurrentTerminalEventsAndPollsLoseNoCounts() {
        val state = NotificationGroupState()
        val anchor = task("anchor", "running")
        state.onPoll(listOf(anchor))

        val threads = 8
        val perThread = 7 // 8 * 7 + anchor stays under MAX_GROUP_MEMBERS
        val pool = Executors.newFixedThreadPool(threads)
        val start = CountDownLatch(1)
        val failures = ConcurrentLinkedQueue<Throwable>()
        // Per thread: that thread's own sequence is causally ordered, so a
        // lost update shows up as a total that stalls or drops within it.
        val snapshotsByThread = Array(threads) { mutableListOf<Int>() }

        val futures = (0 until threads).map { thread ->
            pool.submit {
                try {
                    start.await()
                    repeat(perThread) { index ->
                        val id = "w$thread-$index"
                        val outcome = if ((thread + index) % 2 == 0) {
                            TaskOutcome.COMPLETED
                        } else {
                            TaskOutcome.FAILED
                        }
                        state.onTerminal(
                            task(id, outcome.name.lowercase()),
                            outcome,
                        )
                        // The 2 s poll runs on another thread in production.
                        state.onPoll(listOf(anchor))
                        snapshotsByThread[thread] += state.counts().total
                    }
                } catch (e: Throwable) {
                    failures += e
                }
            }
        }
        start.countDown()
        futures.forEach { it.get(30, TimeUnit.SECONDS) }
        pool.shutdown()

        assertTrue("worker failures: $failures", failures.isEmpty())
        val counts = state.counts()
        assertEquals(1, counts.running)
        assertEquals(threads * perThread, counts.finished)
        assertEquals(threads * perThread + 1, counts.total)
        assertTrue("a running anchor plus results must show the header", counts.showsSummary)
        // Every insert is additive and the batch stays under the cap, so a lost
        // update would show up as a total that stalls or drops in a thread.
        for (thread in 0 until threads) {
            val totals = snapshotsByThread[thread]
            assertEquals("worker $thread saw $totals", perThread, totals.size)
            assertEquals("worker $thread saw $totals", totals.sorted(), totals)
        }
    }

    @Test
    fun concurrentTerminalEventsNeverExceedTheCap() {
        val state = NotificationGroupState()
        val threads = 4
        val perThread = 40 // 160 inserts against a 64-member cap
        val pool = Executors.newFixedThreadPool(threads)
        val start = CountDownLatch(1)
        val failures = ConcurrentLinkedQueue<Throwable>()

        val futures = (0 until threads).map { thread ->
            pool.submit {
                try {
                    start.await()
                    repeat(perThread) { index ->
                        state.onTerminal(
                            task("c$thread-$index", "completed"),
                            TaskOutcome.COMPLETED,
                        )
                    }
                } catch (e: Throwable) {
                    failures += e
                }
            }
        }
        start.countDown()
        futures.forEach { it.get(30, TimeUnit.SECONDS) }
        pool.shutdown()

        assertTrue("worker failures: $failures", failures.isEmpty())
        val counts = state.counts()
        assertEquals(NotificationGroupState.MAX_GROUP_MEMBERS, counts.total)
        assertEquals(counts.total, counts.completed + counts.failed)
        assertEquals(0, counts.running)
    }

    @Test
    fun concurrentCoroutineUpdatesAreSerialized() = runBlocking {
        val state = NotificationGroupState()
        state.onPoll(listOf(task("anchor", "running")))
        val perCoroutine = 8
        val coroutines = 4

        val jobs = (0 until coroutines).map { worker ->
            launch(Dispatchers.Default) {
                repeat(perCoroutine) { index ->
                    val id = "k$worker-$index"
                    state.onTerminal(task(id, "completed"), TaskOutcome.COMPLETED)
                    // The anchor keeps a runner in the group; a lost write would
                    // surface here as a total that failed to advance.
                    assertTrue(state.counts().total >= index + 2)
                }
            }
        }
        jobs.joinAll()

        val counts = state.counts()
        assertEquals(1, counts.running)
        assertEquals(coroutines * perCoroutine, counts.completed)
        assertEquals(coroutines * perCoroutine + 1, counts.total)
    }
}