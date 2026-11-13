package com.lingyi.noveldl.data

import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Wire contract of the optional search filters. The names must match the REST
 * schema (`min_words`/`max_words`/`updated_after`), and an unset filter must be
 * absent rather than `null`, which is what `Api`'s serializer does.
 */
class SearchRequestSerializationTest {

    private val json = Json {
        ignoreUnknownKeys = true
        explicitNulls = false
        encodeDefaults = false
    }

    @Test
    fun filtersUseTheServerFieldNames() {
        val body = json.encodeToString(
            SearchRequest.serializer(),
            SearchRequest(
                keyword = "斗破苍穹",
                sites = listOf("qidian"),
                min_words = 100_000,
                max_words = 2_000_000,
                updated_after = "2024-01-31",
            ),
        )

        assertTrue(body, body.contains("\"min_words\":100000"))
        assertTrue(body, body.contains("\"max_words\":2000000"))
        assertTrue(body, body.contains("\"updated_after\":\"2024-01-31\""))
        assertTrue(body, body.contains("\"keyword\":\"斗破苍穹\""))
        assertTrue(body, body.contains("\"sites\":[\"qidian\"]"))
    }

    @Test
    fun unsetFiltersStayOffTheWire() {
        val body = json.encodeToString(
            SearchRequest.serializer(),
            SearchRequest(keyword = "x"),
        )

        assertEquals("""{"keyword":"x"}""", body)
        assertFalse(body, body.contains("min_words"))
        assertFalse(body, body.contains("max_words"))
        assertFalse(body, body.contains("updated_after"))
    }

    @Test
    fun zeroAndTheMaximumAreEncodedInsteadOfTreatedAsAbsent() {
        val body = json.encodeToString(
            SearchRequest.serializer(),
            SearchRequest(keyword = "x", min_words = 0, max_words = 1_000_000_000),
        )

        assertTrue(body, body.contains("\"min_words\":0"))
        assertTrue(body, body.contains("\"max_words\":1000000000"))
    }
}
