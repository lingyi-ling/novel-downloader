package com.lingyi.noveldl.ui.music

import com.lingyi.noveldl.data.MusicJobResult
import com.lingyi.noveldl.data.MusicLyricFile
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Path rules for the lyric files the music engine writes beside an audio
 * download: how the sibling names are derived (original `.lrc` and translated
 * `.zh.lrc`), when they may be offered to the user, and how the batch summary
 * counts them.
 */
class MusicLyricsTest {

    private val root = "/data/user/0/com.lingyi.noveldl/files/engine_home/downloads/music"

    private fun existing(vararg paths: String): (String) -> Boolean {
        val files = paths.toSet()
        return { it in files }
    }

    // ---------- path derivation ----------

    @Test
    fun lyricPathReplacesTheAudioExtension() {
        assertEquals(
            "$root/song.lrc",
            MusicLyrics.lyricPathFor("$root/song.mp3"),
        )
        assertEquals(
            "$root/song.lrc",
            MusicLyrics.lyricPathFor("$root/song.flac"),
        )
        assertEquals(
            "$root/song.lrc",
            MusicLyrics.lyricPathFor("$root/song.m4a"),
        )
    }

    @Test
    fun lyricPathAppendsWithoutAnExtensionAndKeepsWindowsSeparators() {
        assertEquals("$root/song.lrc", MusicLyrics.lyricPathFor("$root/song"))
        assertEquals("C:\\Music\\song.lrc", MusicLyrics.lyricPathFor("C:\\Music\\song.mp3"))
    }

    @Test
    fun lyricPathIgnoresDotsInParentDirectories() {
        assertEquals(
            "/data/a.b/song.lrc",
            MusicLyrics.lyricPathFor("/data/a.b/song.mp3"),
        )
    }

    @Test
    fun lyricPathIsEmptyForBlankInput() {
        assertEquals("", MusicLyrics.lyricPathFor(""))
        assertEquals("", MusicLyrics.lyricPathFor("   "))
        assertEquals("", MusicLyrics.lyricPathFor("$root/"))
    }

    @Test
    fun translationLyricPathAddsZhBeforeTheLrcExtension() {
        assertEquals(
            "$root/song.zh.lrc",
            MusicLyrics.translationLyricPathFor("$root/song.mp3"),
        )
        assertEquals(
            "$root/song.zh.lrc",
            MusicLyrics.translationLyricPathFor("$root/song.flac"),
        )
        // No audio extension: both siblings use the same stem.
        assertEquals(
            "$root/song.zh.lrc",
            MusicLyrics.translationLyricPathFor("$root/song"),
        )
        assertEquals(
            "C:\\Music\\song.zh.lrc",
            MusicLyrics.translationLyricPathFor("C:\\Music\\song.mp3"),
        )
    }

    @Test
    fun translationLyricPathIsEmptyForBlankInput() {
        assertEquals("", MusicLyrics.translationLyricPathFor(""))
        assertEquals("", MusicLyrics.translationLyricPathFor("   "))
        assertEquals("", MusicLyrics.translationLyricPathFor("$root/"))
    }

    // ---------- existence / containment ----------

    @Test
    fun findReturnsTheLyricWhenItExistsInsideThePrivateRoot() {
        val audio = "$root/song.mp3"
        val lyric = "$root/song.lrc"

        assertEquals(
            lyric,
            MusicLyrics.find(audio, listOf(root), existing(lyric)),
        )
    }

    @Test
    fun findReturnsNullWhenTheLyricWasNotSaved() {
        assertNull(
            MusicLyrics.find("$root/song.mp3", listOf(root), existing()),
        )
    }

    @Test
    fun findRejectsLyricsOutsideTheAppPrivateDirectories() {
        val audio = "/storage/emulated/0/Music/song.mp3"
        val lyric = "/storage/emulated/0/Music/song.lrc"

        // The file "exists", but the sandbox check runs first: it cannot be
        // reached through the FileProvider and must not become a button.
        assertNull(MusicLyrics.find(audio, listOf(root), existing(lyric)))
    }

    @Test
    fun findRejectsTraversalOutOfThePrivateRoot() {
        val audio = "$root/../secret/song.mp3"
        val lyric = "$root/../secret/song.lrc"

        assertNull(MusicLyrics.find(audio, listOf(root), existing(lyric)))
    }

    @Test
    fun findTreatsAThrowingProbeAsNoLyric() {
        val audio = "$root/song.mp3"

        assertNull(
            MusicLyrics.find(audio, listOf(root)) { throw IllegalStateException("gone") },
        )
    }

