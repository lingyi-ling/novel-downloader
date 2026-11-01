package com.lingyi.noveldl.ui

import android.os.Handler
import android.os.Looper
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.LifecycleOwner
import kotlinx.coroutines.delay
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlin.coroutines.resume

/**
 * Polling-loop plumbing shared by the Tasks screen (task list + login
 * requests) and the Music screen (download-job watch).
 *
 * `LaunchedEffect` cancels its coroutine when the screen leaves composition,
 * which stops a plain `while (true) { poll(); delay() }` loop on navigation —
 * [com.lingyi.noveldl.data.Api.call] rethrows `CancellationException`, so the
 * cancellation stays clean. Composition alone does not follow the activity
 * lifecycle, though: with the app merely backgrounded (activity STOPPED, the
 * screen still composed) such a loop keeps hitting the network every tick
 * forever. [pollWhileComposed] therefore parks in [awaitForeground] before
 * every pass; the production gate, [Lifecycle.awaitResumed], suspends until
 * the host is RESUMED again.
 */

/** Task/login poll interval on the Tasks screen. */
const val TASKS_POLL_INTERVAL_MS = 2_000L

/** Music download-job watch interval. */
const val MUSIC_JOB_POLL_INTERVAL_MS = 1_500L

/**
 * Runs [block] immediately and then once every [intervalMs].
 *
 * Before each pass the loop awaits [awaitForeground], which must suspend for
 * as long as polling is not allowed (app in background) and return as soon as
 * it is allowed again; the no-op default keeps the loop free-running for
 * callers and tests without a lifecycle. The loop ends only by cancellation
 * of its coroutine (the [delay] forwards it), so callers need no exit logic.
 */
suspend fun pollWhileComposed(
    intervalMs: Long,
    awaitForeground: suspend () -> Unit = {},
    block: suspend () -> Unit,
) {
    while (true) {
        awaitForeground()
        block()
        delay(intervalMs)
    }
}

/** Removes lifecycle observers on the main thread, as [Lifecycle] requires. */
private val mainHandler by lazy { Handler(Looper.getMainLooper()) }

/**
 * Suspends until this lifecycle reaches at least [Lifecycle.State.RESUMED].
 *
 * Returns immediately when already resumed. The observer is removed both on
 * resume and on coroutine cancellation, so parking never leaks callbacks into
 * the lifecycle; removal hops to the main thread because registration and
 * lifecycle callbacks always arrive there.
 */
suspend fun Lifecycle.awaitResumed() {
    if (currentState.isAtLeast(Lifecycle.State.RESUMED)) return
    val lifecycle = this
    suspendCancellableCoroutine { continuation ->
        val observer = object : LifecycleEventObserver {
            override fun onStateChanged(source: LifecycleOwner, event: Lifecycle.Event) {
                if (lifecycle.currentState.isAtLeast(Lifecycle.State.RESUMED)) {
                    lifecycle.removeObserver(this)
                    if (continuation.isActive) continuation.resume(Unit)
                }
            }
        }
        lifecycle.addObserver(observer)
        continuation.invokeOnCancellation {
            mainHandler.post { lifecycle.removeObserver(observer) }
        }
        // The state may have advanced between the fast path and registration.
        if (lifecycle.currentState.isAtLeast(Lifecycle.State.RESUMED)) {
            lifecycle.removeObserver(observer)
            if (continuation.isActive) continuation.resume(Unit)
        }
    }
}
