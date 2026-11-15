package com.lingyi.noveldl.data

import android.content.Context
import java.io.File
import java.io.FileOutputStream
import java.nio.file.AtomicMoveNotSupportedException
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import java.util.Locale
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.addJsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.longOrNull
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray

/** File under the app's private files directory holding the recent searches. */
const val SEARCH_HISTORY_FILE: String = "search_history.json"

/** Number of recent search terms kept; the oldest one is evicted first. */
const val MAX_SEARCH_HISTORY_ENTRIES: Int = 10

/** Longest term persisted; the search field is single-line, so this is ample. */
const val MAX_SEARCH_HISTORY_QUERY_CHARS: Int = 200

/** Refuse to parse absurdly large files instead of loading them into memory. */
const val MAX_SEARCH_HISTORY_FILE_BYTES: Long = 256L * 1024

/** On-disk schema version; bump when the layout changes. */
private const val SEARCH_HISTORY_VERSION: Int = 1

/**
 * One remembered search term. [searchedAt] is epoch millis and orders the list
 * (newest first); it is kept alongside the text so a re-search moves the term
 * to the front without relying on file order alone.
 */
data class SearchHistoryEntry(
    val query: String = "",
    val searchedAt: Long = 0L,
)

/**
 * Pure validation/ordering rules for the recent-search list.
 *
 * The rules are deliberately separate from the file I/O so they can be unit
 * tested and reused by the screen:
 * - terms are trimmed and internal whitespace is collapsed;
 * - terms are compared case-insensitively for de-duplication, so "Harry" and
 *   "harry" are one entry (the most recent spelling wins);
 * - the list is ordered by [SearchHistoryEntry.searchedAt], newest first;
 * - at most `maxEntries` terms are kept.
 *
 * Decoding is per-entry: a hand-edited or truncated file must not discard the
 * whole list, and a completely corrupt document decodes to an empty list
 * instead of throwing into the search screen.
 */
object SearchHistoryCodec {

    private val json = Json {
        ignoreUnknownKeys = true
        explicitNulls = false
        isLenient = true
    }

    /** Trimmed, whitespace-collapsed form of [raw]; empty when nothing remains. */
    fun normalizeQuery(raw: String): String = raw
        .trim()
        .replace(WHITESPACE, " ")
        .take(MAX_SEARCH_HISTORY_QUERY_CHARS)
        .trim()

    /** De-duplication key: case-insensitive, so Latin case variants collapse. */
    fun entryKey(query: String): String = normalizeQuery(query).lowercase(Locale.ROOT)

    /**
     * Drops blank entries, normalises text, sorts newest first, removes
     * duplicates and applies the cap. The sort is stable, so entries with the
     * same timestamp keep their incoming (newest-first) order.
     */
    fun sanitize(
        entries: List<SearchHistoryEntry>,
        maxEntries: Int = MAX_SEARCH_HISTORY_ENTRIES,
    ): List<SearchHistoryEntry> {
        val normalized = entries.mapNotNull { entry ->
            val query = normalizeQuery(entry.query)
            if (query.isEmpty()) {
                null
            } else {
                SearchHistoryEntry(query = query, searchedAt = entry.searchedAt.coerceAtLeast(0L))
            }
        }
        val sorted = normalized.sortedByDescending { it.searchedAt }
        val seen = HashSet<String>()
        return sorted
            .filter { seen.add(entryKey(it.query)) }
            .take(maxEntries.coerceAtLeast(0))
    }

    /**
     * Returns the list with [query] recorded at [nowMillis]. Recording an
     * existing term moves it to the front instead of duplicating it; a blank
     * query leaves the list unchanged.
     */
    fun record(
        entries: List<SearchHistoryEntry>,
        query: String,
        nowMillis: Long,
        maxEntries: Int = MAX_SEARCH_HISTORY_ENTRIES,
    ): List<SearchHistoryEntry> {
        val normalized = normalizeQuery(query)
        if (normalized.isEmpty()) return sanitize(entries, maxEntries)
        val entry = SearchHistoryEntry(
            query = normalized,
            searchedAt = nowMillis.coerceAtLeast(0L),
        )
        return sanitize(listOf(entry) + entries, maxEntries)
    }

    /** Returns the list without the entry matching [query] (case-insensitively). */
    fun remove(
        entries: List<SearchHistoryEntry>,
        query: String,
        maxEntries: Int = MAX_SEARCH_HISTORY_ENTRIES,
    ): List<SearchHistoryEntry> {
        val key = entryKey(query)
        if (key.isEmpty()) return sanitize(entries, maxEntries)
        return sanitize(
            entries.filterNot { entryKey(it.query) == key },
            maxEntries,
        )
    }

    /** Serialises [entries] as `{"version":1,"entries":[{"query":..,"searched_at":..}]}`. */
    fun encode(
        entries: List<SearchHistoryEntry>,
        maxEntries: Int = MAX_SEARCH_HISTORY_ENTRIES,
    ): String {
        val bounded = sanitize(entries, maxEntries)
        val document = buildJsonObject {
            put("version", SEARCH_HISTORY_VERSION)
            putJsonArray("entries") {
                bounded.forEach { entry ->
                    addJsonObject {
                        put("query", entry.query)
                        put("searched_at", entry.searchedAt)
                    }
                }
            }
        }
        return document.toString()
    }

