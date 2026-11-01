package com.lingyi.noveldl.ui

import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * JVM state-machine tests for the shared polling loop: the first pass runs
 * immediately, later passes follow the interval, the loop parks (no network,
 * no catch-up burst) while the foreground gate is closed, and cancellation
 * ends it cleanly.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class PollingTest {

    @Test
    fun firstPollRunsImmediatelyThenOncePerInterval() = runTest {
        var polls = 0
        val job = launch { pollWhileComposed(intervalMs = 2000) { polls++ } }
        runCurrent()
        assertEquals(1, polls)
        // advanceTimeBy is exclusive of the target instant, so the amounts
        // below deliberately step past each resumption point.
        advanceTimeBy(1999)
        assertEquals(1, polls)
        advanceTimeBy(2)
        assertEquals(2, polls)
        advanceTimeBy(6000)
        assertEquals(5, polls)
        job.cancel()
    }

    @Test
    fun loopParksWhileTheForegroundGateIsClosed() = runTest {
        val gate = Channel<Unit>(capacity = Channel.UNLIMITED)
        var polls = 0
        val job = launch {
            pollWhileComposed(intervalMs = 1000, awaitForeground = { gate.receive() }) { polls++ }
        }
        runCurrent()
        // "Backgrounded": the gate never opens, so however much time passes
        // no poll fires (this is the app-sits-in-recents case).
        advanceTimeBy(60_000)
        assertEquals(0, polls)
        // "Resumed": the very next pass runs.
        gate.trySend(Unit)
        runCurrent()
        assertEquals(1, polls)
        job.cancel()
    }

    @Test
    fun resumingAfterALongParkRunsASinglePollNotABurst() = runTest {
        val gate = Channel<Unit>(capacity = Channel.UNLIMITED)
        var polls = 0
        val job = launch {
            pollWhileComposed(intervalMs = 1000, awaitForeground = { gate.receive() }) { polls++ }
        }
        gate.trySend(Unit)
        runCurrent()
        assertEquals(1, polls)
        // Parked again for a "long" time: only the interval delay elapses,
        // the loop sits at the gate and must not fire.
        advanceTimeBy(120_000)
        assertEquals(1, polls)
        gate.trySend(Unit)
        runCurrent()
        // Exactly one poll on resume, not 120 queued ones.
        assertEquals(2, polls)
        job.cancel()
    }

    @Test
    fun cancellingTheLoopStopsFurtherPolls() = runTest {
        var polls = 0
        val job = launch { pollWhileComposed(intervalMs = 1000) { polls++ } }
        runCurrent()
        advanceTimeBy(3001)
        assertEquals(4, polls)
        job.cancelAndJoin()
        advanceTimeBy(10_000)
        assertEquals(4, polls)
    }

    @Test
    fun intervalsAreTheConstantsTheScreensUse() {
        assertEquals(2_000L, TASKS_POLL_INTERVAL_MS)
        assertEquals(1_500L, MUSIC_JOB_POLL_INTERVAL_MS)
    }
}
