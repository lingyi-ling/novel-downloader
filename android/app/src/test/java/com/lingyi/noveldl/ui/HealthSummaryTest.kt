package com.lingyi.noveldl.ui

import org.junit.Assert.assertEquals
import org.junit.Test

/** JVM tests for the persistent connection line on the settings screen. */
class HealthSummaryTest {

    @Test
    fun joinsStatusAndVersion() {
        assertEquals("ok · 1.2.0", healthSummary("ok", "1.2.0"))
    }

    @Test
    fun trimsWhitespaceOnBothSides() {
        assertEquals("ok · 1.2.0", healthSummary(" ok ", " 1.2.0 "))
    }

    @Test
    fun missingVersionKeepsOnlyStatus() {
        assertEquals("ok", healthSummary("ok", ""))
        assertEquals("ok", healthSummary("ok", "   "))
    }

    @Test
    fun missingStatusKeepsOnlyVersion() {
        assertEquals("1.2.0", healthSummary("", "1.2.0"))
    }

    @Test
    fun everythingBlankReadsAsUnknown() {
        assertEquals("unknown", healthSummary("", ""))
        assertEquals("unknown", healthSummary("   ", "  "))
    }
}
