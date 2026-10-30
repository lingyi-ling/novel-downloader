package com.lingyi.noveldl.ui.follow

import com.lingyi.noveldl.data.AddTaskRequest
import com.lingyi.noveldl.data.WatchCheckItem

/**
 * Pure rules for turning check results into real download-queue submissions.
 *
 * `/watchlist/check` with `download=true` only *attaches* a ready-to-post
 * task body; the caller must still submit it via `POST /api/v1/tasks`.
 * Keeping the decision logic out of the composable makes it testable on the
 * JVM: entries without a usable task (or with a blank site/book_id) must
 * never be submitted blindly.
 */

/** Stable identity of a watch entry, used to de-duplicate queued submissions. */
fun watchItemKey(site: String, bookId: String): String = "$site/$bookId"

/** [watchItemKey] for a check item. */
fun WatchCheckItem.key(): String = watchItemKey(site, book_id)

/**
 * The `POST /api/v1/tasks` body for a check item, or `null` when the item
 * carries no task (unchanged book, failed check, `download=false`) or the
 * task lacks a concrete site/book_id.
 */
fun queueTarget(item: WatchCheckItem): AddTaskRequest? =
    item.task
        ?.takeIf { it.site.isNotBlank() && it.book_id.isNotBlank() }
        ?.let { AddTaskRequest(site = it.site, book_id = it.book_id, title = it.title) }
