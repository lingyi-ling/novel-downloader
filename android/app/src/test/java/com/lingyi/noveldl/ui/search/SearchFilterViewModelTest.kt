package com.lingyi.noveldl.ui.search

import androidx.lifecycle.SavedStateHandle
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Process-death contract of the filter owner: every edit is mirrored into the
 * [SavedStateHandle], a fresh instance restores it, and `clear()` resets both
 * the visible state and the saved values.
 */
class SearchFilterViewModelTest {

    @Test
    fun startsEmptyWithoutSavedState() {
        val model = SearchFilterViewModel()

        assertEquals(SearchFilterState.EMPTY, model.state.value)
        assertFalse(model.state.value.isActive)
    }

    @Test
    fun editsAreWrittenToSavedState() {
        val saved = SavedStateHandle()
        val model = SearchFilterViewModel(savedState = saved)

        model.updateMinWords("100")
        model.updateMaxWords("2000")
        model.updateUpdatedAfter("2024-01-31")

        assertEquals(
            SearchFilterState(
                minWords = "100",
                maxWords = "2000",
                updatedAfter = "2024-01-31",
            ),
            model.state.value,
        )
        assertEquals("100", saved.get<String>(SearchFilterViewModel.SAVED_MIN_WORDS))
        assertEquals("2000", saved.get<String>(SearchFilterViewModel.SAVED_MAX_WORDS))
        assertEquals(
            "2024-01-31",
            saved.get<String>(SearchFilterViewModel.SAVED_UPDATED_AFTER),
        )
    }

    @Test
    fun aNewInstanceRestoresEveryFilterFromSavedState() {
        val saved = SavedStateHandle(
            mapOf(
                SearchFilterViewModel.SAVED_MIN_WORDS to "50000",
                SearchFilterViewModel.SAVED_MAX_WORDS to "1500000",
                SearchFilterViewModel.SAVED_UPDATED_AFTER to "2024-06-01",
            ),
        )

        val restored = SearchFilterViewModel(savedState = saved)

        assertEquals(
            SearchFilterState(
                minWords = "50000",
                maxWords = "1500000",
                updatedAfter = "2024-06-01",
            ),
            restored.state.value,
        )
        assertEquals(3, restored.state.value.activeFilterCount)
    }

    @Test
    fun restoredTextStaysEditableAndTheDateRoundTrips() {
        val saved = SavedStateHandle(
            mapOf(
                SearchFilterViewModel.SAVED_MIN_WORDS to "abc",
                SearchFilterViewModel.SAVED_UPDATED_AFTER to " 2024-02-29 ",
            ),
        )
        val model = SearchFilterViewModel(savedState = saved)

        // Invalid text is kept so the user can correct it; the canonical date
        // is normalized on the way in.
        assertEquals("abc", model.state.value.minWords)
        assertEquals("2024-02-29", model.state.value.updatedAfter)
        assertFalse(model.state.value.validate().isValid)

        model.updateMinWords("10")
        assertEquals("10", saved.get<String>(SearchFilterViewModel.SAVED_MIN_WORDS))
        assertTrue(model.state.value.validate().isValid)
    }

    @Test
    fun aMalformedSavedDateIsDroppedInsteadOfBlockingTheScreen() {
        val saved = SavedStateHandle(
            mapOf(
                SearchFilterViewModel.SAVED_MIN_WORDS to "10",
                SearchFilterViewModel.SAVED_UPDATED_AFTER to "2024-13-40",
            ),
        )

        val model = SearchFilterViewModel(savedState = saved)

        assertEquals("10", model.state.value.minWords)
        assertEquals("", model.state.value.updatedAfter)
        assertTrue(model.state.value.validate().isValid)
    }

    @Test
    fun sortAndOrderEditsAreWrittenToSavedState() {
        val saved = SavedStateHandle()
        val model = SearchFilterViewModel(savedState = saved)

        model.updateSort(SearchSort.PRIORITY)
        model.updateOrder(SearchOrder.ASC)

        assertEquals(SearchSort.PRIORITY, model.state.value.sort)
        assertEquals(SearchOrder.ASC, model.state.value.order)
        assertEquals("priority", saved.get<String>(SearchFilterViewModel.SAVED_SORT))
        assertEquals("asc", saved.get<String>(SearchFilterViewModel.SAVED_ORDER))
        // Ordering is presentation, not a filter; the active count must not
        // claim that a filter is narrowing the results.
        assertEquals(0, model.state.value.activeFilterCount)
        assertFalse(model.state.value.isActive)
    }

