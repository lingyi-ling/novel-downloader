package com.lingyi.noveldl.notifications

import android.Manifest
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
 * checked on every post.
 */
object TaskNotifier {

    const val CHANNEL_ID = "task_events"

    /** Creates the channel; safe to call repeatedly. */
    fun ensureChannel(context: Context) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return
        val manager = context.getSystemService(NotificationManager::class.java) ?: return
        val channel = NotificationChannel(
            CHANNEL_ID,
            context.getString(R.string.notification_channel_tasks),
            NotificationManager.IMPORTANCE_DEFAULT,
        ).apply {
            description = context.getString(R.string.notification_channel_tasks_desc)
        }
        manager.createNotificationChannel(channel)
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
    fun notify(context: Context, event: TaskTerminalEvent): Boolean {
        if (!canPostNotifications(context)) return false
        ensureChannel(context)

        val appContext = context.applicationContext
        val completed = event.outcome == TaskOutcome.COMPLETED
        val title = appContext.getString(
            if (completed) R.string.notification_task_completed_title
            else R.string.notification_task_failed_title,
        )
        val text = appContext.getString(
            if (completed) R.string.notification_task_completed_text
            else R.string.notification_task_failed_text,
            event.task.title,
        )
        val intent = Intent(appContext, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        val pendingIntent = PendingIntent.getActivity(
            appContext,
            event.task.task_id.hashCode(),
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
            .setOnlyAlertOnce(true)
            .setPriority(NotificationCompat.PRIORITY_DEFAULT)
            .build()

        val notificationId = "task:${event.task.task_id}".hashCode()
        NotificationManagerCompat.from(appContext).notify(notificationId, notification)
        return true
    }
}