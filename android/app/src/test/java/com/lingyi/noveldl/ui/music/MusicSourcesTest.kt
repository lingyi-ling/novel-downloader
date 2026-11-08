package com.lingyi.noveldl.ui.music

import com.lingyi.noveldl.data.MusicTrack
import org.junit.Assert.assertEquals
import org.junit.Test

/** Display rules for aggregate result rows: sources first, provider fallback. */
class MusicSourcesTest {

    private val labels = mapOf("netease" to "网易云", "qq" to "QQ 音乐")

    private fun track(
        provider: String = "netease",
        sources: List<String> = emptyList(),
    ) = MusicTrack(provider = provider, id = "1", name = "晴天", sources = sources)

    @Test
    fun aggregateSourcesAreDisplayedInOrderWithLabels() {
        val track = track(sources = listOf("netease", "qq"))

        assertEquals(listOf("netease", "qq"), MusicSources.keys(track))
        assertEquals("网易云 / QQ 音乐", MusicSources.label(track, labels))
    }

    @Test
    fun unknownSourceKeysAreKeptVerbatim() {
        val track = track(sources = listOf("netease", "kugou"))

        assertEquals(listOf("netease", "kugou"), MusicSources.keys(track))
        assertEquals("网易云 / kugou", MusicSources.label(track, labels))
    }

    @Test
    fun blankSourcesAndWhitespaceAreDropped() {
        assertEquals(
            listOf("qq"),
            MusicSources.keys(track(sources = listOf(" ", "qq", ""))),
        )
    }

    @Test
    fun emptySourcesFallBackToProvider() {
        val track = track(provider = "netease")

        assertEquals(listOf("netease"), MusicSources.keys(track))
        assertEquals("网易云", MusicSources.label(track, labels))
    }

    @Test
    fun missingProviderAndSourcesRenderEmptyLabel() {
        val track = track(provider = "")

        assertEquals(emptyList<String>(), MusicSources.keys(track))
        assertEquals("", MusicSources.label(track, labels))
    }
}
