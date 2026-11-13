package com.lingyi.noveldl.ui.music

import com.lingyi.noveldl.data.MusicJobResult
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * Path rules for the `<stem>.lrc` the music engine writes beside an audio
 * download: how the sibling name is derived, when it may be offered to the
 * user, and how the batch summary counts it.
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
}
