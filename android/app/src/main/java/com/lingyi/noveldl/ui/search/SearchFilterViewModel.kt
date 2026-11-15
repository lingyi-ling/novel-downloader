package com.lingyi.noveldl.ui.search

import androidx.lifecycle.SavedStateHandle
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.createSavedStateHandle
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * Owns the optional word-count/update-date filters and the result ordering of
 * the search screen.
 *
 * Why a ViewModel: the screen can be dropped from composition by a tab switch,
 * and the process can be killed in the background. Keeping the filter text in
 * [SavedStateHandle] means both events restore the panel exactly as the user
 * left it, instead of silently returning to an unfiltered search. The search
 * coroutine reads [state] when it builds its request, so a re-search, retry or
 * any later page request carries the same filters and sort direction without
 * extra plumbing.
 *
 * Saved values are strings (not `Int`/`LocalDate`) because the raw input is
 * what lets an invalid entry be corrected in place; [toSearchRequest] still
 * refuses to send it. A corrupt/legacy date is dropped on restore rather than
 * blocking the screen, while text values are preserved verbatim.
 */
class SearchFilterViewModel @JvmOverloads constructor(
    private val savedState: SavedStateHandle? = null,
) : ViewModel() {

    private val _state = MutableStateFlow(restore(savedState))
    val state: StateFlow<SearchFilterState> = _state.asStateFlow()

    fun updateMinWords(value: String) {
        update(_state.value.copy(minWords = value))
    }

    fun updateMaxWords(value: String) {
        update(_state.value.copy(maxWords = value))
    }

    /** [date] is an ISO `YYYY-MM-DD` value or blank to turn the filter off. */
    fun updateUpdatedAfter(date: String) {
        update(_state.value.copy(updatedAfter = date.trim()))
    }

    /**
     * Selects a sort key. A newly selected key starts in its natural
     * direction ([SearchSort.naturalOrder]), matching the desktop search bar
     * so `priority` does not open with the worst sites first. Re-selecting the
     * active key is a no-op, so a direction the user flipped by hand survives
     * an accidental tap on the same chip.
     */
    fun updateSort(sort: SearchSort) {
        val current = _state.value
        if (current.sort == sort) return
        update(current.copy(sort = sort, order = sort.naturalOrder))
    }

    /** Explicit direction choice; always honoured until the key changes. */
    fun updateOrder(order: SearchOrder) {
        update(_state.value.copy(order = order))
    }

    /**
     * Restores every filter to its default (no bounds, no date) and the
     * ordering to the default quality ranking, newest/highest first.
     */
    fun clear() {
        update(SearchFilterState.EMPTY)
    }

    private fun update(next: SearchFilterState) {
        val previous = _state.value
        if (next == previous) return
        _state.value = next
        if (next.minWords != previous.minWords) {
            savedState?.set(SAVED_MIN_WORDS, next.minWords)
        }
        if (next.maxWords != previous.maxWords) {
            savedState?.set(SAVED_MAX_WORDS, next.maxWords)
        }
        if (next.updatedAfter != previous.updatedAfter) {
            savedState?.set(SAVED_UPDATED_AFTER, next.updatedAfter)
        }
        if (next.sort != previous.sort) {
            savedState?.set(SAVED_SORT, next.sort.wire)
        }
        if (next.order != previous.order) {
            savedState?.set(SAVED_ORDER, next.order.wire)
        }
    }

    companion object {
        internal const val SAVED_MIN_WORDS = "search.min_words"
        internal const val SAVED_MAX_WORDS = "search.max_words"
        internal const val SAVED_UPDATED_AFTER = "search.updated_after"
        internal const val SAVED_SORT = "search.sort"
        internal const val SAVED_ORDER = "search.order"

        /**
         * NavBackStackEntry/Activity-scoped factory. `createSavedStateHandle()`
         * receives the destination's saved instance state, so the filters come
         * back after process death as well as after a tab switch.
         */
        fun factory(): ViewModelProvider.Factory = viewModelFactory {
            initializer { SearchFilterViewModel(createSavedStateHandle()) }
        }

        private fun restore(savedState: SavedStateHandle?): SearchFilterState {
            if (savedState == null) return SearchFilterState.EMPTY
            // Anything outside the server whitelist (an old or corrupt value)
            // falls back to the default instead of being sent.
            val sort = SearchSort.fromWire(savedState.get<String>(SAVED_SORT))
            return SearchFilterState(
                minWords = savedState.get<String>(SAVED_MIN_WORDS).orEmpty(),
                maxWords = savedState.get<String>(SAVED_MAX_WORDS).orEmpty(),
                // Canonicalize a valid date and drop a malformed one written by
                // an older/broken build instead of showing a permanent error.
                updatedAfter = parseFilterDate(
                    savedState.get<String>(SAVED_UPDATED_AFTER).orEmpty(),
                )?.toString().orEmpty(),
                sort = sort,
                // A saved direction always wins, including one the user flipped
                // by hand. Only an absent/unknown value falls back to the
                // restored key's natural direction (an older build that only
                // saved the key lands on the new default instead of `desc`).
                order = SearchOrder.fromWireOrNull(savedState.get<String>(SAVED_ORDER))
                    ?: sort.naturalOrder,
            )
        }
    }
}
