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
import com.lingyi.noveldl.data.Task
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
 *
 * Concurrent downloads additionally share a notification group under
 * [GROUP_KEY]. The per-task notifications stay exactly as they were (same id,
 * same action, same tap target) and become the group's expandable children;
 * a single [SUMMARY_ID] header aggregates them. The header exists only while
 * results and running work overlap, and is canceled when the last task ends,
 * so the shade shows one line instead of one row per finished download.
 */
object TaskNotifier {

    const val CHANNEL_ID = "task_events"

    /** Group key for concurrent task notifications; stable across releases. */
    const val GROUP_KEY = "com.lingyi.noveldl.notifications.TASK_GROUP"

    /**
     * Summary notification id; below [NotificationIds.TASK_ID_FLOOR] so no
     * task hash can collide with it. Also used as the summary's request code.
     */
    const val SUMMARY_ID: Int = NotificationIds.SUMMARY_ID

    private val channelLock = Any()

    @Volatile
    private var channelEnsured = false

    private val groupState = NotificationGroupState()

    /**
     * Counts last applied to the shade. `null` means no summary is showing.
     * Keeping it lets a 2 s poll avoid re-posting an unchanged header, and
     * keeps a header the user swiped away gone until its counts actually
     * change.
     */
    private var publishedCounts: NotificationGroupCounts? = null

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

    /**
     * Feeds one successful task poll into the group and republishes the
     * summary. This is what makes the running count real: per-task
     * notifications only fire on completion, so without the poll the notifier
     * would never know that other downloads are still in flight.
     *
     * Failures are ignored: a stale poll simply leaves the last counts in
     * place rather than dropping the summary.
     */
    @SuppressLint("MissingPermission")
    @Synchronized
    fun sync(context: Context, tasks: List<Task>) {
        if (!canPostNotifications(context)) {
            // Notifications are off (or were revoked): forget what the shade
            // showed, so granting the permission again reposts a fresh header
            // instead of assuming the old one still exists.
            publishedCounts = null
            return
        }
        val appContext = context.applicationContext
        val counts = groupState.onPoll(tasks)
        publishSummary(appContext, counts)
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
            .setGroup(GROUP_KEY)
            // Same behavior on every member: the child alerts once, the
            // summary never does, so a group update cannot double-buzz.
            .setGroupAlertBehavior(NotificationCompat.GROUP_ALERT_CHILDREN)
            .setAutoCancel(true)
            // Re-posts for the same task update in place without buzzing again.
            .setOnlyAlertOnce(true)
            .setPriority(NotificationCompat.PRIORITY_DEFAULT)
            .build()

        return try {
            ensureChannel(appContext)
            NotificationManagerCompat.from(appContext).notify(notificationId, notification)
            publishSummary(appContext, groupState.onTerminal(event.task, event.outcome))
            true
        } catch (_: SecurityException) {
            false
        }
    }

    /**
     * Posts, updates or cancels the group header for [counts].
     *
     * `GROUP_ALERT_CHILDREN` keeps the header silent (children still buzz once
     * each), and `setOnlyAlertOnce` prevents every 2 s poll from re-alerting.
     * `setAutoCancel(false)` is deliberate: tapping the header opens the task
     * page but must not collapse the group that is still aggregating rows.
     *
     * The compat builder applies all group flags below API 26 as no-ops, and
     * the app's minSdk is 26, so no version guard is required.
     */
    @SuppressLint("MissingPermission")
    @Synchronized
    private fun publishSummary(context: Context, counts: NotificationGroupCounts) {
        val desired = counts.takeIf { it.showsSummary }

        if (desired == null) {
            // All tasks are finished (or only one task ever existed): drop the
            // aggregate so a stale "0 running" header cannot linger. This also
            // clears a header left in the shade by a previous process, whose
            // in-memory counts died with it. The individual notifications stay
            // available and expandable in the shade.
            NotificationManagerCompat.from(context).cancel(SUMMARY_ID)
            publishedCounts = null
            return
        }
        if (desired == publishedCounts) return

        val title = context.getString(R.string.notification_group_title)
        val text = context.getString(
            R.string.notification_group_counts,
            desired.running,
            desired.completed,
            desired.failed,
        )
        // No task id extra: the tap opens the Tasks page without focusing a
        // card, which `App.kt` consumes as a null-id request.
        val intent = Intent(context, MainActivity::class.java).apply {
            action = TaskOpen.openTasksAction(SUMMARY_ID)
            flags = Intent.FLAG_ACTIVITY_NEW_TASK or
                Intent.FLAG_ACTIVITY_CLEAR_TOP or
                Intent.FLAG_ACTIVITY_SINGLE_TOP
        }
        val pendingIntent = PendingIntent.getActivity(
            context,
            SUMMARY_ID,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val notification = NotificationCompat.Builder(context, CHANNEL_ID)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle(title)
            .setContentText(text)
            .setStyle(NotificationCompat.BigTextStyle().bigText(text))
            .setContentIntent(pendingIntent)
            .setGroup(GROUP_KEY)
            .setGroupSummary(true)
            .setGroupAlertBehavior(NotificationCompat.GROUP_ALERT_CHILDREN)
            .setOnlyAlertOnce(true)
            .setAutoCancel(false)
            .setPriority(NotificationCompat.PRIORITY_DEFAULT)
            .setShowWhen(false)
            .build()

        try {
            ensureChannel(context)
            NotificationManagerCompat.from(context).notify(SUMMARY_ID, notification)
            publishedCounts = desired
        } catch (_: SecurityException) {
            // Permission revoked between the gate and the post: leave the
            // remembered state untouched so the next poll retries.
        }
    }
}
