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

/** Upper bound for a file name produced by [uniqueChildName]. */
private const val MAX_UNIQUE_FILE_NAME_LENGTH = 180

/** How many `name (n).ext` candidates to try before giving up. */
private const val MAX_CONFLICT_ATTEMPTS = 999

/**
 * The persisted SAF tree cannot be written to any more: its grant was revoked,
 * the provider is gone or the directory was deleted. Download callers treat
 * this as "fall back to the system Downloads directory and tell the user"
 * rather than as a generic failure, so the reason survives the exception.
 */
class DownloadDirectoryUnavailableException(message: String) : IllegalStateException(message)

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
 *
 * [persistedWriteUris] is the set of `content://` strings the app currently
 * holds a persisted write grant for (`ContentResolver.persistedUriPermissions`
 * filtered by `isWritePermission`, see [hasPersistedWriteAccess]). When it is
 * supplied, a stored tree that no longer appears in it (permission revoked,
 * provider uninstalled, app data restored to another device) also falls back
 * to the system Downloads directory. Passing `null` skips the check for
 * callers that only need URI-shape validation.
 */
fun resolveDownloadTarget(
    rawTreeUri: String?,
    persistedWriteUris: Collection<String>? = null,
): DownloadTarget {
    // Normalize whitespace and the trailing slash here too, so a value copied
    // from a grant (which may carry one) and the picker's value compare and
    // behave identically downstream.
    val uri = treeUriKey(rawTreeUri.orEmpty())
    if (!isSupportedTreeUri(uri)) return DownloadTarget.SystemDownloads
    if (persistedWriteUris != null && !hasPersistedWriteAccess(uri, persistedWriteUris)) {
        return DownloadTarget.SystemDownloads
    }
    return DownloadTarget.CustomTree(uri)
}

/**
 * Comparison key for persisted tree URIs. The picker returns the URI without a
 * trailing slash while some providers persist one; whitespace never matters.
 */
internal fun treeUriKey(raw: String): String = raw.trim().trimEnd('/')

/**
 * Whether [treeUri] is one of the [persistedWriteUris] the app may write to.
 * An empty [treeUri] never matches: that is the "no custom directory" state.
 */
fun hasPersistedWriteAccess(
    treeUri: String,
    persistedWriteUris: Collection<String>,
): Boolean {
    val wanted = treeUriKey(treeUri)
    if (wanted.isEmpty()) return false
    return persistedWriteUris.any { treeUriKey(it) == wanted }
}

/**
 * Whether a non-blank stored tree exists but cannot be used right now. The
 * settings screen shows this as an explicit warning; downloads fall back to
 * the system Downloads directory instead of writing to the wrong place.
 */
fun customTreeUnavailable(
    rawTreeUri: String?,
    persistedWriteUris: Collection<String>,
): Boolean {
    val uri = rawTreeUri?.trim().orEmpty()
    return uri.isNotEmpty() && !hasPersistedWriteAccess(uri, persistedWriteUris)
}

/**
 * Returns a name that does not collide with [existingNames] (as returned by
 * `DocumentFile.listFiles()?.name`), appending ` (1)`, ` (2)`, … before the
 * extension: `book.txt` becomes `book (1).txt`.
 *
 * Comparison is case-insensitive because FAT/exFAT-backed SAF providers are:
 * creating `Book.txt` next to `book.txt` would otherwise count as a collision
 * after the provider normalizes it. Hidden files (`.nomedia`) and names whose
 * "extension" is implausibly long are treated as having no extension.
 *
 * When every candidate up to [MAX_CONFLICT_ATTEMPTS] is taken the desired name
 * is returned unchanged; a provider that auto-renames still protects the old
 * file, so this is a last resort rather than a silent overwrite.
 */
fun uniqueChildName(desiredName: String, existingNames: Collection<String>): String {
    val name = desiredName.trim().ifEmpty { "download" }
    val taken = existingNames
        .asSequence()
        .map { it.trim() }
        .filter { it.isNotEmpty() }
        .map { it.lowercase() }
        .toSet()
    if (name.lowercase() !in taken) return name

    val dot = name.lastIndexOf('.')
    val hasExtension = dot > 0 && name.length - dot <= 17
    val base = if (hasExtension) name.substring(0, dot) else name
    val extension = if (hasExtension) name.substring(dot) else ""

    for (index in 1..MAX_CONFLICT_ATTEMPTS) {
        val suffix = " ($index)"
        val room = MAX_UNIQUE_FILE_NAME_LENGTH - extension.length - suffix.length
        val stem = base.take(room.coerceAtLeast(1)).ifEmpty { "download" }
        val candidate = stem + suffix + extension
        if (candidate.lowercase() !in taken) return candidate
    }
    return name
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