    /**
     * Parses [raw]. A missing, unreadable or corrupt document yields an empty
     * list; malformed entries are skipped individually. A newer schema version
     * yields an empty list rather than guessing at changed field meanings.
     */
    fun decode(
        raw: String?,
        maxEntries: Int = MAX_SEARCH_HISTORY_ENTRIES,
    ): List<SearchHistoryEntry> {
        if (raw.isNullOrBlank()) return emptyList()
        val root = runCatching { json.parseToJsonElement(raw) }.getOrNull()
        val elements: List<JsonElement> = when (root) {
            is JsonObject -> {
                val version = (root["version"] as? JsonPrimitive)?.contentOrNull?.toIntOrNull()
                if (version != null && version > SEARCH_HISTORY_VERSION) return emptyList()
                (root["entries"] as? JsonArray) ?: return emptyList()
            }
            // Tolerate a bare array from a hand-written file.
            is JsonArray -> root
            else -> return emptyList()
        }
        val decoded = elements.mapNotNull { element ->
            val entry = element as? JsonObject ?: return@mapNotNull null
            val query = (entry["query"] as? JsonPrimitive)?.contentOrNull.orEmpty()
            val searchedAt = (entry["searched_at"] as? JsonPrimitive)?.longOrNull ?: 0L
            val normalized = normalizeQuery(query)
            if (normalized.isEmpty()) {
                null
            } else {
                SearchHistoryEntry(
                    query = normalized,
                    searchedAt = searchedAt.coerceAtLeast(0L),
                )
            }
        }
        return sanitize(decoded, maxEntries)
    }

    private val WHITESPACE = Regex("[\\s\\p{Z}]+")
}

/**
 * File-backed recent-search storage.
 *
 * Every write replaces the whole file through a temporary file in the same
 * directory, an fsync and an atomic rename, so a crash leaves either the old
 * list or the new one, never a half-written JSON document. Reads are
 * serialised with writes by a process-wide lock because the screen can open
 * more than one instance across recompositions.
 *
 * The store keeps only the search terms themselves; nothing about the user or
 * an account is written.
 */
class SearchHistoryStore(
    private val file: File,
    private val maxEntries: Int = MAX_SEARCH_HISTORY_ENTRIES,
) {

    constructor(context: Context) : this(
        File(context.applicationContext.filesDir, SEARCH_HISTORY_FILE),
    )

    /** Current persisted history, newest first. Never throws on corrupt data. */
    suspend fun snapshot(): List<SearchHistoryEntry> = withContext(Dispatchers.IO) {
        synchronized(LOCK) { readLocked() }
    }

    /**
     * Records [query] as the newest search and returns the updated list, so the
     * caller can render it without a second read.
     */
    suspend fun record(
        query: String,
        nowMillis: Long = System.currentTimeMillis(),
    ): List<SearchHistoryEntry> = mutate { entries ->
        SearchHistoryCodec.record(entries, query, nowMillis, maxEntries)
    }

    /** Deletes one term (case-insensitively) and returns the updated list. */
    suspend fun remove(query: String): List<SearchHistoryEntry> = mutate { entries ->
        SearchHistoryCodec.remove(entries, query, maxEntries)
    }

    /** Deletes every term and returns the (empty) updated list. */
    suspend fun clear(): List<SearchHistoryEntry> = mutate { emptyList() }

    private suspend fun mutate(
        transform: (List<SearchHistoryEntry>) -> List<SearchHistoryEntry>,
    ): List<SearchHistoryEntry> = withContext(Dispatchers.IO) {
        synchronized(LOCK) {
            val current = readLocked()
            val next = transform(current)
            if (next != current) writeLocked(next)
            next
        }
    }

    private fun readLocked(): List<SearchHistoryEntry> {
        if (!file.isFile || file.length() > MAX_SEARCH_HISTORY_FILE_BYTES) {
            return emptyList()
        }
        val raw = runCatching { file.readText(Charsets.UTF_8) }.getOrNull()
            ?: return emptyList()
        return SearchHistoryCodec.decode(raw, maxEntries)
    }

    /**
     * Replaces the file in one step: content goes to a temp file in the same
     * directory, is fsynced, then moved over the target. A failure leaves the
     * previous list intact and the next write retries.
     */
    private fun writeLocked(entries: List<SearchHistoryEntry>) {
        val parent = file.absoluteFile.parentFile ?: return
        if (!parent.isDirectory && !parent.mkdirs() && !parent.isDirectory) return
        val tmp = runCatching {
            File.createTempFile(".${file.name}.", ".tmp", parent)
        }.getOrNull() ?: return
        try {
            val bytes = SearchHistoryCodec.encode(entries, maxEntries).toByteArray(Charsets.UTF_8)
            FileOutputStream(tmp).use { out ->
                out.write(bytes)
                out.flush()
                // fsync before the rename: the name must never point at data
                // that only exists in the page cache.
                out.fd.sync()
            }
            moveIntoPlace(tmp, file)
        } catch (_: Exception) {
            // Keep the previous file; the next write retries.
        } finally {
            tmp.delete()
        }
    }

    private fun moveIntoPlace(tmp: File, target: File) {
        try {
            Files.move(
                tmp.toPath(),
                target.toPath(),
                StandardCopyOption.REPLACE_EXISTING,
                StandardCopyOption.ATOMIC_MOVE,
            )
        } catch (_: AtomicMoveNotSupportedException) {
            // Some filesystems cannot rename atomically; replacing the target
            // is still better than leaving a half-written file.
            Files.move(tmp.toPath(), target.toPath(), StandardCopyOption.REPLACE_EXISTING)
        }
    }

    private companion object {
        /** One lock for every instance, so concurrent screens cannot interleave writes. */
        val LOCK = Any()
    }
}