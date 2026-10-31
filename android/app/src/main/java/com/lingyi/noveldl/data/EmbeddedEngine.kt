package com.lingyi.noveldl.data

import android.content.Context
import android.util.Log
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
    var lastError: String? = null
        private set

    val isStarted: Boolean
        get() = started

    fun ensureStarted(context: Context) {
        if (started) return
        synchronized(this) {
            if (started) return
            try {
                if (!Python.isStarted()) Python.start(AndroidPlatform(context))
                val home = context.filesDir.resolve("engine_home").absolutePath
                Thread(
                    {
                        try {
                            Python.getInstance()
                                .getModule("novel_downloader.apps.embedded.server")
                                .callAttr("start_server", home, PORT)
                            Log.i(TAG, "embedded engine ready on port $PORT")
                        } catch (t: Throwable) {
                            lastError = t.message
                            Log.e(TAG, "embedded server failed", t)
                        }
                    },
                    "embedded-engine",
                ).start()
                started = true
            } catch (t: Throwable) {
                lastError = t.message
                Log.e(TAG, "embedded engine bootstrap failed", t)
            }
        }
    }
}