    @Test
    fun aNewInstanceRestoresSortAndOrder() {
        val saved = SavedStateHandle(
            mapOf(
                SearchFilterViewModel.SAVED_SORT to "words",
                SearchFilterViewModel.SAVED_ORDER to "asc",
            ),
        )

        val restored = SearchFilterViewModel(savedState = saved)

        assertEquals(SearchSort.WORDS, restored.state.value.sort)
        assertEquals(SearchOrder.ASC, restored.state.value.order)
        // The request built after a process-death restore sends the same sort.
        assertEquals("words", restored.state.value.toSearchRequest("x")?.sort)
        assertEquals("asc", restored.state.value.toSearchRequest("x")?.order)
    }

    @Test
    fun anUnknownSavedSortOrOrderFallsBackToTheDefault() {
        val saved = SavedStateHandle(
            mapOf(
                SearchFilterViewModel.SAVED_MIN_WORDS to "10",
                SearchFilterViewModel.SAVED_SORT to "relevance",
                SearchFilterViewModel.SAVED_ORDER to "sideways",
            ),
        )

        val model = SearchFilterViewModel(savedState = saved)

        assertEquals(SearchSort.QUALITY, model.state.value.sort)
        assertEquals(SearchOrder.DESC, model.state.value.order)
        // The legal filter beside the corrupt values is untouched, and the
        // screen is usable instead of being stuck on an invalid request.
        assertEquals("10", model.state.value.minWords)
        assertTrue(model.state.value.validate().isValid)
    }

    @Test
    fun clearRestoresTheDefaultQualitySortAndDirection() {
        val saved = SavedStateHandle()
        val model = SearchFilterViewModel(savedState = saved)
        model.updateSort(SearchSort.UPDATE)
        model.updateOrder(SearchOrder.ASC)

        model.clear()

        assertEquals(SearchSort.QUALITY, model.state.value.sort)
        assertEquals(SearchOrder.DESC, model.state.value.order)
        assertEquals("quality", saved.get<String>(SearchFilterViewModel.SAVED_SORT))
        assertEquals("desc", saved.get<String>(SearchFilterViewModel.SAVED_ORDER))
        assertEquals(
            SearchFilterState.EMPTY,
            SearchFilterViewModel(savedState = saved).state.value,
        )
    }

    @Test
    fun clearResetsTheStateAndTheSavedValues() {
        val saved = SavedStateHandle()
        val model = SearchFilterViewModel(savedState = saved)
        model.updateMinWords("100")
        model.updateMaxWords("200")
        model.updateUpdatedAfter("2024-01-01")

        model.clear()

        assertEquals(SearchFilterState.EMPTY, model.state.value)
        assertEquals("", saved.get<String>(SearchFilterViewModel.SAVED_MIN_WORDS))
        assertEquals("", saved.get<String>(SearchFilterViewModel.SAVED_MAX_WORDS))
        assertEquals("", saved.get<String>(SearchFilterViewModel.SAVED_UPDATED_AFTER))
        assertEquals(SearchSort.QUALITY, model.state.value.sort)
        assertEquals(SearchOrder.DESC, model.state.value.order)

        // A new instance after the clear stays empty too.
        assertEquals(
            SearchFilterState.EMPTY,
            SearchFilterViewModel(savedState = saved).state.value,
        )
    }

    @Test
    fun clearingAnAlreadyEmptyStateIsANoOp() {
        val saved = SavedStateHandle()
        val model = SearchFilterViewModel(savedState = saved)

        model.clear()

        assertEquals(SearchFilterState.EMPTY, model.state.value)
        // Nothing was written: the handle still has no keys for the filters.
        assertFalse(saved.contains(SearchFilterViewModel.SAVED_MIN_WORDS))
        assertFalse(saved.contains(SearchFilterViewModel.SAVED_SORT))
        assertFalse(saved.contains(SearchFilterViewModel.SAVED_ORDER))
    }
}