    @Test
    fun findIsEmptyForBlankAudioPaths() {
        assertNull(MusicLyrics.find("", listOf(root), existing("$root/.lrc")))
    }

    // ---------- original + translation ----------

    @Test
    fun findAllReturnsOriginalThenTranslation() {
        val audio = "$root/song.mp3"
        val original = "$root/song.lrc"
        val translation = "$root/song.zh.lrc"

        assertEquals(
            listOf(
                MusicLyricFile(MusicLyrics.LANG_ORIGINAL, original),
                MusicLyricFile(MusicLyrics.LANG_TRANSLATION, translation),
            ),
            MusicLyrics.findAll(audio, listOf(root), existing(original, translation)),
        )
    }

    @Test
    fun findAllKeepsOneButtonWhenOnlyTheOriginalWasSaved() {
        val audio = "$root/song.mp3"

        assertEquals(
            listOf(MusicLyricFile(MusicLyrics.LANG_ORIGINAL, "$root/song.lrc")),
            MusicLyrics.findAll(audio, listOf(root), existing("$root/song.lrc")),
        )
    }

    @Test
    fun findAllKeepsATranslationThatWasSavedWithoutTheOriginal() {
        // The engine normally writes the translation into `<stem>.lrc` when
        // there is no original, but a provider that only supplies `tlyric`
        // can leave just the `.zh.lrc`.
        assertEquals(
            listOf(MusicLyricFile(MusicLyrics.LANG_TRANSLATION, "$root/song.zh.lrc")),
            MusicLyrics.findAll("$root/song.mp3", listOf(root), existing("$root/song.zh.lrc")),
        )
    }

    @Test
    fun findAllIsEmptyWhenNeitherFileExists() {
        assertTrue(
            MusicLyrics.findAll("$root/song.mp3", listOf(root), existing()).isEmpty(),
        )
    }

    @Test
    fun findAllRejectsLyricsOutsideTheAppPrivateDirectories() {
        val audio = "/storage/emulated/0/Music/song.mp3"
        val files = existing(
            "/storage/emulated/0/Music/song.lrc",
            "/storage/emulated/0/Music/song.zh.lrc",
        )

        assertTrue(MusicLyrics.findAll(audio, listOf(root), files).isEmpty())
    }

    @Test
    fun findAllRejectsTraversalOutOfThePrivateRoot() {
        val audio = "$root/../secret/song.mp3"
        val files = existing("$root/../secret/song.lrc", "$root/../secret/song.zh.lrc")

        assertTrue(MusicLyrics.findAll(audio, listOf(root), files).isEmpty())
    }

    @Test
    fun findAllTreatsAThrowingProbeAsNoLyrics() {
        assertTrue(
            MusicLyrics.findAll("$root/song.mp3", listOf(root)) {
                throw IllegalStateException("gone")
            }.isEmpty(),
        )
    }

    @Test
    fun findAllIsEmptyForBlankAudioPaths() {
        assertTrue(MusicLyrics.findAll("", listOf(root), existing("$root/.lrc")).isEmpty())
        assertTrue(MusicLyrics.findAll("$root/", listOf(root), existing("$root/.lrc")).isEmpty())
    }

    // ---------- summary ----------

    @Test
    fun savedCountCountsOnlyResultsWithAVerifiedLyric() {
        val results = listOf(
            MusicJobResult(status = "ok", lyricPath = "$root/a.lrc"),
            MusicJobResult(status = "ok"),
            MusicJobResult(status = "skip", lyricPath = "$root/b.lrc"),
            MusicJobResult(status = "fail"),
        )

        assertEquals(2, MusicLyrics.savedCount(results))
    }

    @Test
    fun savedCountIsZeroWithoutLyrics() {
        assertEquals(0, MusicLyrics.savedCount(emptyList()))
        assertEquals(
            0,
            MusicLyrics.savedCount(listOf(MusicJobResult(status = "ok"))),
        )
    }

    @Test
    fun savedCountCountsATrackWithBothLyricsOnce() {
        val results = listOf(
            MusicJobResult(
                status = "ok",
                lyricPath = "$root/a.lrc",
                lyricPaths = listOf(
                    MusicLyricFile(MusicLyrics.LANG_ORIGINAL, "$root/a.lrc"),
                    MusicLyricFile(MusicLyrics.LANG_TRANSLATION, "$root/a.zh.lrc"),
                ),
            ),
            MusicJobResult(
                status = "skip",
                lyricPaths = listOf(
                    MusicLyricFile(MusicLyrics.LANG_TRANSLATION, "$root/b.zh.lrc"),
                ),
            ),
            MusicJobResult(status = "ok"),
        )

        assertEquals(2, MusicLyrics.savedCount(results))
    }
}
