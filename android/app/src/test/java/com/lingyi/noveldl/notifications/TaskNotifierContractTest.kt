package com.lingyi.noveldl.notifications

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * Pins the notification contract that plain JVM tests cannot execute: the
 * builder flags, the permission gate and the per-task id source. The app's
 * unit tests run without Robolectric, so `NotificationCompat` / `PendingIntent`
 * are not callable here; reading the implementation keeps the security-relevant
 * flags from silently regressing.
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
    fun channelCreationIsIdempotent() {
        assertTrue(source, source.contains("getNotificationChannel(CHANNEL_ID)"))
        assertTrue(source, source.contains("channelEnsured"))
        // The id is user-visible history: renaming it would strand the old
        // channel's settings.
        assertTrue(source, source.contains("CHANNEL_ID = \"task_events\""))
    }

    private fun read(name: String): String {
        val start = File(checkNotNull(System.getProperty("user.dir"))).absoluteFile
        var dir: File? = start
        while (dir != null) {
            listOf(
                File(dir, "src/main/java/com/lingyi/noveldl/notifications/$name"),
                File(dir, "app/src/main/java/com/lingyi/noveldl/notifications/$name"),
                File(dir, "android/app/src/main/java/com/lingyi/noveldl/notifications/$name"),
            ).firstOrNull { it.isFile }?.let { return it.readText() }
            dir = dir.parentFile
        }
        error("cannot locate $name from $start")
    }
}
