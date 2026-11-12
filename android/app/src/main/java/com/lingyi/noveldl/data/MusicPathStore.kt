package com.lingyi.noveldl.data

import android.content.Context
import java.io.File
import java.io.FileOutputStream
import java.nio.file.AtomicMoveNotSupportedException
import java.nio.file.Files
import java.nio.file.StandardCopyOption
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.addJsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray

/** File holding the persisted `provider + id -> absolute path` map. */
const val MUSIC_TRACK_PATHS_FILE: String = "music_track_paths.json"

/** Hard cap on persisted tracks; the oldest writes are evicted first. */
const val MUSIC_TRACK_PATHS_MAX_ENTRIES: Int = 2_000

/** Refuse to parse absurdly large files instead of loading them into memory. */
const val MUSIC_TRACK_PATHS_MAX_BYTES: Long = 4L * 1024 * 1024

/** On-disk schema version; bump when the layout changes. */
private const val MUSIC_TRACK_PATHS_VERSION: Int = 1

/** Longest path accepted from the engine; real media paths are far shorter. */
private const val MAX_TRACK_PATH_CHARS: Int = 2_048

/**
 * Persists the local path of every track the engine finished downloading, so
 * a playlist can still be exported after the app process was killed.
 *
 * The in-memory path map owned by the music screen ([com.lingyi.noveldl.ui.music.MusicSearchUiState])
 * dies with the process. The engine, however, keeps the downloaded files, so
 * only the `provider + id -> path` association has to survive. This store is
 * that record.
 *
 * Bounds:
 * - at most [MUSIC_TRACK_PATHS_MAX_ENTRIES] entries, oldest evicted first;
 * - only valid track keys and absolute paths are written;
 * - entries pointing at a *device-local* file that no longer exists are
 *   removed on the next read/write (paths outside the app's own directories
 *   are server-side paths in remote mode and cannot be checked from here);
 * - every write replaces the whole file through a temporary + fsync + atomic
 *   rename, so a crash leaves either the old map or the new one.
 */
interface MusicPathPersistence {

    /** Current persisted map, after pruning device-local files that vanished. */
    suspend fun snapshot(): Map<String, String>

    /** Records [paths] (newest wins), evicts overflow and persists atomically. */
    suspend fun putAll(paths: Map<String, String>)

    /** Removes entries for [keys] (e.g. tracks the engine now reports failed). */
    suspend fun remove(keys: Collection<String>)

    companion object {
        /** No-op implementation used by unit tests and by transient transports. */
        val None: MusicPathPersistence = object : MusicPathPersistence {
            override suspend fun snapshot(): Map<String, String> = emptyMap()

            override suspend fun putAll(paths: Map<String, String>) = Unit

            override suspend fun remove(keys: Collection<String>) = Unit
        }
    }
}

/**
 * Pure JSON codec and validation rules for the persisted path map.
 *
 * Decoding is per-entry: a hand-edited or truncated file must not discard
 * every remembered path, and a completely corrupt file must decode to an empty
 * map instead of throwing into the music screen.
 */
object MusicPathCodec {

    private val json = Json {
        ignoreUnknownKeys = true
        explicitNulls = false
        isLenient = true
    }

    /**
     * Whether [value] has the track-key shape `provider\u0000id` (both parts
     * non-blank, exactly one separator). Mirrors
     * [com.lingyi.noveldl.ui.music.MusicJobResults.isTrackKey]; bare ids saved
     * by older builds are rejected instead of matching another provider.
     */
    fun isTrackKey(value: String): Boolean {
        val separator = value.indexOf('\u0000')
        if (separator <= 0 || separator >= value.lastIndex) return false
        return value.indexOf('\u0000', separator + 1) == -1
    }

    /** Absolute POSIX path (`/...`), Windows drive path or UNC path. */
    fun isAbsolutePath(path: String): Boolean {
        val text = path.trim()
        if (text.isEmpty()) return false
        if (text.startsWith("/")) return true
        if (text.length >= 3 && text[1] == ':' && text[0].isLetter()) {
            return text[2] == '\\' || text[2] == '/'
        }
        return text.startsWith("\\\\")
    }

    /** Whether [path] can safely be persisted and sent back as a playlist entry. */
    fun isSafePath(path: String): Boolean {
        val text = path.trim()
        if (text.isEmpty() || text.length > MAX_TRACK_PATH_CHARS) return false
        if (text.any { it == '\u0000' || it == '\n' || it == '\r' }) return false
        return isAbsolutePath(text)
    }

    fun isValidEntry(key: String, path: String): Boolean =
        isTrackKey(key) && isSafePath(path)

