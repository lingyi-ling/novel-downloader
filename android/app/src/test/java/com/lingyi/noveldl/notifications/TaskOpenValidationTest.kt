package com.lingyi.noveldl.notifications

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * Security-focused tests for the notification → activity intent contract.
 *
 * `MainActivity` is exported (it is the launcher entry point), so a malicious
 * installed app can start it explicitly with `component = com.lingyi.noveldl/
 * .MainActivity` plus any action and extras it likes. Everything reachable from
 * that intent must therefore be inert: the action is checked against the exact
 * set this app emits, and the task id is bounded and character-checked before it
 * reaches UI state.
 *
 * The pure rules run directly; the PendingIntent flags and the receiver export
 * flag cannot be executed without Robolectric, so they are pinned by reading the
 * implementation — the same approach as [TaskNotifierContractTest].
 */
class TaskOpenValidationTest {

    // ---- action allow-list ----

    @Test
    fun onlyCanonicalNotificationActionsAreAccepted() {
        assertTrue(TaskOpen.isOpenTasksAction(TaskOpen.openTasksAction(1)))
        assertTrue(TaskOpen.isOpenTasksAction(TaskOpen.openTasksAction(Int.MAX_VALUE)))
        assertTrue(TaskOpen.isOpenTasksAction(TaskOpen.openTasksAction(TaskNotifier.SUMMARY_ID)))
    }

    @Test
    fun forgedOrMalformedActionSuffixesAreRejected() {
        val prefix = TaskOpen.openTasksAction(0).substringBeforeLast(':') + ":"

        // A bare prefix, an empty suffix and a non-numeric suffix are not
        // actions this app can produce.
        assertFalse(TaskOpen.isOpenTasksAction(prefix))
        assertFalse(TaskOpen.isOpenTasksAction(prefix + "abc"))
        assertFalse(TaskOpen.isOpenTasksAction(prefix + "1x"))
        assertFalse(TaskOpen.isOpenTasksAction(prefix + "1.0"))
        // Canonical decimal only: no sign, no zero padding, no whitespace.
        assertFalse(TaskOpen.isOpenTasksAction(prefix + "-1"))
        assertFalse(TaskOpen.isOpenTasksAction(prefix + "+1"))
        assertFalse(TaskOpen.isOpenTasksAction(prefix + "007"))
        assertFalse(TaskOpen.isOpenTasksAction(prefix + "0"))
        assertFalse(TaskOpen.isOpenTasksAction(prefix + " 1"))
        assertFalse(TaskOpen.isOpenTasksAction(prefix + "1 "))
        // Overflows and over-long suffixes must not wrap into a valid id.
        assertFalse(TaskOpen.isOpenTasksAction(prefix + "2147483648"))
        assertFalse(TaskOpen.isOpenTasksAction(prefix + "9".repeat(40)))
    }

    @Test
    fun foreignActionsNeverBecomeATaskOpenRequest() {
        assertFalse(TaskOpen.isOpenTasksAction(null))
        assertFalse(TaskOpen.isOpenTasksAction(""))
        assertFalse(TaskOpen.isOpenTasksAction("android.intent.action.MAIN"))
        // A look-alike namespace is a different string and must not match.
        assertFalse(TaskOpen.isOpenTasksAction("com.lingyi.noveldl.action.OPEN_TASKS2:1"))
        assertFalse(TaskOpen.isOpenTasksAction("com.evil.action.OPEN_TASKS:1"))
        assertNull(TaskOpen.requestFrom("com.evil.action.OPEN_TASKS:1", "t1"))
        assertNull(TaskOpen.requestFrom("android.intent.action.MAIN", "t1"))
    }

    // ---- task id validation ----

    @Test
    fun aRealServerTaskIdSurvivesNormalization() {
        // The server generates uuid4().hex, so this is the shape to accept.
        val uuid = "0123456789abcdef0123456789abcdef"
        assertEquals(uuid, TaskOpen.taskId(uuid))
        assertEquals(uuid, TaskOpen.taskId("  $uuid\n"))
        assertEquals("t1", TaskOpen.taskId("t1"))
    }

    @Test
    fun oversizedIdsAreDropped() {
        val atLimit = "a".repeat(TaskOpen.MAX_TASK_ID_CHARS)
        assertEquals(atLimit, TaskOpen.taskId(atLimit))
        assertNull(TaskOpen.taskId("a".repeat(TaskOpen.MAX_TASK_ID_CHARS + 1)))
        // A forged extra must not be able to carry an unbounded string into the
        // activity's saved state.
        assertNull(TaskOpen.taskId("a".repeat(1_000_000)))
    }

    @Test
    fun controlAndBidiCharactersAreDropped() {
        assertNull(TaskOpen.taskId("t\u00001"))
        assertNull(TaskOpen.taskId("t\t1"))
        assertNull(TaskOpen.taskId("t\n1"))
        assertNull(TaskOpen.taskId("t\u202E1"))
        assertNull(TaskOpen.taskId("\u200Bt1"))
        assertNull(TaskOpen.taskId("t 1"))
        assertNull(TaskOpen.taskId("\u0000"))
    }

    @Test
    fun blankIdsStayBlank() {
        assertNull(TaskOpen.taskId(null))
        assertNull(TaskOpen.taskId(""))
        assertNull(TaskOpen.taskId(" \t\n\u00A0"))
    }

