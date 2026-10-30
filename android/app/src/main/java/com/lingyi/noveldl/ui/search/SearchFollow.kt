package com.lingyi.noveldl.ui.search

import com.lingyi.noveldl.data.SearchResult
import com.lingyi.noveldl.data.WatchlistAddRequest

/**
 * Pure rules for following a search result from the search screen.
 *
 * The screen reuses `POST /api/v1/watchlist` with the result's site, book_id
 * and title. Keeping the identity key and request-body mapping out of the
 * composable makes them testable on the JVM.
 */

/** Stable identity of a watch entry, used to de-duplicate follow requests. */
fun searchWatchKey(site: String, bookId: String): String = "$site/$bookId"

/** [searchWatchKey] for a search result. */
fun SearchResult.watchKey(): String = searchWatchKey(site, book_id)

/**
 * The `POST /api/v1/watchlist` body for following [result]; blank results
 * (no site or book_id) are rejected so the button never posts garbage.
 */
fun followRequest(result: SearchResult): WatchlistAddRequest? {
    if (result.site.isBlank() || result.book_id.isBlank()) return null
    return WatchlistAddRequest(site = result.site, book_id = result.book_id, title = result.title)
}
