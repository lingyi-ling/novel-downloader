package com.lingyi.noveldl.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertSame
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

/** JVM tests for the custom SAF download-directory rules. */
class DownloadDirectoryTest {

    private val treeUri =
        "content://com.android.externalstorage.documents/tree/primary%3ADocuments%2FNovelBooks"

    /** Same tree with the case-insensitive URI parts uppercased. */
    private val shoutedTreeUri =
        "CONTENT://COM.ANDROID.EXTERNALSTORAGE.DOCUMENTS/tree/primary%3ADocuments%2FNovelBooks"

    /** Padded the way a value copied from a grant/log may arrive. */
    private val paddedShoutedTreeUri = "  $shoutedTreeUri///  "

    /** Only the document id differs in case: a different tree. */
    private val pathCaseChangedTreeUri = treeUri.replace("NovelBooks", "novelbooks")

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

    @Test
    fun uniqueChildNameTreatsImplausiblyLongExtensionsAsPartOfTheName() {
        val longExtension = "x." + "e".repeat(20)

        // A "extension" longer than 17 characters is not an extension: the
        // counter goes at the end instead of before that suffix.
        assertEquals(
            "$longExtension (1)",
            uniqueChildName(longExtension, listOf(longExtension)),
        )
    }

    @Test
    fun uniqueChildNameKeepsAnExtensionOnAHiddenFile() {
        // Only a *leading* dot at index 0 has no extension; `.config.json`
        // is a hidden file whose final segment is still an extension.
        assertEquals(
            ".config (1).json",
            uniqueChildName(".config.json", listOf(".config.json")),
        )
    }

    @Test
    fun uniqueChildNameFoldsUnicodeCase() {
        assertEquals("Ä (1).txt", uniqueChildName("Ä.txt", listOf("ä.txt")))
    }

    @Test
    fun uniqueChildNameGivesUpInsteadOfOverwritingAfterAllCandidatesAreTaken() {
        val taken = listOf("book.txt") + (1..999).map { "book ($it).txt" }

        // Last resort: the provider's auto-rename protects the existing file.
        assertEquals("book.txt", uniqueChildName("book.txt", taken))
    }

    // ------------------------------------------------------------------
    // URI normalization: whitespace, trailing slashes and the
    // case-insensitive scheme/authority must not affect grant matching, while
    // the document path (which providers treat as opaque) stays exact.
    // ------------------------------------------------------------------

    @Test
    fun uriKeysNormalizeWhitespaceAndTrailingSlashes() {
        assertEquals(treeUri, treeUriKey("  $treeUri  "))
        assertEquals(treeUri, treeUriKey("$treeUri/"))
        assertEquals(treeUri, treeUriKey("$treeUri///"))
        assertEquals(treeUri, treeUriKey("\t$treeUri\n"))
    }

    @Test
    fun uriKeysLowercaseTheSchemeAndAuthorityButNotThePath() {
        assertEquals(treeUri, treeUriKey(shoutedTreeUri))
        // The document id is case-sensitive; folding it would conflate two
        // different directories on case-sensitive providers.
        assertNotEquals(treeUri, treeUriKey(pathCaseChangedTreeUri))
    }

    @Test
    fun grantMatchingIgnoresWhitespaceSlashesAndAuthorityCase() {
        assertTrue(hasPersistedWriteAccess(paddedShoutedTreeUri, setOf("$treeUri/")))
        assertTrue(
            hasPersistedWriteAccess(
                treeUri,
                listOf(
                    "",
                    "   ",
                    "content://other.provider/tree/primary%3ABooks",
                    paddedShoutedTreeUri,
                ),
            ),
        )
        // Different path case is a different tree, not the same grant.
        assertFalse(hasPersistedWriteAccess(treeUri, setOf(pathCaseChangedTreeUri)))
    }

