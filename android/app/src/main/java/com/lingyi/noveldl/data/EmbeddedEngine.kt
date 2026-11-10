package com.lingyi.noveldl.data

import android.content.Context
import android.util.Log
import androidx.annotation.VisibleForTesting
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform

/**
 * Boots the embedded Python engine: the full novel_downloader service layer
 * serving the REST contract on 127.0.0.1:[PORT] inside the app process.
 *
 * `start_server` is idempotent and binds loopback only; a daemon thread is
 * left running for the process lifetime.
 */
object EmbeddedEngine {
    private const val TAG = "EmbeddedEngine"
    const val PORT = 8081
    const val BASE_URL = "http://127.0.0.1:$PORT/"

    @Volatile
    private var started = false

    @Volatile
    private var ready = false

    @Volatile
    var lastError: String? = null
        private set

    /**
     * Whether the bootstrap was scheduled. This becomes true *before* the
     * interpreter has started and the loopback port is listening, so it only
     * means "will not be scheduled twice" — use [isReady] to decide whether a
     * request can succeed.
     */
    val isStarted: Boolean
        get() = started

    /**
     * Whether the bootstrap finished successfully and the loopback server is
     * listening. Stays false while starting and after a failed start (see
     * [lastError]).
     */
    val isReady: Boolean
        get() = ready

    /**
     * Boots the engine at most once per process. Safe (and cheap) to call on
     * the UI thread: the heavy bootstrap runs on the engine thread, and the
     * application context is used so no Activity is retained by the Python
     * runtime for the process lifetime.
     */
    fun ensureStarted(context: Context) {
        val appContext = context.applicationContext ?: context
        val home = appContext.filesDir.resolve("engine_home").absolutePath
        ensureStarted(home) {
            try {
                if (!Python.isStarted()) Python.start(AndroidPlatform(appContext))
                Python.getInstance()
                    .getModule("novel_downloader.apps.embedded.server")
                    .callAttr("start_server", home, PORT)
                Log.i(TAG, "embedded engine ready on port $PORT")
            } catch (t: Throwable) {
                Log.e(TAG, "embedded server failed", t)
                throw t
            }
        }
    }

    /**
     * Schedules [bootstrap] (Python start + server start) exactly once, on
     * the "embedded-engine" thread. [home] is the engine's writable home
     * directory.
     *
     * The bootstrap — `Python.start` in particular — initialises the whole
     * interpreter (hundreds of ms, plus first-run asset extraction), so it
     * must never run on the caller's (UI) thread: this function returns as
     * soon as the thread is scheduled. A failing bootstrap is recorded in
     * [lastError]; it is not retried in this process.
     */
    internal fun ensureStarted(home: String, bootstrap: () -> Unit) {
        if (started) return
        synchronized(this) {
            if (started) return
            started = true
            Thread(
                {
                    try {
                        bootstrap()
                        ready = true
                    } catch (t: Throwable) {
                        lastError = t.message ?: t.javaClass.simpleName
                    }
                },
                "embedded-engine",
            ).start()
        }
    }

    /** Restores the never-started state; unit tests only. */
    @VisibleForTesting
    internal fun resetForTest() {
        synchronized(this) {
            started = false
            ready = false
            lastError = null
        }
    }
}