    /**
     * Merges [incoming] over [existing], validating entries and evicting the
     * oldest ones until the result fits [maxEntries]. Updating a track moves
     * it to the end, so eviction order is write order.
     */
    fun merge(
        existing: Map<String, String>,
        incoming: Map<String, String>,
        maxEntries: Int = MUSIC_TRACK_PATHS_MAX_ENTRIES,
    ): LinkedHashMap<String, String> {
        val merged = LinkedHashMap<String, String>()
        existing.forEach { (key, path) ->
            if (isValidEntry(key, path)) merged[key] = path.trim()
        }
        incoming.forEach { (rawKey, rawPath) ->
            val key = rawKey.trim()
            val path = rawPath.trim()
            if (!isValidEntry(key, path)) return@forEach
            // Re-inserting moves the key to the end (most recently written).
            merged.remove(key)
            merged[key] = path
        }
        val limit = maxEntries.coerceAtLeast(0)
        while (merged.size > limit) {
            val oldest = merged.keys.firstOrNull() ?: break
            merged.remove(oldest)
        }
        return merged
    }

    /** Serialises [entries] as `{"version":1,"entries":[{"key":..,"path":..}]}`. */
    fun encode(
        entries: Map<String, String>,
        maxEntries: Int = MUSIC_TRACK_PATHS_MAX_ENTRIES,
    ): String {
        val bounded = merge(emptyMap(), entries, maxEntries)
        val document = buildJsonObject {
            put("version", MUSIC_TRACK_PATHS_VERSION)
            putJsonArray("entries") {
                bounded.forEach { (key, path) ->
                    addJsonObject {
                        put("key", key)
                        put("path", path)
                    }
                }
            }
        }
        return document.toString()
    }

    /**
     * Parses [raw]. A missing, oversized, unreadable or corrupt document yields
     * an empty map; individual malformed entries are skipped. The returned map
     * keeps array order (oldest first), so a rewritten file preserves recency.
     */
    fun decode(raw: String?): LinkedHashMap<String, String> {
        if (raw.isNullOrBlank()) return LinkedHashMap()
        val root = runCatching { json.parseToJsonElement(raw) }.getOrNull()
            as? JsonObject ?: return LinkedHashMap()
        val version = (root["version"] as? JsonPrimitive)?.intOrNull
        // A newer writer may have changed the meaning of the fields; do not
        // guess. A missing version is treated as the current layout.
        if (version != null && version > MUSIC_TRACK_PATHS_VERSION) return LinkedHashMap()
        val entries = root["entries"] as? JsonArray ?: return LinkedHashMap()
        val decoded = LinkedHashMap<String, String>()
        entries.forEach { element ->
            val entry = element as? JsonObject ?: return@forEach
            val key = (entry["key"] as? JsonPrimitive)?.content?.trim().orEmpty()
            val path = (entry["path"] as? JsonPrimitive)?.content?.trim().orEmpty()
            if (!isValidEntry(key, path)) return@forEach
            decoded.remove(key)
            decoded[key] = path
        }
        return decoded
    }
}

/**
 * Pure path rules deciding whether a reported path lives inside one of the
 * app's own directories (and therefore can be existence-checked on the phone).
 *
 * Comparison is lexical: both separators are unified, `.`/`..` segments are
 * resolved first, so `filesDir/../secret` cannot masquerade as a file inside
 * `filesDir`.
 */
object MusicLocalPaths {

    fun normalize(path: String): String {
        val text = path.trim().replace('\\', '/')
        if (text.isEmpty()) return ""
        val drive = if (text.length >= 2 && text[1] == ':' && text[0].isLetter()) {
            text.substring(0, 2).uppercase()
        } else {
            ""
        }
        val rest = if (drive.isEmpty()) text else text.substring(2)
        val absolute = rest.startsWith("/")
        val segments = ArrayList<String>()
        rest.split('/').forEach { segment ->
            when (segment) {
                "", "." -> Unit
                ".." -> if (segments.isNotEmpty() && segments.last() != "..") {
                    segments.removeAt(segments.lastIndex)
                } else if (!absolute) {
                    segments.add("..")
                }
                else -> segments.add(segment)
            }
        }
        val body = segments.joinToString("/")
        return when {
            drive.isNotEmpty() -> if (body.isEmpty()) "$drive/" else "$drive/$body"
            absolute -> "/$body"
            else -> body
        }
    }

    /** Whether the normalised [path] equals [root] or lies below it. */
    fun isInside(root: String, path: String): Boolean {
        val base = normalize(root)
        val target = normalize(path)
        if (base.isEmpty() || target.isEmpty()) return false
        if (!MusicPathCodec.isAbsolutePath(base)) return false
        if (!MusicPathCodec.isAbsolutePath(target)) return false
        if (base == target) return true
        val prefix = if (base.endsWith("/")) base else "$base/"
        return target.startsWith(prefix)
    }

