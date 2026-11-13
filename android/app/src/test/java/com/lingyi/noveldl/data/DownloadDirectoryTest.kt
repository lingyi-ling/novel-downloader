package com.lingyi.noveldl.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** JVM tests for the custom SAF download-directory rules. */
class DownloadDirectoryTest {

    private val treeUri =
        "content://com.android.externalstorage.documents/tree/primary%3ADocuments%2FNovelBooks"

    @Test
    fun blankOrMissingTreeFallsBackToSystemDownloads() {
        assertEquals(DownloadTarget.SystemDownloads, resolveDownloadTarget(null))
        assertEquals(DownloadTarget.SystemDownloads, resolveDownloadTarget(""))
        assertEquals(DownloadTarget.SystemDownloads, resolveDownloadTarget("   "))
    }

    @Test
    fun nonContentUrisAreNotCustomTrees() {
        assertEquals(
            DownloadTarget.SystemDownloads,
            resolveDownloadTarget("file:///storage/emulated/0/Books"),
        )
        assertEquals(
            DownloadTarget.SystemDownloads,
            resolveDownloadTarget("http://host/tree/pretending"),
        )
        assertEquals(
            DownloadTarget.SystemDownloads,
            resolveDownloadTarget("content://com.android.providers.downloads.documents"),
        )
    }

    @Test
    fun validTreeUriIsKeptTrimmed() {
        val target = resolveDownloadTarget("  $treeUri  ")
        assertTrue(target is DownloadTarget.CustomTree)
        assertEquals(treeUri, (target as DownloadTarget.CustomTree).treeUri)
    }

    @Test
    fun treeUriDetectionRequiresContentSchemeAndTreeSegment() {
        assertTrue(isSupportedTreeUri(treeUri))
        assertTrue(isSupportedTreeUri("CONTENT://x/tree/primary%3ADownloads"))
        assertFalse(isSupportedTreeUri("content://x/documents/primary%3ADownloads"))
        assertFalse(isSupportedTreeUri(""))
    }

    @Test
    fun displayNameDecodesAndDropsTheStorageRoot() {
        assertEquals(
            "Documents/NovelBooks",
            treeUriDisplayName(
                "content://com.android.externalstorage.documents/tree/primary%3ADocuments%2FNovelBooks",
            ),
        )
        // A decoded-but-undecodable segment falls back to the raw value.
        assertEquals(
            "Downloads",
            treeUriDisplayName("content://some.provider/tree/Downloads"),
        )
    }

    @Test
    fun displayNameHandlesRootOnlyAndNonTreeValues() {
        assertEquals(
            "",
            treeUriDisplayName("content://com.android.externalstorage.documents/tree/primary%3A"),
        )
        assertEquals(
            "/sdcard/Books",
            treeUriDisplayName("/sdcard/Books"),
        )
    }

    @Test
    fun dirLabelAlwaysIncludesTheSubdirectory() {
        assertEquals(
            "Documents/NovelBooks/NovelDL",
            customDownloadDirLabel(treeUri),
        )
        // Root-only tree: the subdirectory name is the whole label.
        assertEquals(
            "NovelDL",
            customDownloadDirLabel("content://com.android.externalstorage.documents/tree/primary%3A"),
        )
    }

    @Test
    fun subdirectoryConstantIsStable() {
        // DownloadManager's free path and the SAF path must never drift apart
        // in naming expectations; the label tests above rely on this value.
        assertEquals("NovelDL", DOWNLOAD_SUBDIRECTORY)
        assertEquals("NovelDL", HistoryDownloader.SUBDIRECTORY)
    }

    // ------------------------------------------------------------------
    // Persisted URI grants: a stored tree is only usable while the app still
    // holds a write grant, and losing it must fall back to system Downloads.
    // ------------------------------------------------------------------

