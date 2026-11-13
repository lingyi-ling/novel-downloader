package com.lingyi.noveldl.notifications

import android.Manifest
import android.annotation.SuppressLint
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import com.lingyi.noveldl.MainActivity
import com.lingyi.noveldl.R
import com.lingyi.noveldl.ui.tasks.TaskOutcome
import com.lingyi.noveldl.ui.tasks.TaskTerminalEvent

/**
 * Local notifications for task state changes.
 *
 * Channel metadata comes from `strings.xml` so the name is localized. On
 * Android 13+ notifications are only posted after POST_NOTIFICATIONS was
 * granted; notification permission can be revoked at any time, so it is
 * checked on every post and a revoke racing the check is caught as well.
 *
 * Each task owns one notification id and one PendingIntent request code
 * ([NotificationIds]): re-posting updates the existing entry silently instead
 * of stacking duplicates, and two tasks can never overwrite each other.
 *
 * Tapping the notification opens [MainActivity] with the task id in
 * [TaskOpen.EXTRA_TASK_ID], so the app can scroll to that task instead of
 * merely coming to the foreground.
 */
object TaskNotifier {

    const val CHANNEL_ID = "task_events"

    private val channelLock = Any()

    @Volatile
    private var channelEnsured = false

    /**
     * Creates or refreshes the channel; safe to call repeatedly.
     *
     * The first call wins the race through [channelEnsured]; when the channel
     * already exists only its (localized) name and description are refreshed,
     * so user-adjusted importance, sound and vibration are preserved. This
     * makes repeated `onCreate` calls a no-op instead of resetting settings.
     */
    fun ensureChannel(context: Context) {
        if (channelEnsured) return
        synchronized(channelLock) {
            if (channelEnsured) return
            val manager = context.getSystemService(NotificationManager::class.java) ?: return
            val name = context.getString(R.string.notification_channel_tasks)
            val description = context.getString(R.string.notification_channel_tasks_desc)
            val existing = manager.getNotificationChannel(CHANNEL_ID)
            if (existing == null) {
                val channel = NotificationChannel(
                    CHANNEL_ID,
                    name,
                    NotificationManager.IMPORTANCE_DEFAULT,
                ).apply { this.description = description }
                manager.createNotificationChannel(channel)
            } else {
                existing.name = name
                existing.description = description
                manager.createNotificationChannel(existing)
            }
            channelEnsured = true
        }
    }

    /** Whether the app is currently allowed to post notifications. */
    fun canPostNotifications(context: Context): Boolean {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(
                context,
                Manifest.permission.POST_NOTIFICATIONS,
            ) != PackageManager.PERMISSION_GRANTED
        ) {
            return false
        }
        return NotificationManagerCompat.from(context).areNotificationsEnabled()
    }

    /** Posts the notification for [event]; returns false when it was skipped. */
    // canPostNotifications() is checked right below (POST_NOTIFICATIONS on
    // 13+ plus the app-level toggle), which lint cannot follow into a helper;
    // the try/catch additionally covers a revoke racing the check.
    @SuppressLint("MissingPermission")
    fun notify(context: Context, event: TaskTerminalEvent): Boolean {
        if (!canPostNotifications(context)) return false

        val appContext = context.applicationContext
        val completed = event.outcome == TaskOutcome.COMPLETED
        val title = appContext.getString(
            if (completed) R.string.notification_task_completed_title
            else R.string.notification_task_failed_title,
        )
        val fallbackTitle = appContext.getString(R.string.notification_task_unknown_title)
        val taskTitle = NotificationText.taskTitle(event.task.title, fallbackTitle)
        val text = appContext.getString(
            if (completed) R.string.notification_task_completed_text
            else R.string.notification_task_failed_text,
            taskTitle,
        )

        // A blank task_id means the server returned a malformed record; falling
        // back to site+title still yields a stable key instead of collapsing
        // every such task onto one notification.
        val taskKey = event.task.task_id.trim().ifBlank {
            "fallback:${event.task.site}:${event.task.title}"
        }
        val notificationId = NotificationIds.taskNotificationId(taskKey)

        val intent = Intent(appContext, MainActivity::class.java).apply {
            // Unique per task: PendingIntent identity includes the action, and
            // a shared action would let a later update retarget an older PI.
            action = TaskOpen.openTasksAction(notificationId)
            // The real task id, never the fallback key above: only a real id
            // can be located in the task list.
            putExtra(TaskOpen.EXTRA_TASK_ID, event.task.task_id.trim())
            // SINGLE_TOP keeps a foreground activity alive, so the tap arrives
            // through onNewIntent and the 2 s poll/composition state survives.
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or
                Intent.FLAG_ACTIVITY_CLEAR_TOP or
                Intent.FLAG_ACTIVITY_SINGLE_TOP
        }
        val pendingIntent = PendingIntent.getActivity(
            appContext,
            notificationId,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val notification = NotificationCompat.Builder(appContext, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setContentIntent(pendingIntent)
            .setAutoCancel(true)
            // Re-posts for the same task update in place without buzzing again.
            .setOnlyAlertOnce(true)
            .setPriority(NotificationCompat.PRIORITY_DEFAULT)
            .build()

        return try {
            ensureChannel(appContext)
            NotificationManagerCompat.from(appContext).notify(notificationId, notification)
            true
        } catch (_: SecurityException) {
            false
        }
    }
}
