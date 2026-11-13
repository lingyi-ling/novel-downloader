package com.lingyi.noveldl.ui.search

import com.lingyi.noveldl.data.SearchRequest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.LocalDate

/**
 * Pure contract of the optional search filters: what counts as valid, what
 * reaches `POST /api/v1/search`, and how the date picker's UTC millis map to
 * the API's ISO date.
 */
class SearchFiltersTest {

    @Test
    fun emptyFiltersProduceAnUnfilteredRequest() {
        val request = SearchFilterState().toSearchRequest("斗破苍穹", listOf("qidian"))

        assertNotNull(request)
        assertEquals("斗破苍穹", request!!.keyword)
        assertEquals(listOf("qidian"), request.sites)
        assertNull(request.min_words)
        assertNull(request.max_words)
        assertNull(request.updated_after)
        assertFalse(SearchFilterState().isActive)
    }

    @Test
    fun whitespaceOnlyFiltersAreTreatedAsBlank() {
        val state = SearchFilterState(minWords = "   ", maxWords = "\t", updatedAfter = "  ")
        val request = state.toSearchRequest("x")

        assertNotNull(request)
        assertNull(request!!.min_words)
        assertNull(request.max_words)
        assertNull(request.updated_after)
        assertTrue(state.validate().isValid)
        assertEquals(0, state.activeFilterCount)
    }

    @Test
    fun zeroAndTheServerMaximumAreAccepted() {
        val request = SearchFilterState(
            minWords = "0",
            maxWords = MAX_SEARCH_WORDS.toString(),
        ).toSearchRequest("x")

        assertNotNull(request)
        assertEquals(0, request!!.min_words)
        assertEquals(MAX_SEARCH_WORDS, request.max_words)
    }

    @Test
    fun equalBoundsAreAccepted() {
        val state = SearchFilterState(minWords = "1000", maxWords = "1000")

        assertTrue(state.validate().isValid)
        assertEquals(1000, state.toSearchRequest("x")!!.min_words)
        assertEquals(1000, state.toSearchRequest("x")!!.max_words)
    }

    @Test
    fun invalidWordEntriesAreReportedPerFieldAndNeverSent() {
        val cases = listOf(
            "abc" to WordFilterError.NOT_A_NUMBER,
            "1.5" to WordFilterError.NOT_A_NUMBER,
            "1_000" to WordFilterError.NOT_A_NUMBER,
            "-1" to WordFilterError.NEGATIVE,
            "1000000001" to WordFilterError.TOO_LARGE,
            "999999999999999999999999" to WordFilterError.TOO_LARGE,
        )
        cases.forEach { (text, expected) ->
            val state = SearchFilterState(minWords = text)
            val validation = state.validate()
            assertEquals("min=$text", expected, validation.minWordsError)
            assertFalse("min=$text", validation.isValid)
            assertNull("min=$text", state.toSearchRequest("x"))
        }

        val maxState = SearchFilterState(maxWords = "-5")
        assertEquals(WordFilterError.NEGATIVE, maxState.validate().maxWordsError)
        assertNull(maxState.toSearchRequest("x"))
    }

    @Test
    fun minGreaterThanMaxIsRejectedEvenWhenBothParse() {
        val state = SearchFilterState(minWords = "5000", maxWords = "1000")
        val validation = state.validate()

        assertTrue(validation.rangeError)
        assertFalse(validation.isValid)
        assertNull(validation.minWordsError)
        assertNull(validation.maxWordsError)
        assertNull(state.toSearchRequest("x"))
    }

    @Test
    fun aBrokenMinimumDoesNotAlsoClaimARangeError() {
        val validation = SearchFilterState(minWords = "abc", maxWords = "100").validate()

        assertEquals(WordFilterError.NOT_A_NUMBER, validation.minWordsError)
        assertFalse(validation.rangeError)
    }

    @Test
    fun validIsoDatesAreForwardedIncludingLeapDays() {
        assertEquals(
            "2024-02-29",
            SearchFilterState(updatedAfter = "2024-02-29").toSearchRequest("x")!!.updated_after,
        )
        assertEquals(
            "2024-01-01",
            SearchFilterState(updatedAfter = " 2024-01-01 ").toSearchRequest("x")!!.updated_after,
        )
    }

    @Test
    fun malformedDatesAreReportedAndNeverSent() {
        listOf(
            "2024/01/01",
            "20240101",
            "2024-1-1",
            "2024-13-01",
            "2024-02-30",
            "0000-01-01",
            "soon",
        ).forEach { text ->
            val state = SearchFilterState(updatedAfter = text)
            assertEquals("date=$text", DateFilterError.INVALID, state.validate().updatedAfterError)
            assertFalse("date=$text", state.validate().isValid)
            assertNull("date=$text", state.toSearchRequest("x"))
        }
    }

    @Test
    fun activeCountIncludesInvalidNonBlankEntries() {
        val state = SearchFilterState(minWords = "abc", updatedAfter = "not-a-date")

        assertEquals(2, state.activeFilterCount)
        assertTrue(state.isActive)
    }

    @Test
    fun pickerMillisRoundTripThroughTheSameUtcDay() {
        val date = LocalDate.of(2024, 2, 29)
        val millis = filterDateEpochMillis(date.toString())

        assertNotNull(millis)
        assertEquals(date.toString(), formatFilterDate(millis!!))
        // Material3 reports midnight UTC, not the device zone's midnight.
        assertEquals(0L, millis % 86_400_000L)
    }

    @Test
    fun invalidOrBlankDatesHaveNoPickerValue() {
        assertNull(filterDateEpochMillis(""))
        assertNull(filterDateEpochMillis("2024-02-30"))
    }

    @Test
    fun requestCarriesSitesAndEveryFilterTogether() {
        val state = SearchFilterState(
            minWords = "100000",
            maxWords = "2000000",
            updatedAfter = "2025-01-01",
        )
        val request = state.toSearchRequest("x", listOf("qidian", "fanqienovel"))

        assertEquals(
            SearchRequest(
                keyword = "x",
                sites = listOf("qidian", "fanqienovel"),
                min_words = 100000,
                max_words = 2000000,
                updated_after = "2025-01-01",
            ),
            request,
        )
    }

    @Test
    fun theDefaultStateIsEmptyAndValid() {
        assertEquals(SearchFilterState.EMPTY, SearchFilterState())
        assertEquals(0, SearchFilterState.EMPTY.activeFilterCount)
        assertFalse(SearchFilterState.EMPTY.isActive)
        assertTrue(SearchFilterState.EMPTY.validate().isValid)
    }
}
