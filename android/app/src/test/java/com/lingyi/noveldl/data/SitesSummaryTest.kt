package com.lingyi.noveldl.data

import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** JVM tests for the counts-only sites overview used by the settings screen. */
class SitesSummaryTest {

    @Test
    fun emptyResponseCountsZeroSites() {
        val summary = SitesResponse().toSitesSummary()
        assertEquals(0, summary.searchCount)
        assertEquals(0, summary.downloadCount)
        assertTrue(summary.isEmpty)
    }

    @Test
    fun countsAreTheMapSizes() {
        val response = SitesResponse(
            search = mapOf("b520" to "url-a", "qidian" to "url-b", "booktxt" to "url-c"),
            download = mapOf("b520" to "url-a", "booktxt" to "url-c"),
        )
        val summary = response.toSitesSummary()
        assertEquals(3, summary.searchCount)
        assertEquals(2, summary.downloadCount)
        assertFalse(summary.isEmpty)
    }

    @Test
    fun jsonMissingKeysFallBackToZeroSites() {
        // Mirrors Api's shared configuration (explicitNulls = false). Missing
        // keys keep the default empty maps; a literal JSON null would throw
        // (no coerceInputValues), matching the rest of the app's endpoints.
        val json = Json {
            ignoreUnknownKeys = true
            explicitNulls = false
            encodeDefaults = false
        }
        val decoded = json.decodeFromString<SitesResponse>(
            """{"download":{"b520":"url"},"extra":"ignored"}""",
        )
        val summary = decoded.toSitesSummary()
        assertEquals(0, summary.searchCount)
        assertEquals(1, summary.downloadCount)
    }

    @Test
    fun oneSiteOnlyIsNotEmpty() {
        val summary = SitesResponse(download = mapOf("b520" to "url")).toSitesSummary()
        assertFalse(summary.isEmpty)
        assertEquals(0, summary.searchCount)
        assertEquals(1, summary.downloadCount)
    }
}
