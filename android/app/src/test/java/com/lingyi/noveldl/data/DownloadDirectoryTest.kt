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
}
