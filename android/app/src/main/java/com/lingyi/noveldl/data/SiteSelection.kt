package com.lingyi.noveldl.data

/**
 * Pure rules for the multi-select site filter.
 *
 * The search UI keeps the selection as a comma-separated string so it can be
 * stored both in `rememberSaveable` and in SharedPreferences with the same
 * format. Keeping the normalization here makes it testable on the JVM.
 */

/** Trims every key, drops blanks and de-duplicates while keeping user order. */
fun normalizeSiteKeys(sites: Iterable<String>): List<String> =
    sites.map { it.trim() }.filter { it.isNotEmpty() }.distinct()

/** Restores a selection previously produced by [encodeSelectedSites]. */
fun decodeSelectedSites(encoded: String?): List<String> =
    normalizeSiteKeys(encoded.orEmpty().split(','))

/** Serializes a selection into the compact format used for state and prefs. */
fun encodeSelectedSites(sites: Iterable<String>): String =
    normalizeSiteKeys(sites).joinToString(",")

/**
 * Value for the `sites` field of a search request.
 *
 * An empty selection means "search every enabled site", which the API expects
 * as an omitted field, hence `null` instead of an empty list.
 */
fun selectedSitesParam(sites: Collection<String>): List<String>? =
    normalizeSiteKeys(sites).ifEmpty { null }

/** Adds [site] when absent, removes it otherwise. Blank keys are ignored. */
fun toggleSiteSelection(current: Collection<String>, site: String): List<String> {
    val keys = normalizeSiteKeys(current)
    val key = site.trim()
    if (key.isEmpty()) return keys
    return if (key in keys) keys.filterNot { it == key } else keys + key
}

/** Human readable summary; falls back to the raw key for unknown sites. */
fun siteSummary(selected: Collection<String>, labels: Map<String, String>): String =
    normalizeSiteKeys(selected).joinToString(", ") { labels[it] ?: it }
