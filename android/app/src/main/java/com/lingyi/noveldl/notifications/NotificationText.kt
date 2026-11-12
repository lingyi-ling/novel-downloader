package com.lingyi.noveldl.notifications

/**
 * Pure text preparation for task notifications.
 *
 * Task titles come from remote sites, so they are untrusted input: a title can
 * carry credentials copied out of a URL, contain line breaks or bidirectional
 * control characters that break the collapsed notification layout, or be long
 * enough to dominate the shade. Android itself only truncates at render time,
 * so the limits are applied here, before the text reaches the builder.
 *
 * The object is deliberately Android-free: everything stays unit-testable on
 * the JVM (the app's test suite has no Robolectric runtime).
 */
object NotificationText {

    /** Longest visible title kept inside a notification body. */
    const val MAX_TITLE_CHARS: Int = 120

    private const val ELLIPSIS = '\u2026'

    /** Whitespace after control characters were neutralized. */
    private val WHITESPACE = Regex("[\\s\\u00A0\\u2000-\\u200A\\u202F\\u205F\\u3000]+")

    /** Credentials embedded in a URL authority (`https://user:pass@host`). */
    private val URL_USERINFO = Regex("""(?i)\b([a-z][a-z0-9+.\-]*://)[^\s/@]+@""")

    /** Secret-looking query parameters (`?token=...`, `&key=...`). */
    private val SECRET_QUERY = Regex(
        """(?i)([?&](?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|""" +
            """password|passwd|pwd|auth|authorization|signature|sig|key)=)[^&\s]+""",
    )

    /** `token=...` / `Authorization: ...` style assignments. */
    private val SECRET_ASSIGNMENT = Regex(
        """(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|""" +
            """password|passwd|pwd|authorization|signature)\b\s*[=:]\s*[^\s&]+""",
    )

    /** `Bearer <token>` / `Basic <token>` scheme credentials. */
    private val BEARER_TOKEN = Regex("""(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}""")

    /** Well-known token shapes that need no key/value pair. */
    private val KNOWN_TOKEN = Regex(
        """\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|""" +
            """sk-[A-Za-z0-9_\-]{16,}|AKIA[0-9A-Z]{16})\b""",
    )

    /**
     * Returns a single-line, redacted, length-limited title. [fallback] is used
     * when the remote title is empty or consists only of control characters.
     */
    fun taskTitle(raw: String, fallback: String): String {
        val cleaned = truncate(redact(neutralize(raw)).replace(WHITESPACE, " ").trim())
        return cleaned.ifBlank { fallback }
    }

    /** Turns control characters into spaces and drops formatting (bidi) marks. */
    private fun neutralize(raw: String): String = buildString(raw.length) {
        for (char in raw) {
            when {
                Character.getType(char) == Character.FORMAT.toInt() -> Unit
                Character.isISOControl(char) || Character.isWhitespace(char) -> append(' ')
                else -> append(char)
            }
        }
    }

    /** Masks credentials that a remote title may have leaked into its text. */
    private fun redact(text: String): String = text
        .replace(URL_USERINFO, "${'$'}1")
        // Before the assignment rule, which consumes the "Authorization:" key
        // and would leave the token itself behind.
        .replace(BEARER_TOKEN, "${'$'}1 ***")
        .replace(SECRET_QUERY, "${'$'}1***")
        .replace(SECRET_ASSIGNMENT, "${'$'}1=***")
        .replace(KNOWN_TOKEN, "***")

    /**
     * Cuts [text] to at most [maxChars] characters, ending in an ellipsis. A
     * surrogate pair is never split, so emoji in a title cannot turn into a
     * replacement glyph.
     */
    fun truncate(text: String, maxChars: Int = MAX_TITLE_CHARS): String {
        require(maxChars > 1) { "maxChars must be greater than 1" }
        if (text.length <= maxChars) return text
        var end = maxChars - 1
        if (Character.isHighSurrogate(text[end - 1])) end -= 1
        return text.substring(0, end).trimEnd() + ELLIPSIS
    }
}