    @Test
    fun resolveDownloadTargetReturnsTheNormalizedUri() {
        assertEquals(
            DownloadTarget.CustomTree(treeUri),
            resolveDownloadTarget(paddedShoutedTreeUri, setOf("$treeUri/")),
        )
        assertFalse(customTreeUnavailable(paddedShoutedTreeUri, setOf(treeUri)))
        assertTrue(customTreeUnavailable(treeUri, setOf(pathCaseChangedTreeUri)))
    }

    // ------------------------------------------------------------------
    // SAF destination preparation: tree deleted, missing subdirectory,
    // DocumentFile returning null and createFile throwing. The fake keeps the
    // rules testable without a device, ContentResolver or SAF permission.
    // ------------------------------------------------------------------

    private val unavailable = "directory unavailable"
    private val createFailed = "could not create"

    private fun prepare(
        tree: SafDocument?,
        desiredName: String = "book.txt",
        mimeType: String = "text/plain",
    ): SafDocument = prepareDownloadDestination(
        tree = tree,
        desiredName = desiredName,
        mimeType = mimeType,
        unavailableMessage = unavailable,
        createFailedMessage = createFailed,
    )

    @Test
    fun prepareRejectsMissingUnresolvableAndDeletedTrees() {
        val trees: List<SafDocument?> = listOf(
            // DocumentFile.fromTreeUri() returned null: the provider is gone.
            null,
            // The grant survived but the folder itself was deleted/moved.
            FakeSafDocument(exists = false),
            // The URI resolves to a file, not a directory tree.
            FakeSafDocument(isDirectory = false),
        )

        for (tree in trees) {
            val error = assertThrows(DownloadDirectoryUnavailableException::class.java) {
                prepare(tree)
            }
            assertEquals(unavailable, error.message)
        }
    }

    @Test
    fun prepareCreatesTheSubdirectoryAndReturnsTheCreatedFile() {
        val createdFile = FakeSafDocument(name = "book (1).txt", isDirectory = false)
        val directory = FakeSafDocument(name = DOWNLOAD_SUBDIRECTORY).apply {
            // Existing names collide case-insensitively, as on FAT/exFAT.
            childNames += "BOOK.TXT"
            createFileResult = createdFile
        }
        val tree = FakeSafDocument(name = "Books").apply {
            createDirectoryResult = directory
        }

        val prepared = prepare(tree)

        assertEquals(listOf(DOWNLOAD_SUBDIRECTORY), tree.lookedUpDirectories)
        assertEquals(listOf(DOWNLOAD_SUBDIRECTORY), tree.createdDirectories)
        assertEquals(listOf("text/plain" to "book (1).txt"), directory.createdFiles)
        assertSame(createdFile, prepared)
        assertEquals("book (1).txt", prepared.name)
    }

    @Test
    fun prepareReusesAnExistingSubdirectoryWithoutCreatingIt() {
        val directory = FakeSafDocument(name = DOWNLOAD_SUBDIRECTORY).apply {
            createFileResult = FakeSafDocument(name = "book.txt", isDirectory = false)
        }
        val tree = FakeSafDocument().apply { findDirectoryResult = directory }

        val prepared = prepare(tree, desiredName = "book.txt", mimeType = "application/epub+zip")

        assertEquals(listOf(DOWNLOAD_SUBDIRECTORY), tree.lookedUpDirectories)
        assertTrue(tree.createdDirectories.isEmpty())
        assertEquals(listOf("application/epub+zip" to "book.txt"), directory.createdFiles)
        assertEquals("book.txt", prepared.name)
    }

    @Test
    fun prepareReplacesANonDirectoryEntryNamedLikeTheSubdirectory() {
        val directory = FakeSafDocument(name = DOWNLOAD_SUBDIRECTORY).apply {
            createFileResult = FakeSafDocument(name = "book.txt", isDirectory = false)
        }
        val tree = FakeSafDocument().apply {
            findDirectoryResult =
                FakeSafDocument(name = DOWNLOAD_SUBDIRECTORY, isDirectory = false)
            createDirectoryResult = directory
        }

        prepare(tree)

        assertEquals(listOf(DOWNLOAD_SUBDIRECTORY), tree.createdDirectories)
    }

