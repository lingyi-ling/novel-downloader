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
    "interrupted" -> R.string.tasks_status_interrupted
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
private const val GB = MB * 1024.0

/**
 * Human readable byte size, e.g. 1536 -> "2 KB", 1572864 -> "1.5 MB",
 * 3221225472 -> "3.0 GB". `Locale.US` keeps the decimal separator stable
 * across the app's locales (and across the JVM tests).
 */
fun humanSize(bytes: Long): String = when {
    bytes >= GB.toLong() -> String.format(Locale.US, "%.1f GB", bytes / GB)
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
 * the fields never arrived and [unknown] is shown. The caller passes the
 * localized word so the line does not mix an English fallback into a
 * translated UI.
 */
fun healthSummary(status: String, version: String, unknown: String): String {
    val trimmedStatus = status.trim()
    val trimmedVersion = version.trim()
    return when {
        trimmedStatus.isEmpty() && trimmedVersion.isEmpty() -> unknown
        trimmedStatus.isEmpty() -> trimmedVersion
        trimmedVersion.isEmpty() -> trimmedStatus
        else -> "$trimmedStatus · $trimmedVersion"
    }
}

/**
 * The resource for an empty list area, or `null` when an error banner already
 * explains the screen (the caller renders its retry entry next to it).
 *
 * Loading wins over "empty": before the first response the list is empty for
 * an uninteresting reason, and "nothing here yet" would be a false statement
 * (notably while the 2 s task poll is still in flight).
 */
@StringRes
fun listPlaceholderRes(
    loading: Boolean,
    hasError: Boolean,
    @StringRes emptyRes: Int,
): Int? = when {
    hasError -> null
    loading -> R.string.state_loading
    else -> emptyRes
}

/**
 * The resource for the search result area, or `null` when there is nothing
 * truthful to say: a fresh screen has not searched yet, and during a request
 * or after an error the state is already shown elsewhere.
 */
@StringRes
fun searchPlaceholderRes(
    hasSearched: Boolean,
    loading: Boolean,
    hasError: Boolean,
): Int? = when {
    loading || hasError -> null
    hasSearched -> R.string.search_no_results
    else -> null
}