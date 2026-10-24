package com.lingyi.noveldl.data

import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * JVM tests for the site multi-select rules: the compact encoding is used by
 * both `rememberSaveable` and SharedPreferences, so round-tripping it is what
 * state restoration depends on.
 */
class SiteSelectionTest {

    private val json = Json {
        ignoreUnknownKeys = true
        explicitNulls = false
        encodeDefaults = false
    }

    @Test
    fun decodeTrimsDropsBlanksAndDuplicates() {
        assertEquals(
            listOf("qidian", "biquge5"),
            decodeSelectedSites(" qidian , ,qidian, biquge5 ,"),
        )
        assertEquals(emptyList<String>(), decodeSelectedSites(null))
        assertEquals(emptyList<String>(), decodeSelectedSites(""))
        assertEquals(emptyList<String>(), decodeSelectedSites(" , , "))
    }

    @Test
    fun encodingRoundTripsTheSavedSelection() {
        val saved = listOf("biquge5", "qidian")
        val encoded = encodeSelectedSites(saved)

        assertEquals("biquge5,qidian", encoded)
        assertEquals(saved, decodeSelectedSites(encoded))
    }

    @Test
    fun encodingNormalizesWhitespaceAndDuplicates() {
        assertEquals(
            "qidian,biquge5",
            encodeSelectedSites(listOf(" qidian ", "qidian", "biquge5")),
        )
    }

    @Test
    fun emptySelectionMeansAllSites() {
        assertNull(selectedSitesParam(emptyList()))
        assertNull(selectedSitesParam(listOf("  ", "")))
        assertEquals(
            listOf("qidian", "biquge5"),
            selectedSitesParam(listOf("qidian", " qidian ", "biquge5")),
        )
    }

    @Test
    fun togglingAddsAndRemovesKeys() {
        assertEquals(listOf("qidian"), toggleSiteSelection(emptyList(), "qidian"))
        assertEquals(
            listOf("qidian", "biquge5"),
            toggleSiteSelection(listOf("qidian"), "biquge5"),
        )
        assertEquals(listOf("biquge5"), toggleSiteSelection(listOf("qidian", "biquge5"), "qidian"))
        // Blank keys are ignored, leaving the current selection untouched.
        assertEquals(listOf("qidian"), toggleSiteSelection(listOf("qidian"), "  "))
        assertEquals(emptyList<String>(), toggleSiteSelection(listOf("qidian", "qidian"), "qidian"))
    }

    @Test
    fun summaryPrefersLabelsAndFallsBackToKeys() {
        val labels = mapOf("qidian" to "起点中文网", "biquge5" to "笔趣阁")

        assertEquals("起点中文网, 笔趣阁", siteSummary(listOf("qidian", "biquge5"), labels))
        assertEquals("起点中文网, gone", siteSummary(listOf("qidian", "gone"), labels))
        assertEquals("", siteSummary(emptyList(), labels))
    }

    @Test
    fun searchRequestOmitsSitesWhenAllSitesAreSelected() {
        val body = json.encodeToString(
            SearchRequest(keyword = "斗破", sites = selectedSitesParam(emptyList())),
        )

        assertFalse(body.contains("\"sites\""))
        assertTrue(body.contains("\"keyword\":\"斗破\""))
    }

    @Test
    fun searchRequestSerializesTheSelectedSitesInOrder() {
        val selected = listOf(" qidian ", "qidian", "biquge5")
        val body = json.encodeToString(
            SearchRequest(keyword = "斗破", sites = selectedSitesParam(selected)),
        )

        assertTrue(body.contains("\"sites\":[\"qidian\",\"biquge5\"]"))
    }
}