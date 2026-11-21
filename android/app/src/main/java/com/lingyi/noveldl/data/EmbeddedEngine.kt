package com.lingyi.noveldl.data

import android.content.Context
import android.util.Log
import androidx.annotation.VisibleForTesting
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.security.SecureRandom
import java.util.Base64

/**
 * Environment variable the Python engine reads its bearer token from.
 *
 * Kept in sync with `novel_downloader.apps.embedded.server.TOKEN_ENV_VAR`; a
 * Python test pins the two literals together.
 */
internal const val EMBEDDED_TOKEN_ENV_VAR = "NOVEL_DOWNLOADER_EMBEDDED_TOKEN"

/** Entropy of the per-process token (bytes, before base64url encoding). */
internal const val EMBEDDED_TOKEN_BYTES = 32

/**
 * Generates a fresh bearer token: [EMBEDDED_TOKEN_BYTES] CSPRNG bytes as
 * unpadded base64url (`[A-Za-z0-9_-]{43}` for the default size).
 *
 * base64url keeps the value usable as an HTTP header value without escaping
 * and as a plain environment string.
 */
internal fun generateEmbeddedToken(random: SecureRandom = SecureRandom()): String {
    val bytes = ByteArray(EMBEDDED_TOKEN_BYTES)
    random.nextBytes(bytes)
    return Base64.getUrlEncoder().withoutPadding().encodeToString(bytes)
}

/**
 * The environment entries installed into the Python interpreter for [token].
 *
 * Pure so the hand-off can be asserted on the JVM: an empty token yields no
 * entry (the engine then keeps its unauthenticated desktop/test behaviour).
 * The token is passed as an environment variable rather than a
 * `start_server` argument because an argument would land in the interpreter's
 * visible call signature/log lines, whereas the environment block is private
 * to the process (and `os.environ` here is only ever set in memory, never
 * written to disk).
 */
internal fun embeddedTokenEnvironment(token: String): Map<String, String> =
    if (token.isEmpty()) emptyMap() else mapOf(EMBEDDED_TOKEN_ENV_VAR to token)

/**
 * Boots the embedded Python engine: the full novel_downloader service layer
 * serving the REST contract on 127.0.0.1:[PORT] inside the app process.
 *
 * `start_server` is idempotent and binds loopback only; a daemon thread is
 * left running for the process lifetime.
 *
 * ## Loopback is not a trust boundary
 *
 * Any app on the device can reach `127.0.0.1`, so the engine is not left open:
 * a per-process random [token] is generated here, installed into the Python
 * interpreter's environment, and required by every `/api/v1` and `/downloads`
 * request. It lives in process memory only — never in a log line, a
 * preference file or the argument list — and is regenerated on every process
 * start (so it dies with the app that owns it).
 */
object EmbeddedEngine {
    private const val TAG = "EmbeddedEngine"
    const val PORT = 8081
    const val BASE_URL = "http://127.0.0.1:$PORT/"

    /**
     * Directory under `filesDir` used as the engine's writable home. The
     * engine's default `general.output_dir` (`./downloads`) resolves below
     * it, which is where the music engine also writes `<stem>.lrc`.
     */
    const val HOME_DIRECTORY = "engine_home"

    /**
     * The bearer token the engine expects from this process.
     *
     * Generated eagerly (not on the bootstrap thread) because `Api.configure`
     * can run before the engine finished starting and must already know which
     * credential to attach. Empty only after [resetForTest] was called without
     * a subsequent generation.
     */
    @Volatile
    var token: String = generateEmbeddedToken()
        private set

    @Volatile
    private var started = false

    @Volatile
    private var ready = false

    @Volatile
    var lastError: String? = null
        private set

    /**
     * The thread running the bootstrap. Kept so unit tests can wait for it to
     * finish before resetting the state: a test that resets while the previous
     * bootstrap is still parked would otherwise see `ready` flip back to true
     * (or a stale `lastError`) in the next test.
     */
    @Volatile
    private var worker: Thread? = null

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
        val home = appContext.filesDir.resolve(HOME_DIRECTORY).absolutePath
        ensureStarted(home) {
            try {
                if (!Python.isStarted()) Python.start(AndroidPlatform(appContext))
                // Hand the credential over in memory, before the server binds
                // its port: the interpreter is in this process, so nothing is
                // written to disk and no argv is visible to other apps.
                // `os.environ` (not `os.putenv`) is used because the Python
                // side reads `os.environ.get(...)`.
                val python = Python.getInstance()
                val environ = python.getModule("os").get("environ")
                    ?: error("os.environ is unavailable in the embedded interpreter")
                embeddedTokenEnvironment(token).forEach { (name, value) ->
                    environ.put(name, value)
                }
                python
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
            val thread = Thread(
                {
                    try {
                        bootstrap()
                        ready = true
                    } catch (t: Throwable) {
                        lastError = t.message ?: t.javaClass.simpleName
                    }
                },
                "embedded-engine",
            )
            worker = thread
            thread.start()
        }
    }

    /** Restores the never-started state; unit tests only. */
    @VisibleForTesting
    internal fun resetForTest() {
        synchronized(this) {
            started = false
            ready = false
            lastError = null
            token = generateEmbeddedToken()
        }
    }

    /**
     * Waits for the scheduled bootstrap to finish; unit tests only.
     *
     * Call this before [resetForTest] so a still-running bootstrap cannot
     * write `ready`/`lastError` after the reset and leak state into the next
     * test. Returns false if the thread is still alive after [timeoutMs].
     */
    @VisibleForTesting
    internal fun awaitBootstrapForTest(timeoutMs: Long = 5_000): Boolean {
        val thread = worker ?: return true
        thread.join(timeoutMs)
        return !thread.isAlive
    }
}
