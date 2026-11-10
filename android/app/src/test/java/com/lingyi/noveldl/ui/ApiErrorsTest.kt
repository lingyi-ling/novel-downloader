package com.lingyi.noveldl.ui

import com.lingyi.noveldl.R
import com.lingyi.noveldl.data.ApiErrorCode
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Every transport failure the data layer can report must be localizable.
 *
 * The data layer used to carry Chinese sentences, which English builds showed
 * verbatim (and made new error paths easy to add without a translation). The
 * codes are mapped here, and this test fails when a code is added without a
 * deliberate mapping.
 */
class ApiErrorsTest {

    @Test
    fun configurationAndEngineProblemsHaveDedicatedTexts() {
        assertEquals(
            R.string.api_error_not_configured,
            apiErrorRes(ApiErrorCode.NOT_CONFIGURED),
        )
        assertEquals(
            R.string.api_error_engine_unreachable,
            apiErrorRes(ApiErrorCode.ENGINE_UNREACHABLE),
        )
    }

    @Test
    fun transportAndBodyProblemsUseTheDetailTemplate() {
        assertEquals(
            R.string.api_error_network,
            apiErrorRes(ApiErrorCode.NETWORK),
        )
        assertEquals(
            R.string.api_error_bad_response,
            apiErrorRes(ApiErrorCode.BAD_RESPONSE),
        )
        assertEquals(
            R.string.api_error_unknown,
            apiErrorRes(ApiErrorCode.UNKNOWN),
        )
    }

    @Test
    fun httpFailuresAreRenderedFromTheirOwnDetail() {
        // The server's `detail` (or "HTTP <code>") is already human text.
        assertNull(apiErrorRes(ApiErrorCode.HTTP))
    }

    @Test
    fun everyCodeIsEitherLocalizedOrDetailOnly() {
        // Pin the enum size: adding a code must come with a decision here.
        assertEquals(6, ApiErrorCode.entries.size)
        ApiErrorCode.entries.forEach { code ->
            val mapped = apiErrorRes(code) != null
            assertTrue(
                "$code has no resource and is not detail-only",
                mapped || code == ApiErrorCode.HTTP,
            )
        }
    }
}
