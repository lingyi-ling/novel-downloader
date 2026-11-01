package com.lingyi.noveldl.ui

import com.lingyi.noveldl.R
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Before
import org.junit.Test
import java.util.Locale
import java.util.TimeZone

/** JVM tests for the pure presentation helpers extracted from the composables. */
class FormattingTest {

    private lateinit var originalTimeZone: TimeZone

    @Before
    fun pinTimeZone() {
        originalTimeZone = TimeZone.getDefault()
        TimeZone.setDefault(TimeZone.getTimeZone("UTC"))
    }

    @After
    fun restoreTimeZone() {
        TimeZone.setDefault(originalTimeZone)
    }

    @Test
    fun knownStatusesMapToTheirLabels() {
        assertEquals(R.string.tasks_status_queued, taskStatusLabelRes("queued"))
        assertEquals(R.string.tasks_status_running, taskStatusLabelRes("running"))
        assertEquals(R.string.tasks_status_processing, taskStatusLabelRes("processing"))
        assertEquals(R.string.tasks_status_exporting, taskStatusLabelRes("exporting"))
        assertEquals(R.string.tasks_status_completed, taskStatusLabelRes("completed"))
        assertEquals(R.string.tasks_status_cancelled, taskStatusLabelRes("cancelled"))
        assertEquals(R.string.tasks_status_failed, taskStatusLabelRes("failed"))
    }

    @Test
    fun unknownStatusesFallBackToFailed() {
        assertEquals(R.string.tasks_status_failed, taskStatusLabelRes(""))
        assertEquals(R.string.tasks_status_failed, taskStatusLabelRes("RUNNING"))
        assertEquals(R.string.tasks_status_failed, taskStatusLabelRes("whatever"))
    }

    @Test
    fun progressFractionHandlesZeroAndClamping() {
        assertEquals(0f, progressFraction(0, 0), 0.0001f)
        assertEquals(0f, progressFraction(0, -5), 0.0001f)
        assertEquals(0f, progressFraction(3, 0), 0.0001f)
        assertEquals(0f, progressFraction(-1, 4), 0.0001f)
        assertEquals(0.75f, progressFraction(3, 4), 0.0001f)
        assertEquals(1f, progressFraction(5, 4), 0.0001f)
    }

    @Test
    fun progressPercentRoundsAndClamps() {
        assertEquals(0, progressPercent(0, 0))
        assertEquals(0, progressPercent(0, 10))
        assertEquals(33, progressPercent(1, 3))
        assertEquals(67, progressPercent(2, 3))
        assertEquals(50, progressPercent(1, 2))
        assertEquals(100, progressPercent(10, 10))
        assertEquals(100, progressPercent(20, 10))
    }

    @Test
    fun humanSizeFormatsBytesKilobytesAndMegabytes() {
        assertEquals("0 B", humanSize(0))
        assertEquals("512 B", humanSize(512))
        assertEquals("1023 B", humanSize(1023))
        assertEquals("1 KB", humanSize(1024))
        assertEquals("2 KB", humanSize(1536))
        assertEquals("1024 KB", humanSize(1024 * 1024 - 1))
        assertEquals("1.0 MB", humanSize(1024L * 1024))
        assertEquals("1.5 MB", humanSize(1024L * 1024 * 3 / 2))
        assertEquals("1024.0 MB", humanSize(1024L * 1024 * 1024 - 1))
    }

    @Test
    fun humanSizeFormatsGigabytesOnceTheMBWouldOverflow() {
        assertEquals("1.0 GB", humanSize(1024L * 1024 * 1024))
        assertEquals("1.5 GB", humanSize(1024L * 1024 * 1024 * 3 / 2))
        assertEquals("4.0 GB", humanSize(4L * 1024 * 1024 * 1024))
    }

    @Test
    fun timestampUsesLocalWallClockFormat() {
        assertEquals("2024-03-09 16:00", formatTimestamp(1710000000.0, Locale.US))
        assertEquals("1970-01-01 00:00", formatTimestamp(0.0, Locale.US))
    }
}