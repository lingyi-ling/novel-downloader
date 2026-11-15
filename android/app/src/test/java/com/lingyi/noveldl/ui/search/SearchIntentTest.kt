package com.lingyi.noveldl.ui.search

import com.lingyi.noveldl.R
import com.lingyi.noveldl.data.SearchResponse
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

/**
 * Contract of the list-level `query_intent` annotation: its wire name, the
 * missing/explicit-null compatibility, and the chip mapping. Unknown verdicts
 * from a newer server stay hidden instead of claiming an ordering.
 */
class SearchIntentTest {

    // Same configuration Api uses, so the decoding tested here matches real
    // responses, including unknown keys added by a newer server.
    private val json = Json {
        ignoreUnknownKeys = true
        explicitNulls = false
        encodeDefaults = false
    }

    @Test
    fun decodesTheServerFieldName() {
        val response = json.decodeFromString<SearchResponse>(
            """
            {
              "total": 1,
              "results": [],
              "query_intent": "author"
            }
            """.trimIndent(),
        )

        assertEquals(1, response.total)
        assertEquals("author", response.query_intent)
    }

    @Test
    fun legacyResponsesWithoutTheFieldStayNull() {
        val response = json.decodeFromString<SearchResponse>(
            """{"total":0,"results":[]}""",
        )

        assertNull(response.query_intent)
        assertNull(searchIntentBadge(response.query_intent))
    }

    @Test
    fun explicitNullIsTreatedAsAbsent() {
        val response = json.decodeFromString<SearchResponse>(
            """{"total":0,"results":[],"query_intent":null}""",
        )

        assertNull(response.query_intent)
    }

    @Test
    fun onlyExplicitVerdictsProduceAChip() {
        assertEquals(R.string.search_intent_title, searchIntentBadge("title")?.labelRes)
        assertEquals(R.string.search_intent_author, searchIntentBadge("author")?.labelRes)
        assertNull(searchIntentBadge(null))
        assertNull(searchIntentBadge(""))
        assertNull(searchIntentBadge("either"))
        assertNull(searchIntentBadge("book"))
        assertNull(searchIntentBadge("Title"))
    }
}