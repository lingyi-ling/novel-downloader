package com.lingyi.noveldl.data

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.ByteArrayInputStream
import java.io.ByteArrayOutputStream
import java.io.FileNotFoundException
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.util.concurrent.CancellationException

/**
 * JVM tests for the SAF streaming loop: byte counting, progress sampling, and
 * the permission/space/provider classification. No device, ContentResolver or
 * OkHttp connection is involved — the loop takes plain streams.
 */
class HistoryDownloadProgressTest {

    // ------------------------------------------------------------------
    // Progress model: the UI needs a real fraction only when the server
    // declared a length; otherwise it shows an indeterminate bar.
    // ------------------------------------------------------------------

    @Test
    fun fractionTracksTheServerTotal() {
        val quarter = DownloadProgress(bytesWritten = 250, totalBytes = 1000)

        assertEquals(0.25f, quarter.fraction!!, 0.0001f)
        assertEquals(25, quarter.percent)
    }

    @Test
    fun completionReachesOneHundredPercent() {
        val complete = DownloadProgress(bytesWritten = 1000, totalBytes = 1000)

        assertEquals(1f, complete.fraction!!, 0f)
        assertEquals(100, complete.percent)
    }

    @Test
    fun unknownTotalHasNoFractionSoTheBarStaysIndeterminate() {
        assertNull(DownloadProgress(bytesWritten = 500, totalBytes = null).fraction)
        assertNull(DownloadProgress(bytesWritten = 500, totalBytes = null).percent)
        // A zero or negative Content-Length is "unknown", not "already done".
        assertNull(DownloadProgress(bytesWritten = 500, totalBytes = 0).fraction)
        assertNull(DownloadProgress(bytesWritten = 500, totalBytes = -1).fraction)
    }

    @Test
    fun fractionClampsWhenMoreBytesArriveThanPromised() {
        val overshoot = DownloadProgress(bytesWritten = 2000, totalBytes = 1000)

        assertEquals(1f, overshoot.fraction!!, 0f)
        assertEquals(100, overshoot.percent)
    }

    // ------------------------------------------------------------------
    // Throttle: one sample per interval or per byte step, first one always.
    // ------------------------------------------------------------------

    @Test
    fun throttleEmitsTheFirstSampleThenRespectsByteAndTimeCaps() {
        val throttle = DownloadProgressThrottle(minIntervalNanos = 100, minBytes = 10)

        assertTrue("first sample", throttle.shouldEmit(0, 0))
        assertFalse("5 bytes / 50 ns is below both", throttle.shouldEmit(5, 50))
        assertTrue("10 bytes reached", throttle.shouldEmit(10, 60))
        assertFalse("5 more bytes, 40 ns later", throttle.shouldEmit(15, 100))
        assertTrue("100 ns since the last sample", throttle.shouldEmit(15, 160))
    }

    @Test
    fun throttleEmitsAgainOnceTheByteStepIsReached() {
        val throttle = DownloadProgressThrottle(minIntervalNanos = 1_000_000, minBytes = 4096)

        assertTrue(throttle.shouldEmit(0, 0))
        assertFalse(throttle.shouldEmit(4095, 10))
        assertTrue(throttle.shouldEmit(4096, 20))
    }

    // ------------------------------------------------------------------
    // Streaming loop: bytes are copied, counted, and reported.
    // ------------------------------------------------------------------

    @Test
    fun streamingCopiesTheBodyAndEndsAtTheDeclaredTotal() {
        val data = ByteArray(50_000) { (it % 251).toByte() }
        val output = ByteArrayOutputStream()
        val samples = mutableListOf<DownloadProgress>()

        val finalSample = streamToDocument(
            input = ByteArrayInputStream(data),
            output = output,
            totalBytes = data.size.toLong(),
            onProgress = samples::add,
            throttle = DownloadProgressThrottle(minIntervalNanos = 1, minBytes = 0),
        )

        assertArrayEquals(data, output.toByteArray())
        assertEquals(data.size.toLong(), finalSample.bytesWritten)
        assertEquals(data.size.toLong(), finalSample.totalBytes)
        assertEquals(1f, finalSample.fraction!!, 0f)
        // The first sample is the 0-byte start and progress never regresses.
        assertEquals(0L, samples.first().bytesWritten)
        assertTrue(
            "progress went backwards: $samples",
            samples.zipWithNext().all { (before, after) ->
                before.bytesWritten <= after.bytesWritten
            },
        )
        assertEquals(finalSample, samples.last())
    }

    @Test
    fun unknownContentLengthEndsWithTheWrittenSizeInsteadOfAStuckBar() {
        val data = ByteArray(10_000)
        val samples = mutableListOf<DownloadProgress>()

        val finalSample = streamToDocument(
            input = ByteArrayInputStream(data),
            output = ByteArrayOutputStream(),
            totalBytes = null,
            onProgress = samples::add,
        )

        assertEquals(10_000L, finalSample.bytesWritten)
        assertEquals(10_000L, finalSample.totalBytes)
        assertEquals(1f, finalSample.fraction!!, 0f)
        // Before the transfer finished there was no Content-Length to show.
        assertNull(samples.first().totalBytes)
    }