    /** Whether [path] is a device-local path under any of [roots]. */
    fun isLocal(path: String, roots: List<String>): Boolean =
        roots.any { isInside(it, path) }

    /**
     * Directories whose files the app can inspect on this device: the private
     * files dir (where the embedded engine's music output lives) and the
     * app-specific external files dir.
     */
    fun deviceRoots(context: Context): List<String> {
        val app = context.applicationContext
        return buildList {
            add(app.filesDir.absolutePath)
            runCatching { app.getExternalFilesDir(null) }
                .getOrNull()
                ?.let { add(it.absolutePath) }
        }
    }
}

/**
 * File-backed [MusicPathPersistence].
 *
 * Reads/writes are serialised by a process-wide lock, because the job
 * ViewModel writes while the export ViewModel may be reading through a
 * separate instance. Production uses [MUSIC_TRACK_PATHS_FILE] in the app's
 * private files dir; tests drive it with a plain [File] (no Android Context).
 */
class MusicPathStore(
    private val file: File,
    private val localRoots: List<String> = emptyList(),
    private val exists: (String) -> Boolean = { File(it).isFile },
    private val maxEntries: Int = MUSIC_TRACK_PATHS_MAX_ENTRIES,
) : MusicPathPersistence {

    constructor(context: Context) : this(
        File(context.applicationContext.filesDir, MUSIC_TRACK_PATHS_FILE),
        MusicLocalPaths.deviceRoots(context.applicationContext),
    )

    override suspend fun snapshot(): Map<String, String> = withContext(Dispatchers.IO) {
        synchronized(LOCK) {
            val stored = readLocked()
            val cleaned = retainExisting(stored)
            // Persist the cleanup: a deleted track must not linger and be
            // exported as a dead playlist entry on the next launch.
            if (cleaned.size != stored.size) writeLocked(cleaned)
            cleaned
        }
    }

    override suspend fun putAll(paths: Map<String, String>) {
        if (paths.isEmpty()) return
        withContext(Dispatchers.IO) {
            synchronized(LOCK) {
                val raw = readLocked()
                val stored = retainExisting(raw)
                val accepted = paths.filterValues(::isStorable)
                if (accepted.isEmpty()) {
                    if (stored.size != raw.size) writeLocked(stored)
                } else {
                    val merged = MusicPathCodec.merge(stored, accepted, maxEntries)
                    if (merged != stored) writeLocked(merged)
                }
            }
        }
    }

    override suspend fun remove(keys: Collection<String>) {
        if (keys.isEmpty()) return
        withContext(Dispatchers.IO) {
            synchronized(LOCK) {
                val stored = retainExisting(readLocked())
                val remaining = LinkedHashMap(stored)
                keys.forEach { remaining.remove(it) }
                if (remaining != stored) writeLocked(remaining)
            }
        }
    }

    /** Device-local paths must still exist; server-side paths cannot be checked. */
    private fun isStorable(path: String): Boolean {
        if (!MusicPathCodec.isSafePath(path)) return false
        return !MusicLocalPaths.isLocal(path, localRoots) || existsSafely(path)
    }

    private fun retainExisting(entries: Map<String, String>): LinkedHashMap<String, String> {
        val kept = LinkedHashMap<String, String>()
        entries.forEach { (key, path) ->
            if (isStorable(path)) kept[key] = path
        }
        return kept
    }

    private fun existsSafely(path: String): Boolean =
        runCatching { exists(path) }.getOrDefault(true)

    private fun readLocked(): LinkedHashMap<String, String> {
        if (!file.isFile || file.length() > MUSIC_TRACK_PATHS_MAX_BYTES) {
            return LinkedHashMap()
        }
        val raw = runCatching { file.readText(Charsets.UTF_8) }.getOrNull()
            ?: return LinkedHashMap()
        return MusicPathCodec.decode(raw)
    }

    /**
     * Replaces the file in one step: content goes to a temp file in the same
     * directory, is fsynced, then moved over the target. A failure leaves the
     * previous map intact.
     */
    private fun writeLocked(entries: Map<String, String>) {
        val parent = file.absoluteFile.parentFile ?: return
        if (!parent.isDirectory && !parent.mkdirs() && !parent.isDirectory) return
        val tmp = runCatching {
            File.createTempFile(".${file.name}.", ".tmp", parent)
        }.getOrNull() ?: return
        try {
            val bytes = MusicPathCodec.encode(entries, maxEntries).toByteArray(Charsets.UTF_8)
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
        /** One lock for every instance: job and export ViewModels share the file. */
        val LOCK = Any()
    }
}
