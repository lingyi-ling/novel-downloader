package com.lingyi.noveldl.data

/**
 * Machine-readable cause of a failed API call.
 *
 * The data layer must not own user-facing wording: every failure carries a
 * code here, and the UI maps it to a `strings.xml` entry (Chinese and
 * English). [ApiFailure.detail] stays transport/server supplied (an HTTP
 * `detail` field, a socket message, a decoder message) and is only used as
 * the `%s` argument of the localized text — never as the text itself.
 */
enum class ApiErrorCode {
    /** No API address was configured yet (the settings screen was not saved). */
    NOT_CONFIGURED,

    /**
     * The on-device engine's loopback port is not reachable: the Python
     * bootstrap is still starting, or it crashed. Distinct from an
     * unreachable remote address, which is a configuration problem.
     */
    ENGINE_UNREACHABLE,

    /** The request could not be delivered (DNS, refused, timeout, TLS...). */
    NETWORK,

    /** A 2xx response whose body could not be decoded as the expected JSON. */
    BAD_RESPONSE,

    /**
     * A non-2xx response. [ApiFailure.detail] is the server's `detail` text
     * when present, otherwise `HTTP <code>`.
     */
    HTTP,

    /** Anything else; the detail is the exception message or class name. */
    UNKNOWN,
}

/**
 * Failure of an API call, carrying the cause as an [ApiErrorCode].
 *
 * It extends [IllegalStateException] so existing `Result`-based call sites
 * keep working, and its [message] is [detail] (or the code name when the
 * detail is empty) so server-supplied texts such as `"task already queued"`
 * still reach callers that only look at the message.
 */
class ApiFailure(
    val code: ApiErrorCode,
    val detail: String = "",
    cause: Throwable? = null,
) : IllegalStateException(detail.ifBlank { code.name }, cause)
