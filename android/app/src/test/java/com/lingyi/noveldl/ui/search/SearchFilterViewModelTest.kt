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
    fun eachSortKeyStartsWithItsNaturalDirection() {
        // Mirrors the desktop search bar: quality/words/update newest or
        // longest first, site priority best first.
        val expected = mapOf(
            SearchSort.QUALITY to SearchOrder.DESC,
            SearchSort.PRIORITY to SearchOrder.ASC,
            SearchSort.WORDS to SearchOrder.DESC,
            SearchSort.UPDATE to SearchOrder.DESC,
        )
        assertEquals(SearchSort.entries.toSet(), expected.keys)

        expected.forEach { (sort, natural) ->
            val saved = SavedStateHandle()
            val model = SearchFilterViewModel(savedState = saved)
            // Seed another key when the target is already active: selecting
            // the active key is a no-op by design, so the loop must exercise a
            // real key change.
            if (model.state.value.sort == sort) {
                model.updateSort(
                    if (sort == SearchSort.QUALITY) SearchSort.UPDATE else SearchSort.QUALITY,
                )
            }
            // Flip first, so the reset is observable even when the natural
            // direction happens to be the default `desc`.
            model.updateOrder(natural.flipped())

            model.updateSort(sort)

            assertEquals(sort, model.state.value.sort)
            assertEquals("sort=${sort.wire}", natural, model.state.value.order)
            assertEquals(sort.wire, saved.get<String>(SearchFilterViewModel.SAVED_SORT))
            assertEquals(
                "sort=${sort.wire}",
                natural.wire,
                saved.get<String>(SearchFilterViewModel.SAVED_ORDER),
            )
            // The request and a later restore both keep the pair.
            assertEquals(
                "sort=${sort.wire}",
                natural.wire,
                model.state.value.toSearchRequest("x")?.order,
            )
            assertEquals(
                sort,
                SearchFilterViewModel(savedState = saved).state.value.sort,
            )
        }
    }

    @Test
    fun switchingTheSortKeyOverridesAManualDirection() {
        val model = SearchFilterViewModel(savedState = SavedStateHandle())
        model.updateOrder(SearchOrder.ASC)
        assertEquals(SearchOrder.ASC, model.state.value.order)

        // Selecting a different key is a fresh start: words is natural `desc`.
        model.updateSort(SearchSort.WORDS)

        assertEquals(SearchFilterState(sort = SearchSort.WORDS), model.state.value)
    }

    @Test
    fun reSelectingTheActiveSortKeyKeepsTheManualDirection() {
        val saved = SavedStateHandle()
        val model = SearchFilterViewModel(savedState = saved)
        model.updateSort(SearchSort.PRIORITY)
        assertEquals(SearchOrder.ASC, model.state.value.order)

        model.updateOrder(SearchOrder.DESC)
        // A second tap on the already-selected chip (which Compose still
        // delivers) must not silently undo the flip.
        model.updateSort(SearchSort.PRIORITY)

        assertEquals(SearchSort.PRIORITY, model.state.value.sort)
        assertEquals(SearchOrder.DESC, model.state.value.order)
        assertEquals("desc", saved.get<String>(SearchFilterViewModel.SAVED_ORDER))
    }

    @Test
    fun updateOrderStaysManualAcrossUnrelatedEdits() {
        val saved = SavedStateHandle()
        val model = SearchFilterViewModel(savedState = saved)
        model.updateSort(SearchSort.UPDATE)
        model.updateOrder(SearchOrder.ASC)

        model.updateMinWords("1000")
        model.updateMaxWords("5000")
        model.updateUpdatedAfter("2024-05-01")

        assertEquals(SearchOrder.ASC, model.state.value.order)
        assertEquals("asc", saved.get<String>(SearchFilterViewModel.SAVED_ORDER))
    }

    @Test
    fun eachSortKeyRestoresTheSavedDirectionEvenWhenItIsNotNatural() {
        // A direction the user flipped by hand is never re-normalised, so a
        // process-death restore looks exactly like the panel they left.
        SearchSort.entries.forEach { sort ->
            val manual = sort.naturalOrder.flipped()
            val saved = SavedStateHandle(
                mapOf(
                    SearchFilterViewModel.SAVED_SORT to sort.wire,
                    SearchFilterViewModel.SAVED_ORDER to manual.wire,
                ),
            )

            val restored = SearchFilterViewModel(savedState = saved)

            assertEquals(sort, restored.state.value.sort)
            assertEquals("sort=${sort.wire}", manual, restored.state.value.order)
            assertEquals(
                "sort=${sort.wire}",
                manual.wire,
                restored.state.value.toSearchRequest("x")?.order,
            )
        }
    }

    @Test
    fun aMissingSavedDirectionFallsBackToTheRestoredKeysNaturalOrder() {
        SearchSort.entries.forEach { sort ->
            // An older build (or a corrupt handle) may have saved only the key.
            val saved = SavedStateHandle(
                mapOf(SearchFilterViewModel.SAVED_SORT to sort.wire),
            )

            val restored = SearchFilterViewModel(savedState = saved)

            assertEquals(sort, restored.state.value.sort)
            assertEquals("sort=${sort.wire}", sort.naturalOrder, restored.state.value.order)
        }
    }

    @Test
    fun anUnknownSavedDirectionFallsBackToTheRestoredKeysNaturalOrder() {
        val saved = SavedStateHandle(
            mapOf(
                SearchFilterViewModel.SAVED_SORT to "priority",
                SearchFilterViewModel.SAVED_ORDER to "sideways",
            ),
        )

        val restored = SearchFilterViewModel(savedState = saved)

        assertEquals(SearchSort.PRIORITY, restored.state.value.sort)
        assertEquals(SearchOrder.ASC, restored.state.value.order)
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

    private fun SearchOrder.flipped(): SearchOrder = when (this) {
        SearchOrder.ASC -> SearchOrder.DESC
        SearchOrder.DESC -> SearchOrder.ASC
    }
}
