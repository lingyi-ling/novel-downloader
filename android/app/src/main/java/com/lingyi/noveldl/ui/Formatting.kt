package com.lingyi.noveldl.ui

import androidx.annotation.StringRes
import com.lingyi.noveldl.R
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import kotlin.math.roundToInt

/**
 * Pure presentation helpers shared by the screens.
 *
 * They live outside the composables so the formatting/derivation rules can be
 * unit tested on the JVM without a device.
 */

/** Maps an API task status to its label resource; unknown values fall back to "failed". */
@StringRes
fun taskStatusLabelRes(status: String): Int = when (status) {
    "queued" -> R.string.tasks_status_queued
    "running" -> R.string.tasks_status_running
    "processing" -> R.string.tasks_status_processing
    "exporting" -> R.string.tasks_status_exporting
    "completed" -> R.string.tasks_status_completed
    "cancelled" -> R.string.tasks_status_cancelled
    else -> R.string.tasks_status_failed
}

/** Completed fraction (0f..1f); 0 when the total is unknown or non-positive. */
fun progressFraction(done: Int, total: Int): Float =
    if (total <= 0) 0f else (done.toFloat() / total).coerceIn(0f, 1f)

/** Completed percentage (0..100). */
fun progressPercent(done: Int, total: Int): Int =
    (progressFraction(done, total) * 100).roundToInt().coerceIn(0, 100)

private const val KB = 1024.0
private const val MB = KB * 1024.0

/** Human readable byte size, e.g. 1536 -> "2 KB", 1572864 -> "1.5 MB". */
fun humanSize(bytes: Long): String = when {
    bytes >= MB.toLong() -> String.format(Locale.US, "%.1f MB", bytes / MB)
    bytes >= 1024 -> String.format(Locale.US, "%.0f KB", bytes / KB)
    else -> "$bytes B"
}

private const val TIME_PATTERN = "yyyy-MM-dd HH:mm"

/** Formats a unix timestamp (seconds) as local "yyyy-MM-dd HH:mm". */
fun formatTimestamp(seconds: Double, locale: Locale = Locale.getDefault()): String =
    SimpleDateFormat(TIME_PATTERN, locale).format(Date((seconds * 1000).toLong()))

/**
 * Joins the health endpoint's `status` and `version` into one label for the
 * persistent connection row on the settings screen, e.g. `ok · 1.2.0`.
 *
 * Either side may be missing (older servers omit `version`); two blanks mean
 * the fields never arrived, which reads as "unknown".
 */
fun healthSummary(status: String, version: String): String {
    val trimmedStatus = status.trim()
    val trimmedVersion = version.trim()
    return when {
        trimmedStatus.isEmpty() && trimmedVersion.isEmpty() -> "unknown"
        trimmedStatus.isEmpty() -> trimmedVersion
        trimmedVersion.isEmpty() -> trimmedStatus
        else -> "$trimmedStatus · $trimmedVersion"
    }
}