    @Test
    fun emptyBodyStillReportsAStartAndAnEnd() {
        val samples = mutableListOf<DownloadProgress>()

        val finalSample = streamToDocument(
            input = ByteArrayInputStream(ByteArray(0)),
            output = ByteArrayOutputStream(),
            totalBytes = null,
            onProgress = samples::add,
        )

        assertEquals(0L, finalSample.bytesWritten)
        assertEquals(2, samples.size)
    }

    // ------------------------------------------------------------------
    // Failure classification: what the UI names ("permission", "no space",
    // "provider", "network").
    // ------------------------------------------------------------------

    @Test
    fun aReadFailureIsANetworkFailure() {
        val failure = assertThrows(DownloadFailureException::class.java) {
            streamToDocument(
                input = FailingInputStream(IOException("unexpected end of stream")),
                output = ByteArrayOutputStream(),
                totalBytes = 1024,
                onProgress = {},
            )
        }

        assertEquals(DownloadFailureKind.Network, failure.kind)
    }

    @Test
    fun aFullVolumeIsClassifiedAsNoSpace() {
        val failure = assertThrows(DownloadFailureException::class.java) {
            streamToDocument(
                input = ByteArrayInputStream(ByteArray(1024)),
                output = FailingOutputStream(
                    IOException("write failed: ENOSPC (No space left on device)"),
                ),
                totalBytes = 1024,
                onProgress = {},
            )
        }

        assertEquals(DownloadFailureKind.NoSpace, failure.kind)
    }

    @Test
    fun aGrantRevokedDuringWriteIsClassifiedAsPermission() {
        val errors: List<Exception> = listOf(
            SecurityException("grant revoked"),
            IOException("write failed: EACCES (Permission denied)"),
            IOException("write failed: EPERM (Operation not permitted)"),
        )

        for (error in errors) {
            val failure = assertThrows(DownloadFailureException::class.java) {
                streamToDocument(
                    input = ByteArrayInputStream(ByteArray(64)),
                    output = FailingOutputStream(error),
                    totalBytes = 64,
                    onProgress = {},
                )
            }
            assertEquals("wrong kind for ${error.message}", DownloadFailureKind.Permission, failure.kind)
        }
    }

    @Test
    fun aFlushFailureIsClassifiedLikeAWriteFailure() {
        val output = object : ByteArrayOutputStream() {
            override fun flush() {
                throw IOException("Disk quota exceeded")
            }
        }

        val failure = assertThrows(DownloadFailureException::class.java) {
            streamToDocument(
                input = ByteArrayInputStream(ByteArray(8)),
                output = output,
                totalBytes = 8,
                onProgress = {},
            )
        }

        assertEquals(DownloadFailureKind.NoSpace, failure.kind)
    }

    @Test
    fun unrecognizedWriteProblemsAreProviderFailures() {
        assertEquals(
            DownloadFailureKind.Provider,
            classifyWriteFailure(IOException("generic I/O error")),
        )
        assertEquals(
            DownloadFailureKind.Provider,
            classifyWriteFailure(IllegalStateException("provider crashed")),
        )
        assertEquals(
            DownloadFailureKind.Provider,
            classifyWriteFailure(UnsupportedOperationException("no stream")),
        )
    }

    @Test
    fun spaceAndPermissionMarkersAreFoundInTheCauseChain() {
        assertEquals(
            DownloadFailureKind.NoSpace,
            classifyWriteFailure(
                IOException(
                    "wrapped",
                    FileNotFoundException("open failed: ENOSPC (No space left on device)"),
                ),
            ),
        )
        assertEquals(
            DownloadFailureKind.Permission,
            classifyWriteFailure(
                IOException("wrapped", IOException("EPERM (Operation not permitted)")),
            ),
        )
        assertEquals(
            DownloadFailureKind.NoSpace,
            classifyWriteFailure(IOException("There is not enough space on the disk")),
        )
    }

    @Test
    fun cancellationPropagatesInsteadOfBecomingAFailureResult() {
        val cancellation = CancellationException("navigated away")

        val thrown = assertThrows(CancellationException::class.java) {
            streamToDocument(
                input = ByteArrayInputStream(ByteArray(64)),
                output = ByteArrayOutputStream(),
                totalBytes = 64,
                onProgress = {},
                checkActive = { throw cancellation },
            )
        }

        assertSame(cancellation, thrown)
    }
}

/** Input stream that fails on the first read. */
private class FailingInputStream(private val error: IOException) : InputStream() {
    override fun read(): Int = throw error
}

/** Output stream that fails on the first write. */
private class FailingOutputStream(private val error: Exception) : OutputStream() {
    override fun write(b: Int) {
        throw error
    }

    override fun write(b: ByteArray, off: Int, len: Int) {
        throw error
    }
}