package com.lingyi.noveldl.data

/**
 * Counts-only view of `GET /api/v1/sites` for the settings "sites overview".
 *
 * The UI intentionally shows numbers only (never the site names), so the whole
 * summary stays pure and trivially testable on the JVM.
 */
data class SitesSummary(
    val searchCount: Int = 0,
    val downloadCount: Int = 0,
) {
    /** True when the server reported no sites at all. */
    val isEmpty: Boolean get() = searchCount == 0 && downloadCount == 0
}

/**
 * Reduces a [SitesResponse] to the number of enabled search and download
 * sites. A missing JSON key keeps the default empty map (a literal JSON `null`
 * would fail without `coerceInputValues`, like on every other endpoint), so
 * absent and empty site lists both end up at 0.
 */
fun SitesResponse.toSitesSummary(): SitesSummary = SitesSummary(
    searchCount = search.size,
    downloadCount = download.size,
)
