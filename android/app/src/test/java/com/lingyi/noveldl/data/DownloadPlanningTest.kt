package com.lingyi.noveldl.data

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** JVM tests for the pure URL/file-name rules used by history downloads. */
class DownloadPlanningTest {

    @Test
    fun baseUrlAndRelativePathAreJoinedWithOneSlash() {
        assertEquals(
            "http://10.0.2.2:8081/downloads/a.txt",
            resolveDownloadUrl("http://10.0.2.2:8081/", "/downloads/a.txt"),
        )
        assertEquals(
            "http://10.0.2.2:8081/downloads/a.txt",
            resolveDownloadUrl("http://10.0.2.2:8081", "downloads/a.txt"),
        )
        assertEquals(
            "http://host/api/downloads/a.txt",
            resolveDownloadUrl("http://host/api/", "/downloads/a.txt"),
        )
    }

    @Test
    fun absoluteUrlsAreNotPrefixedAgain() {
        assertEquals(
            "https://cdn.example.com/a.epub",
            resolveDownloadUrl("http://10.0.2.2:8081/", "https://cdn.example.com/a.epub"),
        )
        assertEquals(
            "HTTP://cdn.example.com/a.epub",
            resolveDownloadUrl("http://10.0.2.2:8081/", "HTTP://cdn.example.com/a.epub"),
        )
    }

    @Test
    fun onlyHttpSchemesAreSupported() {
        assertTrue(isSupportedDownloadUrl("http://host/a"))
        assertTrue(isSupportedDownloadUrl("HTTPS://host/a"))
        assertFalse(isSupportedDownloadUrl("ftp://host/a"))
        assertFalse(isSupportedDownloadUrl("javascript:alert(1)"))
        assertFalse(isSupportedDownloadUrl(""))
    }

    @Test
    fun sanitizeFileNameKeepsUnicodeAndStripsIllegalCharacters() {
        assertEquals("斗破苍穹.txt", sanitizeFileName("斗破苍穹.txt"))
        assertEquals("c_d_e_f_g_h_i", sanitizeFileName("c:d*e?f\"g<h>i"))
        assertEquals("passwd", sanitizeFileName("a/b/passwd"))
        assertEquals("passwd", sanitizeFileName("a\\b\\passwd"))
        assertEquals("name.txt", sanitizeFileName("  name.txt  "))
        assertEquals("name", sanitizeFileName("...name..."))
        assertEquals("passwd", sanitizeFileName("../../../etc/passwd"))
        assertEquals("", sanitizeFileName("   "))
    }

    @Test
    fun controlCharactersBecomeUnderscores() {
        assertEquals("a_b", sanitizeFileName("a\u0000b"))
        assertEquals("a_b", sanitizeFileName("a\nb"))
    }

    @Test
    fun downloadNamePrefersTheApiDisplayName() {
        assertEquals(
            "斗破苍穹.txt",
            downloadFileName("斗破苍穹.txt", "/downloads/ignored.txt", "txt"),
        )
        assertEquals(
            "safe_name.txt",
            downloadFileName("../safe:name.txt", "/downloads/ignored.txt", "txt"),
        )
    }

    @Test
    fun downloadNameDecodesTheUrlWhenTheDisplayNameIsBlank() {
        assertEquals(
            "斗破苍穹.txt",
            downloadFileName(
                "",
                "/downloads/%E6%96%97%E7%A0%B4%E8%8B%8D%E7%A9%B9.txt",
                "txt",
            ),
        )
        assertEquals(
            "book.epub",
            downloadFileName("", "/downloads/book.epub?token=1#frag", "epub"),
        )
    }

    @Test
    fun downloadNameFallsBackToTheExtension() {
        assertEquals("download.txt", downloadFileName("", "", "txt"))
        assertEquals("download.epub", downloadFileName("", "/", ".epub"))
        assertEquals("download.txt", downloadFileName("", "", ""))
    }

    @Test
    fun knownExtensionsMapToMimeTypes() {
        assertEquals("text/plain", mimeTypeForExtension("txt"))
        assertEquals("text/plain", mimeTypeForExtension("TXT"))
        assertEquals("application/epub+zip", mimeTypeForExtension("epub"))
        assertNull(mimeTypeForExtension("pdf"))
        assertNull(mimeTypeForExtension(""))
    }
}