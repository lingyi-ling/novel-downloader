package com.lingyi.noveldl.ui.search

import androidx.compose.ui.semantics.Role
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Test

/**
 * Contract of the sort-key and direction chips. Material3's `FilterChip` is a
 * multi-select control that hard-codes `Role.Checkbox`; each sort group allows
 * exactly one active option, so the screen overrides the role with
 * `Role.RadioButton` (keeping the chip's own selected state). No Compose UI
 * test dependency is needed for that contract: the role is a plain value.
 */
class SearchSortChipSemanticsTest {

    @Test
    fun sortChipsAnnounceARadioButtonNotACheckbox() {
        assertEquals(Role.RadioButton, searchSortChipRole)
        assertNotEquals(Role.Checkbox, searchSortChipRole)
    }
}