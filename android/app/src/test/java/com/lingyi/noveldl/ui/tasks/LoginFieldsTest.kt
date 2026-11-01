package com.lingyi.noveldl.ui.tasks

import com.lingyi.noveldl.data.LoginField
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * JVM tests for the login-credential dialog rules: server prefill handling
 * (clamped, blank falls back to the default), the length cap that keeps the
 * single-line fields cheap to re-layout, and the trimmed submit payload.
 */
class LoginFieldsTest {

    private fun field(
        name: String = "cookie",
        required: Boolean = false,
        default: String = "",
    ) = LoginField(name = name, label = name, required = required, default = default)

    // ---- initial editor value ----

    @Test
    fun prefillWinsOverTheDefaultWhenItCarriesText() {
        val value = initialLoginFieldValue(
            field(default = "default"),
            mapOf("cookie" to "session=1"),
        )

        assertEquals("session=1", value)
    }

    @Test
    fun blankPrefillFallsBackToTheDefault() {
        assertEquals("default", initialLoginFieldValue(field(default = "default"), mapOf("cookie" to "   ")))
    }

    @Test
    fun missingPrefillFallsBackToTheDefault() {
        assertEquals("default", initialLoginFieldValue(field(default = "default"), emptyMap()))
    }

    @Test
    fun prefillForOtherFieldsDoesNotLeakIntoThisOne() {
        assertEquals("default", initialLoginFieldValue(field(default = "default"), mapOf("user" to "alice")))
    }

    @Test
    fun hugeServerPrefillIsClampedToTheLayoutSafeCap() {
        val hostile = "c".repeat(LOGIN_FIELD_MAX_LENGTH + 5000)

        val value = initialLoginFieldValue(field(), mapOf("cookie" to hostile))

        assertEquals(LOGIN_FIELD_MAX_LENGTH, value.length)
        assertEquals("c".repeat(LOGIN_FIELD_MAX_LENGTH), value)
    }

    @Test
    fun hugeDefaultIsClampedLikePrefill() {
        val value = initialLoginFieldValue(field(default = "d".repeat(LOGIN_FIELD_MAX_LENGTH + 1)), emptyMap())

        assertEquals(LOGIN_FIELD_MAX_LENGTH, value.length)
    }

    // ---- typed input clamping ----

    @Test
    fun shortTypedInputPassesThroughUnchanged() {
        assertEquals("abc", clampLoginFieldValue("abc"))
        assertEquals("", clampLoginFieldValue(""))
    }

    @Test
    fun typedInputAtTheCapIsKeptExactly() {
        val at = "x".repeat(LOGIN_FIELD_MAX_LENGTH)

        assertEquals(at, clampLoginFieldValue(at))
    }

    @Test
    fun pastedInputBeyondTheCapIsTruncatedToTheCap() {
        val value = clampLoginFieldValue("x".repeat(10_000))

        assertEquals(LOGIN_FIELD_MAX_LENGTH, value.length)
        assertEquals("x".repeat(LOGIN_FIELD_MAX_LENGTH), value)
    }

    // ---- submit readiness ----

    @Test
    fun submitRequiresNonBlankAnswersForRequiredFieldsOnly() {
        val fields = listOf(
            field(name = "user", required = true),
            field(name = "remember", required = false),
        )

        assertFalse(loginSubmitReady(fields, emptyMap()))
        assertFalse(loginSubmitReady(fields, mapOf("user" to "")))
        assertFalse(loginSubmitReady(fields, mapOf("user" to "   ")))
        assertTrue(loginSubmitReady(fields, mapOf("user" to "alice")))
        // Optional fields never gate the button.
        assertTrue(loginSubmitReady(fields, mapOf("user" to "alice", "remember" to "")))
    }

    @Test
    fun submitIsReadyWithoutAnyRequiredField() {
        assertTrue(loginSubmitReady(emptyList(), emptyMap()))
        assertTrue(loginSubmitReady(listOf(field(name = "cookie")), emptyMap()))
    }

    // ---- submitted payload ----

    @Test
    fun submittedValuesAreTrimmedPerAnnouncedField() {
        val fields = listOf(field(name = "cookie"), field(name = "user", required = true))

        val values = loginSubmitValues(
            fields,
            mapOf("cookie" to "  session=1\n", "user" to " alice ", "rogue" to "x"),
        )

        // Unknown keys are dropped; a pasted newline cannot poison a cookie.
        assertEquals(mapOf("cookie" to "session=1", "user" to "alice"), values)
    }

    @Test
    fun submittedValuesMissingFromTheBufferAreSentBlank() {
        assertEquals(mapOf("cookie" to ""), loginSubmitValues(listOf(field()), emptyMap()))
    }
}
