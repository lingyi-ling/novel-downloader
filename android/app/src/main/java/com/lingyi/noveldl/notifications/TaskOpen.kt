package com.lingyi.noveldl.notifications

import androidx.lifecycle.ViewModel
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * The intent contract between a task notification and `MainActivity`.
 *
 * `TaskNotifier` used to launch the activity without saying *which* task the
 * alert was about, so every tap only reopened the app. The task id now travels
 * as an extra, and the action keeps encoding the notification id: PendingIntent
 * identity includes the action, so two tasks still can never share (and
 * silently retarget) one PendingIntent.
 *
 * `MainActivity` is exported (it is the launcher entry point), so any installed
 * app can start it explicitly with an arbitrary action and extras. Nothing here
 * may therefore turn intent data into a privileged effect: the action is
 * matched against a strict allow-list and the id is bounded and character
 * checked, so a forged intent can at most open the local task list. It can
 * never read a file, start a download, or reach a cancel/share path.
 *
 * The decision logic here is Android-free so the JVM tests can exercise it
 * (`TaskOpenTest`, `TaskOpenValidationTest`); the activity only forwards
 * `action` / extra into it.
 */
object TaskOpen {

    /**
     * Extra carrying the real task id. A notification for a malformed record
     * (blank id) posts with an empty extra: the tasks tab opens, but nothing is
     * focused, because the id keyed by the notifier is only a fallback label.
     */
    const val EXTRA_TASK_ID: String = "com.lingyi.noveldl.extra.TASK_ID"

    /**
     * Longest task id accepted from an intent.
     *
     * Server ids are `uuid4().hex` (32 chars), so this leaves ample room for
     * other implementations while refusing an unbounded string that would be
     * carried in the activity's state. Anything longer is treated as "no id":
     * the task list still opens, it just cannot focus a card.
     */
    const val MAX_TASK_ID_CHARS: Int = 128

    private const val ACTION_PREFIX = "com.lingyi.noveldl.action.OPEN_TASKS:"

    /** `Int.MAX_VALUE` has 10 digits; a longer suffix cannot be an id. */
    private const val MAX_ACTION_ID_DIGITS = 10

    /** The per-task PendingIntent action; must stay unique per notification id. */
    fun openTasksAction(notificationId: Int): String = "$ACTION_PREFIX$notificationId"

    /**
     * Whether [action] was produced by [TaskNotifier].
     *
     * This is an allow-list, not a prefix test: the suffix must be the canonical
     * decimal form of a positive int, exactly as [openTasksAction] emits it. A
     * forged `OPEN_TASKS:abc`, `OPEN_TASKS:007`, `OPEN_TASKS:` or an overflow
     * value is rejected, so the accepted action space is exactly the one the
     * app can produce.
     */
    fun isOpenTasksAction(action: String?): Boolean {
        if (action == null || !action.startsWith(ACTION_PREFIX)) return false
        val suffix = action.substring(ACTION_PREFIX.length)
        if (suffix.isEmpty() || suffix.length > MAX_ACTION_ID_DIGITS) return false
        // Canonical form only: no sign, no zero padding.
        if (suffix.length > 1 && suffix[0] == '0') return false
        if (!suffix.all { it in '0'..'9' }) return false
        val id = suffix.toLong()
        return id in 1..Int.MAX_VALUE.toLong()
    }

    /**
     * Normalizes an extra: surrounding whitespace is not part of any task id,
     * and an id that could not be one is dropped.
     *
     * The id is only ever compared against the task list, so the checks are the
     * ones that keep the value inert: bounded length, no embedded whitespace,
     * and no control or bidi-format characters that would let a forged extra
     * smuggle invisible content into UI state.
     */
    fun taskId(raw: String?): String? {
        val trimmed = raw?.trim() ?: return null
        if (trimmed.isEmpty() || trimmed.length > MAX_TASK_ID_CHARS) return null
        val unusable = trimmed.any { char ->
            char.isWhitespace() ||
                Character.isSpaceChar(char) ||
                Character.isISOControl(char) ||
                Character.getType(char) == Character.FORMAT.toInt()
        }
        return trimmed.takeUnless { unusable }
    }

    /**
     * The request encoded by a delivered intent, or `null` when the intent did
     * not come from a task notification (launcher tap, update installer, ...).
     * A notification without a usable id still produces a request: the caller
     * opens the task list without focusing a card.
     */
    fun requestFrom(action: String?, rawTaskId: String?): TaskOpenIntent? =
        if (isOpenTasksAction(action)) TaskOpenIntent(taskId(rawTaskId)) else null

    /**
     * Whether `onCreate` should process its intent.
     *
     * A configuration change (non-null [savedStatePresent]) replays the very
     * same intent; the surviving [TaskOpenViewModel] already consumed it (or
     * still holds it), so processing again would scroll back to the old card.
     * A *fresh* ViewModel means a new process/activity lineage — including a
     * restoration after the process was killed while the notification was
     * pending — and must be processed even with saved state present.
     */
    fun shouldHandleDelivery(savedStatePresent: Boolean, freshViewModel: Boolean): Boolean =
        !savedStatePresent || freshViewModel
}

/** A notification-originated request to show the task list. */
data class TaskOpenIntent(val taskId: String?)

/**
 * One published navigation request plus a token.
 *
 * The token lets the consumer acknowledge exactly the request it handled: a
 * newer tap must not be cleared by the screen finishing an older one.
 */
data class TaskOpenRequest(val taskId: String?, val token: Int)

/**
 * Holds the latest notification request until the UI consumes it.
 *
 * Plain class (no lifecycle types) so the raise/consume rules run in JVM tests;
 * [TaskOpenViewModel] only adds the lifetime.
 */
class TaskOpenRequests {

    private val state = MutableStateFlow<TaskOpenRequest?>(null)

    val request: StateFlow<TaskOpenRequest?> = state.asStateFlow()

    private var lastToken = 0

    /** Publishes [intent] as the current request and returns it. */
    @Synchronized
    fun raise(intent: TaskOpenIntent): TaskOpenRequest {
        val published = TaskOpenRequest(taskId = intent.taskId, token = lastToken + 1)
        lastToken = published.token
        state.value = published
        return published
    }

    /** Clears the request only if it is still the one identified by [token]. */
    fun consume(token: Int) {
        val current = state.value
        if (current != null && current.token == token) {
            state.value = null
        }
    }
}

/**
 * Activity-scoped owner of the pending notification request.
 *
 * A ViewModel (not a field) because the activity is recreated on rotation: the
 * same intent is delivered again, and without the surviving consumed state the
 * tasks screen would jump back to the old notification's card.
 */
class TaskOpenViewModel : ViewModel() {

    private val requests = TaskOpenRequests()

    val request: StateFlow<TaskOpenRequest?> = requests.request

    private var sawAnyIntent = false

    /**
     * True until this activity lineage has processed an intent. Used to tell a
     * rotation (saved state present, same ViewModel) from a new process.
     */
    val isFresh: Boolean get() = !sawAnyIntent

    /** Records the intent the activity was launched/resumed with, if relevant. */
    fun onIntent(action: String?, rawTaskId: String?) {
        // Even an irrelevant (launcher MAIN) intent marks the lineage as
        // started, so a later rotation is not mistaken for a fresh launch.
        sawAnyIntent = true
        TaskOpen.requestFrom(action, rawTaskId)?.let(requests::raise)
    }

    /** Marks the request as handled so a recomposition does not replay it. */
    fun consume(token: Int) = requests.consume(token)
}
