package com.lingyi.noveldl.data

import com.lingyi.noveldl.ui.humanSize
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.InputStream

/**
 * Pure JVM tests for the SAF progress model.
 *
 * Covers the contract the history row relies on: an indeterminate bar when the
 * server sent no Content-Length, clamping when the body outgrows the declared
 * length, the percent rounding rule, the byte-size formatting it renders, and
 * the default 150 ms / 256 KiB throttle including the guaranteed start and
 * final samples. No device, ContentResolver or OkHttp connection is involved.
 */
class DownloadProgressTest {

    // ------------------------------------------------------------------
    // Indeterminate totals
    // ------------------------------------------------------------------

    @Test
    fun aZeroOrNegativeTotalStaysIndeterminate() {
        for (total in listOf<Long?>(null, 0L, -1L)) {
            val sample = DownloadProgress(bytesWritten = 512, totalBytes = total)
            assertNull("fraction for total=$total", sample.fraction)
            assertNull("percent for total=$total", sample.percent)
        }
    }

    @Test
    fun aZeroLengthDownloadNeverShowsAPercentage() {
        val sample = DownloadProgress(bytesWritten = 0, totalBytes = 0)

        assertNull(sample.fraction)
        assertNull(sample.percent)
    }

    // ------------------------------------------------------------------
    // Clamping and rounding
    // ------------------------------------------------------------------

    @Test
    fun bytesBeyondTheDeclaredLengthClampToComplete() {
        val sample = DownloadProgress(bytesWritten = 2000, totalBytes = 1000)

        assertEquals(1f, sample.fraction!!, 0f)
        assertEquals(100, sample.percent)
    }

    @Test
    fun percentRoundsToTheNearestIntegerWithHalvesGoingUp() {
        // Below one percent still reads 0; the first half percent rounds up.
        assertEquals(0, DownloadProgress(bytesWritten = 0, totalBytes = 1000).percent)
        assertEquals(0, DownloadProgress(bytesWritten = 1, totalBytes = 1000).percent)
        assertEquals(1, DownloadProgress(bytesWritten = 1, totalBytes = 200).percent)

        // Thirds exercise the non-exact cases.
        assertEquals(33, DownloadProgress(bytesWritten = 1, totalBytes = 3).percent)
        assertEquals(67, DownloadProgress(bytesWritten = 2, totalBytes = 3).percent)

        // Eighths are exact binary fractions: 12.5 / 37.5 / 62.5 / 87.5.
        assertEquals(13, DownloadProgress(bytesWritten = 1, totalBytes = 8).percent)
        assertEquals(38, DownloadProgress(bytesWritten = 3, totalBytes = 8).percent)
        assertEquals(63, DownloadProgress(bytesWritten = 5, totalBytes = 8).percent)
        assertEquals(88, DownloadProgress(bytesWritten = 7, totalBytes = 8).percent)

        // 99.4 truncates to 99, 99.9 rounds to the clamped 100.
        assertEquals(99, DownloadProgress(bytesWritten = 994, totalBytes = 1000).percent)
        assertEquals(100, DownloadProgress(bytesWritten = 999, totalBytes = 1000).percent)
    }

    // ------------------------------------------------------------------
    // Byte formatting used by the progress row (humanSize)
    // ------------------------------------------------------------------

    @Test
    fun humanSizeSwitchesUnitsExactlyAtBinaryBoundaries() {
        assertEquals("0 B", humanSize(0))
        assertEquals("1 B", humanSize(1))
        assertEquals("1023 B", humanSize(1023))
        assertEquals("1 KB", humanSize(1024))
        assertEquals("1024 KB", humanSize(1024L * 1024 - 1))
        assertEquals("1.0 MB", humanSize(1024L * 1024))
        assertEquals("1024.0 MB", humanSize(1024L * 1024 * 1024 - 1))
        assertEquals("1.0 GB", humanSize(1024L * 1024 * 1024))
        assertEquals("3.0 GB", humanSize(3L * 1024 * 1024 * 1024))
    }

    // ------------------------------------------------------------------
    // Default throttle: 150 ms OR 256 KiB, whichever comes first
    // ------------------------------------------------------------------

    @Test
    fun theDefaultThrottleUses150MillisecondsOr256KiB() {
        val throttle = DownloadProgressThrottle()
        val kib256 = 256L * 1024L

        assertTrue("the 0-byte start always passes", throttle.shouldEmit(0, 0))
        assertFalse(
            "one byte short of 256 KiB and no time passed",
            throttle.shouldEmit(kib256 - 1, 0),
        )
        assertTrue("256 KiB written", throttle.shouldEmit(kib256, 0))
        assertFalse(
            "149 ms and no new bytes is still throttled",
            throttle.shouldEmit(kib256, 149_999_999),
        )
        assertTrue(
            "150 ms since the last sample",
            throttle.shouldEmit(kib256, 150_000_000),
        )
    }

    @Test
    fun timeAloneTriggersEvenWhenNoNewBytesArrived() {
        val throttle = DownloadProgressThrottle()

        assertTrue(throttle.shouldEmit(0, 0))
        assertFalse(throttle.shouldEmit(0, 149_999_999))
        assertTrue(throttle.shouldEmit(0, 150_000_000))
    }

    // ------------------------------------------------------------------
    // First and last samples are never throttled away
    // ------------------------------------------------------------------

    @Test
    fun streamAlwaysReportsTheFinalSampleEvenWhenTheThrottleWouldSuppressIt() {
        val data = ByteArray(10_000)
        val samples = mutableListOf<DownloadProgress>()

        val finalSample = streamToDocument(
            input = ByteArrayInputStream(data),
            output = ByteArrayOutputStream(),
            totalBytes = data.size.toLong(),
            onProgress = samples::add,
            // Frozen clock: below 256 KiB only a throttle emission would fire.
            nowNanos = { 0L },
        )

        assertEquals("start and final only", 2, samples.size)
        assertEquals(0L, samples.first().bytesWritten)
        assertEquals(finalSample, samples.last())
        assertEquals(1f, finalSample.fraction!!, 0f)
    }

    @Test
    fun a256KiBStepEmitsAnIntermediateSampleBeforeTheEnd() {
        val data = ByteArray(256 * 1024 + 8192)
        val samples = mutableListOf<DownloadProgress>()

        streamToDocument(
            // Fixed 8 KiB reads: the byte threshold is crossed at exactly
            // 256 KiB regardless of the internal buffer size.
            input = ChunkedInputStream(data, chunk = 8192),
            output = ByteArrayOutputStream(),
            totalBytes = data.size.toLong(),
            onProgress = samples::add,
            nowNanos = { 0L },
        )

        assertEquals(listOf(0L, 256L * 1024L, data.size.toLong()), samples.map { it.bytesWritten })
    }
}

/** Serves [data] in fixed-size chunks so sample boundaries stay deterministic. */
private class ChunkedInputStream(
    private val data: ByteArray,
    private val chunk: Int,
) : InputStream() {
    private var position = 0

    override fun read(): Int =
        if (position < data.size) data[position++].toInt() and 0xFF else -1

    override fun read(target: ByteArray, offset: Int, length: Int): Int {
        if (position >= data.size) return -1
        val count = minOf(length, chunk, data.size - position)
        System.arraycopy(data, position, target, offset, count)
        position += count
        return count
    }
}