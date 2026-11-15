package com.lingyi.noveldl.notifications

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * Pins the notification contract that plain JVM tests cannot execute: the
 * builder flags, the group summary, the permission gate and the per-task id
 * source. The app's unit tests run without Robolectric, so `NotificationCompat`
 * / `PendingIntent` are not callable here; reading the implementation keeps the
 * security-relevant flags from silently regressing.
 */
class TaskNotifierContractTest {

    private val source: String by lazy { read("TaskNotifier.kt") }

    @Test
    fun pendingIntentIsImmutable() {
        assertTrue(source, source.contains("PendingIntent.FLAG_IMMUTABLE"))
    }

    @Test
    fun repeatedUpdatesDoNotAlertAgain() {
        assertTrue(source, source.contains(".setOnlyAlertOnce(true)"))
    }

    @Test
    fun postsAreGatedOnTheAndroid13Permission() {
        assertTrue(source, source.contains("Manifest.permission.POST_NOTIFICATIONS"))
        assertTrue(source, source.contains("PackageManager.PERMISSION_GRANTED"))
        assertTrue(source, source.contains("SecurityException"))
    }

    @Test
    fun idsComeFromTheStableAllocatorNotStringHashCode() {
        assertTrue(source, source.contains("NotificationIds.taskNotificationId("))
        assertTrue(source, source.contains("PendingIntent.getActivity("))
        assertFalse("request code must not use String.hashCode", source.contains("hashCode()"))
    }

    @Test
    fun thePendingIntentCarriesTheRealTaskId() {
        assertTrue(source, source.contains("TaskOpen.openTasksAction(notificationId)"))
        assertTrue(source, source.contains("putExtra(TaskOpen.EXTRA_TASK_ID"))
        // The fallback key only keeps the notification id stable for malformed
        // records; handing it to the activity would focus a task that cannot
        // exist. The trimmed real id is the only acceptable extra.
        assertTrue(source, source.contains("event.task.task_id.trim()"))
        assertFalse(source, source.contains("putExtra(TaskOpen.EXTRA_TASK_ID, taskKey)"))
    }

    @Test
    fun aForegroundTapArrivesThroughOnNewIntent() {
        // CLEAR_TOP alone recreates a standard-launch activity; SINGLE_TOP
        // keeps it alive so the tap is delivered to onNewIntent instead.
        assertTrue(source, source.contains("Intent.FLAG_ACTIVITY_SINGLE_TOP"))
    }

    @Test
    fun channelCreationIsIdempotent() {
        assertTrue(source, source.contains("getNotificationChannel(CHANNEL_ID)"))
        assertTrue(source, source.contains("channelEnsured"))
        // The id is user-visible history: renaming it would strand the old
        // channel's settings.
        assertTrue(source, source.contains("CHANNEL_ID = \"task_events\""))
    }

    // ---- group summary ----

    @Test
    fun taskNotificationsJoinAStableGroup() {
        assertTrue(source, source.contains("const val GROUP_KEY"))
        assertTrue(source, source.contains(".setGroup(GROUP_KEY)"))
        // The group key is persisted by the system; renaming it would orphan
        // every row that is already in the shade.
        assertTrue(source, source.contains("\"com.lingyi.noveldl.notifications.TASK_GROUP\""))
    }

    @Test
    fun theSummaryIsTheGroupHeaderAndCanBeCanceled() {
        assertTrue(source, source.contains(".setGroupSummary(true)"))
        assertTrue(source, source.contains("cancel(SUMMARY_ID)"))
        // Canceled as soon as no task is running, never while one still is.
        assertTrue(source, source.contains("counts.takeIf { it.showsSummary }"))
    }

    @Test
    fun theSummaryReportsRunningCompletedAndFailed() {
        assertTrue(source, source.contains("R.string.notification_group_counts"))
        assertTrue(source, source.contains("desired.running"))
        assertTrue(source, source.contains("desired.completed"))
        assertTrue(source, source.contains("desired.failed"))
    }

    @Test
    fun theSummaryOpensTheTaskPageWithoutFocusingACard() {
        assertTrue(source, source.contains("TaskOpen.openTasksAction(SUMMARY_ID)"))
        val summaryBlock = source.substringAfter("private fun publishSummary")
        assertFalse(
            "the summary must not carry a task id extra",
            summaryBlock.contains("TaskOpen.EXTRA_TASK_ID"),
        )
    }

    @Test
    fun theSummaryIsSilentSoChildrenKeepTheirSingleAlert() {
        assertTrue(
            source,
            source.contains(".setGroupAlertBehavior(NotificationCompat.GROUP_ALERT_CHILDREN)"),
        )
    }

    @Test
    fun summaryAndTaskIdsCanNeverCollide() {
        assertEquals(TaskNotifier.SUMMARY_ID, NotificationIds.SUMMARY_ID)
        assertTrue(NotificationIds.SUMMARY_ID >= 1)
        assertTrue(NotificationIds.SUMMARY_ID < NotificationIds.TASK_ID_FLOOR)
    }

    @Test
    fun thePollKeepsTheRunningCountReal() {
        assertTrue(source, source.contains("fun sync(context: Context, tasks: List<Task>)"))
        assertTrue(
            source,
            source.contains("groupState.onPoll(tasks)"),
        )
        // Integration: completion notifications alone can never report how
        // many downloads are still running, so the task poll must feed the
        // group as well.
        val screen = readRelative(
            "src/main/java/com/lingyi/noveldl/ui/tasks/TasksScreen.kt",
        )
        assertTrue(screen, screen.contains("TaskNotifier.sync(context, tasks)"))
    }

    @Test
    fun summaryStringsCarryAllThreeCountsInBothLocales() {
        for (locale in listOf("values", "values-en")) {
            val xml = readRelative("src/main/res/$locale/strings.xml")
            assertTrue("$locale: group title", xml.contains("name=\"notification_group_title\""))
            val line = xml.lineSequence()
                .firstOrNull { it.contains("name=\"notification_group_counts\"") }
            assertTrue("$locale: group counts", line != null)
            assertTrue("$locale: running placeholder", line!!.contains("%1\$d"))
            assertTrue("$locale: completed placeholder", line.contains("%2\$d"))
            assertTrue("$locale: failed placeholder", line.contains("%3\$d"))
        }
    }

    private fun read(name: String): String =
        readRelative("src/main/java/com/lingyi/noveldl/notifications/$name")

    private fun readRelative(relative: String): String {
        val start = File(checkNotNull(System.getProperty("user.dir"))).absoluteFile
        var dir: File? = start
        while (dir != null) {
            listOf(
                File(dir, relative),
                File(dir, "app/$relative"),
                File(dir, "android/app/$relative"),
            ).firstOrNull { it.isFile }?.let { return it.readText() }
            dir = dir.parentFile
        }
        error("cannot locate $relative from $start")
    }
}