    @Test
    fun persistedWriteGrantKeepsTheCustomTree() {
        assertEquals(
            DownloadTarget.CustomTree(treeUri),
            resolveDownloadTarget(treeUri, setOf(treeUri)),
        )
        // Providers may persist the URI with a trailing slash.
        assertEquals(
            DownloadTarget.CustomTree(treeUri),
            resolveDownloadTarget(treeUri, setOf("$treeUri/")),
        )
        assertEquals(
            DownloadTarget.CustomTree(treeUri),
            resolveDownloadTarget("  $treeUri/  ", setOf(treeUri)),
        )
    }

    @Test
    fun lostGrantFallsBackToSystemDownloads() {
        val otherTree = "content://com.android.externalstorage.documents/tree/primary%3ADownload"

        assertEquals(DownloadTarget.SystemDownloads, resolveDownloadTarget(treeUri, emptySet()))
        assertEquals(DownloadTarget.SystemDownloads, resolveDownloadTarget(treeUri, setOf(otherTree)))
        // Passing no grant collection skips the check (URI shape only).
        assertTrue(resolveDownloadTarget(treeUri) is DownloadTarget.CustomTree)
    }

    @Test
    fun customTreeAvailabilityOnlyFlagsAStoredButUnusableValue() {
        assertTrue(customTreeUnavailable(treeUri, emptySet()))
        assertFalse(customTreeUnavailable(treeUri, setOf(treeUri)))
        assertFalse(customTreeUnavailable("", setOf(treeUri)))
        assertFalse(customTreeUnavailable(null, emptySet()))
        // An empty grant list has no tree "stored", so there is nothing to warn about.
        assertFalse(customTreeUnavailable("   ", emptySet()))
    }

    @Test
    fun emptyUriNeverCountsAsGranted() {
        assertFalse(hasPersistedWriteAccess("", emptySet()))
        assertFalse(hasPersistedWriteAccess("   ", setOf("")))
        assertTrue(hasPersistedWriteAccess(treeUri, setOf(treeUri)))
    }

    // ------------------------------------------------------------------
    // File-name conflicts: SAF providers differ in how they treat duplicate
    // display names, so the name is made unique before createFile().
    // ------------------------------------------------------------------

    @Test
    fun uniqueChildNameKeepsFreeNames() {
        assertEquals("book.txt", uniqueChildName("book.txt", emptyList()))
        assertEquals("book.txt", uniqueChildName("book.txt", listOf("other.txt")))
        assertEquals("book.txt", uniqueChildName("  book.txt  ", listOf("other.txt")))
    }

    @Test
    fun uniqueChildNameAppendsACounterBeforeTheExtension() {
        assertEquals("book (1).txt", uniqueChildName("book.txt", listOf("book.txt")))
        assertEquals(
            "book (2).txt",
            uniqueChildName("book.txt", listOf("book.txt", "book (1).txt")),
        )
        // External storage is case-insensitive, so BOOK.EPUB collides.
        assertEquals("book (1).epub", uniqueChildName("book.epub", listOf("BOOK.EPUB")))
        assertEquals("book (1).txt", uniqueChildName("book.txt", listOf("  book.txt  ")))
    }

    @Test
    fun uniqueChildNameHandlesNamesWithoutExtensions() {
        assertEquals("book (1)", uniqueChildName("book", listOf("book")))
        // A leading dot marks a hidden file, not an extension.
        assertEquals(".nomedia (1)", uniqueChildName(".nomedia", listOf(".nomedia")))
        assertEquals("download (1)", uniqueChildName("   ", listOf("download")))
    }

    @Test
    fun uniqueChildNameStaysWithinTheLengthLimit() {
        val longName = "a".repeat(180) + ".txt"

        val result = uniqueChildName(longName, listOf(longName))

        assertTrue(result.length <= 180)
        assertTrue(result.endsWith(".txt"))
        assertTrue(result.contains(" (1)"))
    }

    @Test
    fun uniqueChildNameIgnoresBlankExistingEntries() {
        assertEquals("book.txt", uniqueChildName("book.txt", listOf("", "   ")))
    }
}
