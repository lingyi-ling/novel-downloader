package com.lingyi.noveldl.data

import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotSame
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference

/**
 * Scheduling contract of [EmbeddedEngine]: the bootstrap (Python interpreter
 * start + loopback server) must run exactly once, off the caller thread, and
 * must never block the caller — MainActivity and the settings screen call
 * `ensureStarted` from the main thread.
 */
class EmbeddedEngineTest {

    @After
    fun resetEngine() {
        EmbeddedEngine.resetForTest()
    }

    @Test(timeout = 10_000)
    fun bootstrapRunsOnItsOwnThreadWithoutBlockingTheCaller() {
        val caller = Thread.currentThread()
        val bootstrapThread = AtomicReference<Thread?>(null)
        val entered = CountDownLatch(1)
        val release = CountDownLatch(1)
        val finished = AtomicInteger(0)

        EmbeddedEngine.ensureStarted("/engine/home") {
            bootstrapThread.set(Thread.currentThread())
            entered.countDown()
            // Park until the test releases us: the caller must be able to
            // return while the engine is still booting.
            release.await(2, TimeUnit.SECONDS)
            finished.incrementAndGet()
        }

        assertFalse(
            "ensureStarted must return before the bootstrap finished",
            finished.get() > 0,
        )
        assertTrue("bootstrap must have started", entered.await(5, TimeUnit.SECONDS))
        release.countDown()

        val worker = bootstrapThread.get()
        assertNotNull("bootstrap must record its thread", worker)
        assertNotSame("bootstrap must not run on the caller thread", caller, worker)
        assertEquals("embedded-engine", worker!!.name)
    }

    @Test(timeout = 10_000)
    fun secondEnsureStartedNeverRunsTheBootstrapAgain() {
        val runs = AtomicInteger(0)
        val first = CountDownLatch(1)

        EmbeddedEngine.ensureStarted("/engine/home") {
            runs.incrementAndGet()
            first.countDown()
        }
        assertTrue(first.await(5, TimeUnit.SECONDS))
        EmbeddedEngine.ensureStarted("/engine/home") {
            runs.incrementAndGet()
        }

        // A wrongly duplicated schedule would run the bootstrap a second time.
        assertEquals(1, runs.get())
    }

    @Test(timeout = 10_000)
    fun bootstrapFailureIsRecordedAsLastErrorWithoutThrowing() {
        EmbeddedEngine.ensureStarted("/engine/home") {
            throw IllegalStateException("boom")
        }

        val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5)
        while (EmbeddedEngine.lastError == null && System.nanoTime() < deadline) {
            Thread.sleep(10)
        }
        assertEquals("boom", EmbeddedEngine.lastError)
    }

    @Test
    fun resetForTestRestartsTheSchedulingState() {
        val runs = AtomicInteger(0)
        val done = CountDownLatch(1)
        EmbeddedEngine.ensureStarted("/engine/home") {
            runs.incrementAndGet()
            done.countDown()
        }
        assertTrue(done.await(5, TimeUnit.SECONDS))
        EmbeddedEngine.resetForTest()

        assertNull(EmbeddedEngine.lastError)
        val second = CountDownLatch(1)
        EmbeddedEngine.ensureStarted("/engine/home") {
            runs.incrementAndGet()
            second.countDown()
        }
        assertTrue(second.await(5, TimeUnit.SECONDS))
        assertEquals(2, runs.get())
    }
}