    @Test
    fun prepareFailsWhenTheSubdirectoryCannotBeCreated() {
        val tree = FakeSafDocument()
        val error = assertThrows(IllegalStateException::class.java) { prepare(tree) }

        assertEquals(createFailed, error.message)
        // A create failure is a hard error, not a "tree went away" fallback.
        assertFalse(error is DownloadDirectoryUnavailableException)
        assertTrue(tree.createdFiles.isEmpty())
    }

    @Test
    fun prepareFailsWhenCreateFileReturnsNull() {
        val directory = FakeSafDocument(name = DOWNLOAD_SUBDIRECTORY).apply {
            createFileResult = null
        }
        val tree = FakeSafDocument().apply {
            findDirectoryResult = directory
        }

        val error = assertThrows(IllegalStateException::class.java) { prepare(tree) }

        assertEquals(createFailed, error.message)
        assertFalse(error is DownloadDirectoryUnavailableException)
        assertEquals(listOf("text/plain" to "book.txt"), directory.createdFiles)
    }

    @Test
    fun preparePropagatesProviderFailuresFromCreateFile() {
        val failure = SecurityException("grant revoked mid-write")
        val directory = FakeSafDocument(name = DOWNLOAD_SUBDIRECTORY).apply {
            createFileFailure = failure
        }
        val tree = FakeSafDocument().apply {
            findDirectoryResult = directory
        }

        val thrown = assertThrows(SecurityException::class.java) { prepare(tree) }

        assertSame(failure, thrown)
        // Nothing was returned to the caller, so there is nothing to delete.
        assertFalse(directory.deleted)
    }

    @Test
    fun prepareResolvesNameConflictsBeforeCreatingTheFile() {
        val original = FakeSafDocument(name = "book.txt", isDirectory = false)
        val directory = FakeSafDocument(name = DOWNLOAD_SUBDIRECTORY).apply {
            childNames += listOf("book.txt", "book (1).txt", "BOOK (2).TXT")
            createFileResult = original
        }
        val tree = FakeSafDocument().apply { findDirectoryResult = directory }

        prepare(tree)

        // Both case variants are taken, so the third candidate is used.
        assertEquals(listOf("text/plain" to "book (3).txt"), directory.createdFiles)
    }

    @Test
    fun prepareReportsTheNameTheProviderActuallyCreated() {
        val directory = FakeSafDocument(name = DOWNLOAD_SUBDIRECTORY).apply {
            createFileResult = FakeSafDocument(name = "book.txt", isDirectory = false)
        }
        val tree = FakeSafDocument().apply { findDirectoryResult = directory }

        // Providers may enforce an extension or adjust the display name.
        assertEquals("book.txt", prepare(tree, desiredName = "book").name)
    }
}

/**
 * Configurable [SafDocument] that records calls, standing in for a real
 * `DocumentFile` (whose methods are not available in JVM unit tests).
 */
private class FakeSafDocument(
    override val name: String? = null,
    override val isDirectory: Boolean = true,
    override val exists: Boolean = true,
) : SafDocument {
    val childNames = mutableListOf<String>()
    val lookedUpDirectories = mutableListOf<String>()
    val createdDirectories = mutableListOf<String>()
    val createdFiles = mutableListOf<Pair<String, String>>()

    var findDirectoryResult: SafDocument? = null
    var createDirectoryResult: SafDocument? = null
    var createFileResult: SafDocument? = null
    var createFileFailure: RuntimeException? = null
    var deleted = false

    override fun findDirectory(displayName: String): SafDocument? {
        lookedUpDirectories += displayName
        return findDirectoryResult
    }

    override fun createDirectory(displayName: String): SafDocument? {
        createdDirectories += displayName
        return createDirectoryResult
    }

    override fun listChildNames(): List<String> = childNames.toList()

    override fun createFile(mimeType: String, displayName: String): SafDocument? {
        createdFiles += mimeType to displayName
        createFileFailure?.let { throw it }
        return createFileResult
    }

    override fun delete(): Boolean {
        deleted = true
        return true
    }
}
