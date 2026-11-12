package com.lingyi.noveldl.ui

import android.content.Context
import androidx.annotation.StringRes
import com.lingyi.noveldl.R
import com.lingyi.noveldl.data.ApiErrorCode
import com.lingyi.noveldl.data.ApiFailure

/**
 * Localizes an API failure. The data layer only reports a code (plus an
 * opaque detail such as the server's `detail` field or a socket message);
 * the wording lives in `strings.xml` for every supported language.
 *
 * `HTTP` keeps the detail verbatim because it is already a human string — the
 * server's own explanation, or `HTTP <code>` when the body had none.
 */
internal fun apiErrorMessage(context: Context, error: Throwable?): String {
    val failure = error as? ApiFailure ?: return error?.message ?: ""
    val resource = apiErrorRes(failure.code) ?: return failure.detail
    return if (failure.code in DETAIL_CODES) {
        context.getString(resource, failure.detail)
    } else {
        context.getString(resource)
    }
}

/** Localized text for a code reported out of band (e.g. "not configured yet"). */
internal fun apiErrorMessage(
    context: Context,
    code: ApiErrorCode,
    detail: String = "",
): String = apiErrorMessage(context, ApiFailure(code, detail))

/**
 * The string resource for [code], or null when the failure is rendered from
 * its own detail. Every code must either map here or be intentionally
 * detail-only; `ApiErrorsTest` pins that.
 */
@StringRes
internal fun apiErrorRes(code: ApiErrorCode): Int? = when (code) {
    ApiErrorCode.NOT_CONFIGURED -> R.string.api_error_not_configured
    ApiErrorCode.ENGINE_UNREACHABLE -> R.string.api_error_engine_unreachable
    ApiErrorCode.NETWORK -> R.string.api_error_network
    ApiErrorCode.TIMEOUT -> R.string.api_error_timeout
    ApiErrorCode.DNS -> R.string.api_error_dns
    ApiErrorCode.TLS -> R.string.api_error_tls
    ApiErrorCode.INSECURE_REDIRECT -> R.string.api_error_insecure_redirect
    ApiErrorCode.BAD_RESPONSE -> R.string.api_error_bad_response
    ApiErrorCode.UNKNOWN -> R.string.api_error_unknown
    ApiErrorCode.HTTP -> null
}

/** Codes whose localized text takes the failure detail as `%1$s`. */
private val DETAIL_CODES = setOf(
    ApiErrorCode.NETWORK,
    ApiErrorCode.TIMEOUT,
    ApiErrorCode.DNS,
    ApiErrorCode.TLS,
    ApiErrorCode.BAD_RESPONSE,
    ApiErrorCode.UNKNOWN,
)
