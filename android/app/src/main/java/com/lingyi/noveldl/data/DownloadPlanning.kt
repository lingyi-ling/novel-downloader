package com.lingyi.noveldl.data

import java.net.URLDecoder

/**
 * Pure planning helpers for history downloads.
 *
 * They are kept free of Android types so the rules (URL joining, file naming,
 * MIME lookup) can be exercised by JVM unit tests.
 */

/** Characters that are not allowed in a file name on any supported file system. */
private val ILLEGAL_FILE_NAME_CHARS = setOf('/', '\\', ':', '*', '?', '"', '<', '>', '|')

private const val MAX_FILE_NAME_LENGTH = 180

/**
 * Joins the API base URL with the relative `download_url` from the history
 * payload. Absolute http(s) URLs are passed through unchanged and duplicate
 * slashes at the join point are removed.
 */
fun resolveDownloadUrl(baseUrl: String, downloadUrl: String): String {
    val relative = downloadUrl.trim()
    if (relative.startsWith("http://", ignoreCase = true) ||
        relative.startsWith("https://", ignoreCase = true)
    ) {
        return relative
    }
    return baseUrl.trim().trimEnd('/') + "/" + relative.trimStart('/')
}

/** Whether [url] can be handed to DownloadManager. */
fun isSupportedDownloadUrl(url: String): Boolean =
    url.startsWith("http://", ignoreCase = true) ||
        url.startsWith("https://", ignoreCase = true)

/**
 * Makes [raw] safe to use as a single file name: path separators are reduced
 * to their last segment, control/illegal characters become `_`, and leading or
 * trailing dots/spaces (which are invisible or reserved) are dropped.
 */
fun sanitizeFileName(raw: String): String {
    val leaf = raw.substringAfterLast('/').substringAfterLast('\\')
    val cleaned = buildString(leaf.length) {
        leaf.forEach { char ->
            append(if (char.code < 0x20 || char in ILLEGAL_FILE_NAME_CHARS) '_' else char)
        }
    }
    return cleaned.trim().trim('.').take(MAX_FILE_NAME_LENGTH).trim()
}

/**
 * Picks the file name used in the public Downloads directory.
 *
 * The display name from the API wins; when it is blank the last segment of
 * `download_url` is percent-decoded. A final fallback keeps the extension so
 * the downloaded file stays identifiable.
 */
fun downloadFileName(name: String, downloadUrl: String, fallbackExt: String): String {
    sanitizeFileName(name).takeIf { it.isNotEmpty() }?.let { return it }

    val encodedLeaf = downloadUrl
        .substringBefore('?')
        .substringBefore('#')
        .substringAfterLast('/')
    val decoded = runCatching { URLDecoder.decode(encodedLeaf, "UTF-8") }
        .getOrDefault(encodedLeaf)
    sanitizeFileName(decoded).takeIf { it.isNotEmpty() }?.let { return it }

    val ext = fallbackExt.trim().trimStart('.').take(16)
        .ifEmpty { "txt" }
        .filter { it.isLetterOrDigit() }
        .ifEmpty { "txt" }
    return "download.$ext"
}

/** MIME type for the file extensions exposed by the history endpoint. */
fun mimeTypeForExtension(ext: String): String? = when (ext.trim().lowercase()) {
    "txt" -> "text/plain"
    "epub" -> "application/epub+zip"
    else -> null
}