    @Test
    fun aForgedIdStillOnlyOpensTheTaskList() {
        // The id is compared against the list and nothing else; an unknown id
        // is reported as missing, never resolved into a file or a download.
        val request = TaskOpen.requestFrom(TaskOpen.openTasksAction(1), "../../etc/passwd")
        assertEquals("../../etc/passwd", request?.taskId)
        assertNull(TaskOpen.requestFrom(TaskOpen.openTasksAction(1), "bad\u0000id")?.taskId)
    }

    // ---- PendingIntent construction ----

    @Test
    fun everyPendingIntentIsImmutableAndUpdatable() {
        val notifier = read("notifications/TaskNotifier.kt")
        val calls = Regex("PendingIntent\\.getActivity\\(").findAll(notifier).count()
        assertEquals(
            "both the per-task and the summary intent must be built",
            calls.toLong(),
            2L,
        )
        assertEquals(
            "every PendingIntent must be immutable so its extras cannot be filled in by a caller",
            calls.toLong(),
            Regex("PendingIntent\\.FLAG_IMMUTABLE").findAll(notifier).count().toLong(),
        )
        assertEquals(
            "re-posting a task must update its intent instead of creating a second one",
            calls.toLong(),
            Regex("PendingIntent\\.FLAG_UPDATE_CURRENT").findAll(notifier).count().toLong(),
        )
        // A mutable PendingIntent would let the receiver of the notification
        // fill in the activity intent.
        assertFalse(notifier.contains("FLAG_MUTABLE"))
    }

    @Test
    fun requestCodesAreThePerTaskNotificationIds() {
        val notifier = read("notifications/TaskNotifier.kt")
        // The per-task intent's request code is the notification id, and the
        // summary uses its own reserved id: neither can retarget the other.
        assertTrue(
            notifier,
            Regex("""PendingIntent\.getActivity\(\s*appContext,\s*notificationId,""")
                .containsMatchIn(notifier),
        )
        assertTrue(
            notifier,
            Regex("""PendingIntent\.getActivity\(\s*context,\s*SUMMARY_ID,""")
                .containsMatchIn(notifier),
        )
        assertTrue(NotificationIds.SUMMARY_ID < NotificationIds.TASK_ID_FLOOR)
    }

    @Test
    fun theActivityIsNotStartedWithAnExportedHelperIntent() {
        val notifier = read("notifications/TaskNotifier.kt")
        // Explicit component only: no implicit action that another app could
        // intercept by registering a matching filter.
        assertTrue(notifier.contains("Intent(appContext, MainActivity::class.java)"))
        assertTrue(notifier.contains("Intent(context, MainActivity::class.java)"))
        assertFalse(notifier.contains("Intent(\""))
        assertFalse(notifier.contains("setPackage("))
    }

    // ---- broadcast receiver ----

    @Test
    fun theDownloadReceiverIsNotExported() {
        val history = read("ui/history/HistoryScreen.kt")
        assertTrue(
            history,
            history.contains("ContextCompat.RECEIVER_NOT_EXPORTED"),
        )
        assertFalse(
            "an exported receiver would let another app spoof ACTION_DOWNLOAD_COMPLETE",
            history.contains("ContextCompat.RECEIVER_EXPORTED"),
        )
        // The only registration is the ContextCompat overload that takes an
        // explicit export flag; the two-argument form defaults to exported.
        val registrations = Regex("(\\w+)\\.registerReceiver\\(")
            .findAll(codeOnly(history))
            .map { it.groupValues[1] }
            .toList()
        assertEquals(listOf("ContextCompat"), registrations)
        // Only the system's download id is read, and only for ids this screen
        // enqueued.
        assertTrue(history.contains("DownloadManager.EXTRA_DOWNLOAD_ID"))
        assertTrue(history.contains("pendingDownloads.remove(id) ?: return"))
    }

    @Test
    fun theActivityReadsOnlyTheNotificationExtra() {
        val activity = read("MainActivity.kt")
        assertTrue(activity.contains("getStringExtra(TaskOpen.EXTRA_TASK_ID)"))
        val code = codeOnly(activity)
        assertFalse(code.contains("getData()"))
        assertFalse(code.contains("clipData"))
        assertFalse(code.contains("getParcelableExtra"))
        assertFalse(code.contains("getSerializableExtra"))
        assertFalse(code.contains("openInputStream"))
    }

    /** Strips Kotlin comments: a comment must not satisfy or break a check. */
    private fun codeOnly(source: String): String =
        source
            .replace(Regex("/\\*.*?\\*/", RegexOption.DOT_MATCHES_ALL), "")
            .replace(Regex("//[^\\n]*"), "")

    private fun read(relative: String): String {
        val start = File(checkNotNull(System.getProperty("user.dir"))).absoluteFile
        var dir: File? = start
        while (dir != null) {
            listOf(
                File(dir, "src/main/java/com/lingyi/noveldl/$relative"),
                File(dir, "app/src/main/java/com/lingyi/noveldl/$relative"),
                File(dir, "android/app/src/main/java/com/lingyi/noveldl/$relative"),
            ).firstOrNull { it.isFile }?.let { return it.readText() }
            dir = dir.parentFile
        }
        error("cannot locate $relative from $start")
    }
}
