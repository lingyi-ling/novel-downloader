package com.lingyi.noveldl.ui

import org.junit.Assert.assertEquals
import org.junit.Test

/** JVM tests for the persistent connection line on the settings screen. */
class HealthSummaryTest {

    @Test
    fun joinsStatusAndVersion() {
        assertEquals("ok · 1.2.0", healthSummary("ok", "1.2.0", "unknown"))
    }

    @Test
    fun trimsWhitespaceOnBothSides() {
        assertEquals("ok · 1.2.0", healthSummary(" ok ", " 1.2.0 ", "unknown"))
    }

    @Test
    fun missingVersionKeepsOnlyStatus() {
        assertEquals("ok", healthSummary("ok", "", "unknown"))
        assertEquals("ok", healthSummary("ok", "   ", "unknown"))
    }

    @Test
    fun missingStatusKeepsOnlyVersion() {
        assertEquals("1.2.0", healthSummary("", "1.2.0", "unknown"))
    }

    @Test
    fun everythingBlankReadsAsTheCallersFallback() {
        assertEquals("unknown", healthSummary("", "", "unknown"))
        assertEquals("unknown", healthSummary("   ", "  ", "unknown"))
    }

    @Test
    fun fallbackCanBeLocalized() {
        assertEquals("未知", healthSummary("", "", "未知"))
    }
}
