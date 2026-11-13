package com.lingyi.noveldl.ui.search

import com.lingyi.noveldl.data.SearchRequest
import java.math.BigInteger
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneOffset

/**
 * Optional search filters shared by the UI and its tests.
 *
 * The fields keep the raw text the user typed (or the date the picker chose)
 * instead of the parsed values, so an invalid entry can be shown with its own
 * error and corrected in place. [toSearchRequest] refuses to build a request
 * while anything is invalid, which keeps malformed numbers/dates off the wire
 * (the server would answer `422`).
 */
data class SearchFilterState(
    val minWords: String = "",
    val maxWords: String = "",
    /** ISO `YYYY-MM-DD`, empty when the date filter is off. */
    val updatedAfter: String = "",
) {
    /** Number of non-blank fields, including entries that are not valid yet. */
    val activeFilterCount: Int
        get() = listOf(minWords, maxWords, updatedAfter).count { it.isNotBlank() }

    val isActive: Boolean
        get() = activeFilterCount > 0

    /** Per-field errors plus the min/max relationship, all computed purely. */
    fun validate(): SearchFilterValidation {
        val min = parseWordFilter(minWords)
        val max = parseWordFilter(maxWords)
        val parsedDate = parseFilterDate(updatedAfter)
        return SearchFilterValidation(
            minWordsError = min.error,
            maxWordsError = max.error,
            updatedAfterError = if (updatedAfter.isBlank() || parsedDate != null) {
                null
            } else {
                DateFilterError.INVALID
            },
            rangeError = min.value != null && max.value != null && min.value > max.value,
        )
    }

    /**
     * The `POST /api/v1/search` body for this filter set, or `null` when any
     * field is invalid. Blank filters become `null` fields, which the JSON
     * serializer omits, so an empty panel behaves exactly like the old
     * unfiltered request.
     */
    fun toSearchRequest(keyword: String, sites: List<String>? = null): SearchRequest? {
        if (!validate().isValid) return null
        return SearchRequest(
            keyword = keyword,
            sites = sites,
            min_words = parseWordFilter(minWords).value,
            max_words = parseWordFilter(maxWords).value,
            updated_after = parseFilterDate(updatedAfter)?.toString(),
        )
    }

    companion object {
        val EMPTY = SearchFilterState()
    }
}

/** A word-count entry could not be turned into a server-accepted value. */
enum class WordFilterError {
    NOT_A_NUMBER,
    NEGATIVE,
    TOO_LARGE,
}

/** A date entry is not an ISO `YYYY-MM-DD` calendar date. */
enum class DateFilterError {
    INVALID,
}

data class SearchFilterValidation(
    val minWordsError: WordFilterError? = null,
    val maxWordsError: WordFilterError? = null,
    val updatedAfterError: DateFilterError? = null,
    val rangeError: Boolean = false,
) {
    val isValid: Boolean
        get() = minWordsError == null &&
            maxWordsError == null &&
            updatedAfterError == null &&
            !rangeError
}

/**
 * Upper bound mirrored from the REST schema (`MAX_SEARCH_WORDS`). Real books
 * are far below it; a larger value is rejected instead of silently clamped.
 */
const val MAX_SEARCH_WORDS: Int = 1_000_000_000

internal data class ParsedWordFilter(val value: Int?, val error: WordFilterError?)

/**
 * Parses one word-count field. Blank means "no bound"; anything else must be
 * a base-10 whole number in `0..MAX_SEARCH_WORDS`. [BigInteger] is used so an
 * overlong digit string is reported as [WordFilterError.TOO_LARGE] instead of
 * being mistaken for non-numeric input.
 */
internal fun parseWordFilter(raw: String): ParsedWordFilter {
    val text = raw.trim()
    if (text.isEmpty()) return ParsedWordFilter(null, null)
    val parsed = text.toBigIntegerOrNull()
        ?: return ParsedWordFilter(null, WordFilterError.NOT_A_NUMBER)
    return when {
        parsed.signum() < 0 -> ParsedWordFilter(null, WordFilterError.NEGATIVE)
        parsed > BigInteger.valueOf(MAX_SEARCH_WORDS.toLong()) ->
            ParsedWordFilter(null, WordFilterError.TOO_LARGE)
        else -> ParsedWordFilter(parsed.toInt(), null)
    }
}

/** Strict ISO calendar-date shape; `LocalDate.parse` alone accepts wider years. */
private val ISO_DATE = Regex("""\d{4}-\d{2}-\d{2}""")

/**
 * Parses the filter date, returning `null` for blanks and malformed values.
 * A year below 1 is rejected because the server's `date` type cannot carry it
 * (and the picker never produces one).
 */
fun parseFilterDate(raw: String): LocalDate? {
    val text = raw.trim()
    if (!ISO_DATE.matches(text)) return null
    val date = runCatching { LocalDate.parse(text) }.getOrNull() ?: return null
    return date.takeIf { it.year >= 1 }
}

/**
 * Formats the UTC millisecond value reported by Material3's date picker as
 * the ISO date the API expects. The picker works in UTC, so the same offset is
 * used here; using the device zone would shift the chosen day for users west
 * of UTC.
 */
fun formatFilterDate(epochMillis: Long): String =
    Instant.ofEpochMilli(epochMillis).atZone(ZoneOffset.UTC).toLocalDate().toString()

/** Initial picker value for [formatFilterDate]; `null` when nothing is set. */
fun filterDateEpochMillis(date: String): Long? =
    parseFilterDate(date)?.atStartOfDay(ZoneOffset.UTC)?.toInstant()?.toEpochMilli()
