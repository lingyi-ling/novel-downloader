package com.lingyi.noveldl.data

import java.net.URLDecoder

/**
 * Pure helpers for the optional custom SAF download directory.
 *
 * The user can pick a tree with `ACTION_OPEN_DOCUMENT_TREE`; its URI is stored
 * in DataStore and history downloads are written into a fixed subdirectory of
 * that tree. These helpers decide whether a stored value is a usable tree URI
 * and derive a human-readable label for it, without touching Android types, so
 * the rules can be unit tested on the JVM.
 */

/** Subdirectory created (via DocumentFile) inside the chosen SAF tree. */
const val DOWNLOAD_SUBDIRECTORY = "NovelDL"

/** Where history files go for the current settings. */
sealed interface DownloadTarget {
    /** The system Downloads directory via DownloadManager (previous behaviour). */
    data object SystemDownloads : DownloadTarget

    /** A persisted SAF tree URI; files land in [DOWNLOAD_SUBDIRECTORY] inside it. */
    data class CustomTree(val treeUri: String) : DownloadTarget
}

/**
 * Maps the stored DataStore value to a [DownloadTarget]. Blank values — and
 * anything that is not a `content://...tree/...` URI (e.g. a provider that was
 * uninstalled) — fall back to the system Downloads directory.
 */
fun resolveDownloadTarget(rawTreeUri: String?): DownloadTarget {
    val uri = rawTreeUri?.trim().orEmpty()
    return if (isSupportedTreeUri(uri)) {
        DownloadTarget.CustomTree(uri)
    } else {
        DownloadTarget.SystemDownloads
    }
}

/** Whether [raw] looks like a persisted SAF tree URI (`content://.../tree/...`). */
fun isSupportedTreeUri(raw: String): Boolean =
    raw.startsWith("content://", ignoreCase = true) && raw.contains("/tree/")

/**
 * Human-readable label for a stored tree URI, e.g.
 * `content://com.android.externalstorage.documents/tree/primary%3ADocuments%2FBooks`
 * becomes `Documents/Books`.
 *
 * The decoding mirrors [downloadFileName]: the `/tree/` path segment is
 * percent-decoded, the storage-root prefix before the first `:` is dropped and
 * trailing separators are trimmed. Anything that does not look like a tree URI
 * is returned as-is; when the whole path is just the root the label is empty.
 */
fun treeUriDisplayName(raw: String): String {
    val trimmed = raw.trim()
    if (!trimmed.contains("/tree/")) return trimmed
    val treePart = trimmed.substringAfter("/tree/", "")
    val decoded = runCatching { URLDecoder.decode(treePart, "UTF-8") }.getOrDefault(treePart)
    return decoded
        // The part before the first ':' is the storage root ("primary:",
        // "home:", …); trees without a root marker keep their whole path.
        .substringAfter(':', decoded)
        .trimEnd('/')
        .trimStart('/')
}

/**
 * Full label shown to the user for the actual download destination: the chosen
 * directory plus the [DOWNLOAD_SUBDIRECTORY] folder created inside it.
 */
fun customDownloadDirLabel(rawTreeUri: String): String {
    val name = treeUriDisplayName(rawTreeUri)
    return if (name.isBlank()) DOWNLOAD_SUBDIRECTORY else "$name/$DOWNLOAD_SUBDIRECTORY"
}
