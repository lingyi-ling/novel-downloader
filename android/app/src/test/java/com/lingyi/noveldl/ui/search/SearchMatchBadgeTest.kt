package com.lingyi.noveldl.ui.search

import com.lingyi.noveldl.R
import com.lingyi.noveldl.data.SearchResult
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Contract of the read-only search relevance annotations: their wire names,
 * the tier-to-badge mapping, and the fallback when an older response omits
 * them (cached rows, browse, or an embedded engine without the fields).
 */
class SearchMatchBadgeTest {

    // Same configuration Api uses, so the decoding tested here matches real
    // responses, including unknown keys added by a newer server.
    private val json = Json {
        ignoreUnknownKeys = true
        explicitNulls = false
        encodeDefaults = false
    }

    @Test
    fun decodesTheServerFieldNames() {
        val result = json.decodeFromString<SearchResult>(
            """
            {
              "site": "qidian",
              "book_id": "123",
              "title": "斗破苍穹",
              "rank_score": 0.912345,
              "match_tier": 4
            }
            """.trimIndent(),
        )

        assertEquals(0.912345, result.rank_score!!, 1e-9)
        assertEquals(4, result.match_tier)
    }

    @Test
    fun legacyResponsesWithoutTheFieldsStayNull() {
        val result = json.decodeFromString<SearchResult>(
            """{"site":"qidian","book_id":"123","title":"斗破苍穹"}""",
        )

        assertNull(result.rank_score)
        assertNull(result.match_tier)
        assertNull(searchMatchBadge(result))
    }

    @Test
    fun explicitNullsAreTreatedAsAbsent() {
        val result = json.decodeFromString<SearchResult>(
            """{"book_id":"1","rank_score":null,"match_tier":null}""",
        )

        assertNull(result.rank_score)
        assertNull(result.match_tier)
        assertNull(searchMatchBadge(result))
    }

    @Test
    fun eachTierMapsToItsShortLabel() {
        assertEquals(R.string.search_match_exact, matchTierLabelRes(4))
        assertEquals(R.string.search_match_prefix, matchTierLabelRes(3))
        assertEquals(R.string.search_match_contains, matchTierLabelRes(2))
        assertEquals(R.string.search_match_partial, matchTierLabelRes(1))
        assertEquals(R.string.search_match_none, matchTierLabelRes(0))
    }

    @Test
    fun tiersOutsideTheDocumentedRangeHaveNoLabel() {
        assertNull(matchTierLabelRes(-1))
        assertNull(matchTierLabelRes(5))
        assertNull(matchTierLabelRes(Int.MAX_VALUE))
        // A negative tier from a broken row must not demote to "no match".
        assertNull(searchMatchBadge(SearchResult(match_tier = -1)))
    }

    @Test
    fun scoresAreFormattedToTwoDecimals() {
        assertEquals("0.91", formatSearchRankScore(0.912345))
        assertEquals("0.90", formatSearchRankScore(0.9))
        assertEquals("1.00", formatSearchRankScore(1.0))
        assertEquals("0.00", formatSearchRankScore(0.0))
    }

    @Test
    fun malformedScoresAreTreatedAsAbsent() {
        assertNull(formatSearchRankScore(null))
        assertNull(formatSearchRankScore(Double.NaN))
        assertNull(formatSearchRankScore(Double.POSITIVE_INFINITY))
        assertNull(formatSearchRankScore(Double.NEGATIVE_INFINITY))
        assertNull(formatSearchRankScore(-0.01))
        assertNull(formatSearchRankScore(1.01))
    }

    @Test
    fun tierAndScoreProduceOneScoredBadge() {
        val badge = searchMatchBadge(SearchResult(match_tier = 3, rank_score = 0.755))

        assertEquals(R.string.search_match_prefix, badge!!.labelRes)
        assertEquals("0.76", badge.scoreText)
        assertEquals(R.string.search_match_desc_scored, badge.descriptionRes)
        assertTrue(badge.descriptionUsesScore)
        assertTrue(badge.labelArgs.isEmpty())
    }

    @Test
    fun aTierWithoutAScoreStillShowsTheBadge() {
        val badge = searchMatchBadge(SearchResult(match_tier = 4))

        assertEquals(R.string.search_match_exact, badge!!.labelRes)
        assertNull(badge.scoreText)
        assertEquals(R.string.search_match_desc, badge.descriptionRes)
        assertFalse(badge.descriptionUsesScore)
    }

    @Test
    fun aScoreWithoutATierFallsBackToARelevanceBadge() {
        val badge = searchMatchBadge(SearchResult(rank_score = 0.8))

        assertEquals(R.string.search_match_score_label, badge!!.labelRes)
        assertEquals(listOf("0.80"), badge.labelArgs)
        assertNull(badge.scoreText)
        assertEquals(R.string.search_match_score_label, badge.descriptionRes)
        assertFalse(badge.descriptionUsesScore)
    }

    @Test
    fun anOutOfRangeScoreDoesNotHideAValidTier() {
        val badge = searchMatchBadge(SearchResult(match_tier = 2, rank_score = 2.0))

        assertEquals(R.string.search_match_contains, badge!!.labelRes)
        assertNull(badge.scoreText)
    }

    @Test
    fun anUnknownTierWithAScoreUsesTheRelevanceBadge() {
        val badge = searchMatchBadge(SearchResult(match_tier = 9, rank_score = 0.5))

        assertEquals(R.string.search_match_score_label, badge!!.labelRes)
        assertEquals(listOf("0.50"), badge.labelArgs)
    }
}