package com.lingyi.noveldl.data

import android.content.Context
import android.os.Build
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File
import java.io.IOException
import java.io.InputStream
import java.security.MessageDigest
import java.util.concurrent.TimeUnit

/**
 * One update candidate as shown by the UI.
 *
 * [latest] is the release tag with the leading `v` stripped, [sha256] is the
 * lowercased hex digest of [assetUrl] or `null` when GitHub published no
 * digest — a null digest means the download is refused, because an APK whose
 * integrity cannot be verified must never reach the installer.
 */
data class UpdateInfo(
    val current: String,
    val latest: String,
    val available: Boolean,
    val notesUrl: String?,
    val assetName: String?,
    val assetUrl: String?,
    val assetSize: Long?,
    val sha256: String?,
)

/** Why an update step failed; the UI maps each case to a translated string. */
enum class UpdateFailure {
    CHECK_FAILED,
    NO_ASSET,
    NO_CHECKSUM,
    CHECKSUM_MISMATCH,
    INSECURE_URL,
    DOWNLOAD_FAILED,
    INSUFFICIENT_SPACE,
    INSTALL_FAILED,
}

/** Network/download failure carrying a machine-readable [reason]. */
class UpdateException(
    val reason: UpdateFailure,
    message: String,
    cause: Throwable? = null,
) : IOException(message, cause)

/** Canonical message of a failure, used for the log-friendly exception text. */
internal fun UpdateFailure.message(): String = when (this) {
    UpdateFailure.CHECK_FAILED -> "check failed"
    UpdateFailure.NO_ASSET -> "no apk asset"
    UpdateFailure.NO_CHECKSUM -> "missing checksum"
    UpdateFailure.CHECKSUM_MISMATCH -> "checksum mismatch"
    UpdateFailure.INSECURE_URL -> "insecure asset url"
    UpdateFailure.DOWNLOAD_FAILED -> "download failed"
    UpdateFailure.INSUFFICIENT_SPACE -> "insufficient storage"
    UpdateFailure.INSTALL_FAILED -> "install failed"
}

/**
 * Pre-flight checks that must pass before a single byte is fetched: there has
 * to be an APK, its URL must be HTTPS, and a SHA-256 digest must be published.
 * A missing digest is a hard refusal — an APK that cannot be verified is never
 * handed to the installer.
 */
internal fun downloadRequestFailure(info: UpdateInfo): UpdateFailure? {
    val url = info.assetUrl.orEmpty().trim()
    return when {
        url.isEmpty() -> UpdateFailure.NO_ASSET
        !url.lowercase().startsWith("https://") -> UpdateFailure.INSECURE_URL
        Checksums.normalizeDigest(info.sha256) == null -> UpdateFailure.NO_CHECKSUM
        else -> null
    }
}

/**
 * Version ordering shared with the desktop updater
 * (`src/novel_downloader/apps/common/updater.py`): the leading `v` and any
 * `-suffix` are ignored and the remaining numeric segments are compared with
 * missing trailing segments counted as zero, so `1.4` and `1.4.0` are equal.
 */
object VersionCompare {

    /**
     * Numeric segments of [value], stopping at the first segment without a
     * digit. Unparsable input yields `[0]`, so it never ranks as an update.
     *
     * Build metadata (`1.4.0+41`) is dropped as well: otherwise the digits of
     * the build number would be read as a fourth version segment and
     * `1.4.0+41` would rank above `1.4.1`.
     */
    fun parse(value: String): List<Int> {
        val head = value.trim()
            .substringBefore('-')
            .substringBefore('+')
            .trimStart('v', 'V')
        val parts = mutableListOf<Int>()
        for (chunk in head.split('.')) {
            val number = chunk.filter { it in '0'..'9' }.toIntOrNull()
            if (number == null) break
            parts += number
        }
        return if (parts.isEmpty()) listOf(0) else parts
    }

    /** Whether [latest] is strictly newer than [current]. */
    fun isNewer(latest: String, current: String): Boolean {
        val left = parse(latest)
        val right = parse(current)
        val width = maxOf(left.size, right.size)
        for (index in 0 until width) {
            val a = left.getOrElse(index) { 0 }
            val b = right.getOrElse(index) { 0 }
            if (a != b) return a > b
        }
        return false
    }

    /** Display form of a release tag: no `v` prefix, no surrounding blanks. */
    fun normalize(value: String): String {
        val trimmed = value.trim()
        val stripped = trimmed.removePrefix("v").removePrefix("V")
        return stripped.ifBlank { trimmed }
    }
}

/** One GitHub release asset reduced to the fields the updater needs. */
data class ReleaseAsset(
    val name: String,
    val url: String,
    val size: Long?,
    val digest: String?,
)

/**
 * Android asset selection. The release workflow names a single APK
 * `novel-downloader-<ver>-android.apk` and switches to
 * `novel-downloader-<ver>-android-<abi>.apk` when several are published, so
 * the canonical name wins, then the per-ABI variant matching the device, then
 * the first project APK.
 */
object ReleaseAssets {

    private val PROJECT_PREFIXES = listOf("novel-downloader-", "noveldownloader-")

    fun isProjectApk(name: String): Boolean {
        val lower = name.trim().lowercase()
        return lower.endsWith(".apk") && PROJECT_PREFIXES.any { lower.startsWith(it) }
    }

    fun selectApk(
        assets: List<ReleaseAsset>,
        preferredAbis: List<String> = emptyList(),
    ): ReleaseAsset? {
        val apks = assets.filter { isProjectApk(it.name) }
        if (apks.isEmpty()) return null
        apks.firstOrNull { it.name.trim().lowercase().endsWith("-android.apk") }?.let {
            return it
        }
        for (abi in preferredAbis) {
            val normalized = abi.trim().lowercase()
            if (normalized.isEmpty()) continue
            apks.firstOrNull { it.name.lowercase().contains(normalized) }?.let { return it }
        }
        return apks.first()
    }
}

/** SHA-256 helpers; every path here is pure JVM (no Android APIs). */
object Checksums {

    private val SHA256_HEX = Regex("[0-9a-f]{64}")

    /** Lowercased hex from a GitHub `digest` (`sha256:<hex>`) or bare hex. */
    fun normalizeDigest(value: String?): String? {
        val text = value?.trim()?.lowercase().orEmpty().removePrefix("sha256:")
        return text.takeIf { SHA256_HEX.matches(it) }
    }

    /** Hex SHA-256 of [file]; throws [IOException] when it cannot be read. */
    fun sha256Hex(file: File): String {
        val digest = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { input ->
            val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
            while (true) {
                val read = input.read(buffer)
                if (read <= 0) break
                digest.update(buffer, 0, read)
            }
        }
        return digest.digest().joinToString("") { byte ->
            ((byte.toInt() and 0xff) + 0x100).toString(16).substring(1)
        }
    }

    /**
     * Whether [file] matches [expected]. A missing/unreadable file or a
     * malformed digest simply returns `false` — never throws.
     */
    fun verify(file: File, expected: String?): Boolean {
        val wanted = normalizeDigest(expected) ?: return false
        val actual = runCatching { sha256Hex(file) }.getOrNull() ?: return false
        return MessageDigest.isEqual(actual.toByteArray(), wanted.toByteArray())
    }
}

/**
 * Keeps only `[A-Za-z0-9._-]` characters and drops leading dots, so a hostile
 * asset name such as `../evil.apk` cannot escape the updates directory.
 *
 * Names are also capped at [MAX_FILE_NAME] characters (keeping a short `.apk`
 * style extension when possible): filesystems reject names longer than 255
 * bytes with `ENAMETOOLONG`, so a 300-character asset name would otherwise
 * make every download fail.
 */
internal fun safeFileName(raw: String, fallback: String = "update.apk"): String {
    val cleaned = buildString {
        for (char in raw.trim()) {
            val keep = char in 'a'..'z' || char in 'A'..'Z' ||
                char in '0'..'9' || char == '.' || char == '_' || char == '-'
            append(if (keep) char else '_')
        }
    }.trimStart('.')
    if (cleaned.isEmpty()) return fallback
    if (cleaned.length <= MAX_FILE_NAME) return cleaned
    val suffix = cleaned.substringAfterLast('.', "")
    val extension = if (suffix.isNotEmpty() && suffix.length <= 8) ".$suffix" else ""
    val head = cleaned.take(MAX_FILE_NAME - extension.length)
        .trimEnd('.', '_', '-')
    return head.ifEmpty { "update" } + extension
}

/** Longest file name we accept; well under the 255-byte filesystem limit. */
private const val MAX_FILE_NAME = 120

/**
 * Resolves the download target inside [directory]. Because [safeFileName]
 * removes every path separator the result always stays inside that directory;
 * that containment is what stops a hostile asset name from writing elsewhere.
 */
internal fun downloadTarget(directory: File, assetName: String?): File =
    File(directory, safeFileName(assetName ?: "update.apk"))

/**
 * Creates [directory] and deletes files left behind by an interrupted or
 * earlier download: an unverified leftover must never be reused (or handed to
 * the installer) on a retry.
 */
internal fun prepareUpdatesDir(directory: File): File {
    directory.mkdirs()
    directory.listFiles()?.forEach { if (it.isFile) it.delete() }
    return directory
}

/**
 * Last check before the APK reaches the installer. The digest is recomputed
 * here instead of trusting the download-time result, which closes the
 * time-of-check/time-of-use window (cache cleared, file replaced or truncated
 * between download and install).
 */
internal fun installPrecheck(file: File, expectedSha256: String?): UpdateFailure? {
    if (Checksums.normalizeDigest(expectedSha256) == null) return UpdateFailure.NO_CHECKSUM
    if (!Checksums.verify(file, expectedSha256)) return UpdateFailure.CHECKSUM_MISMATCH
    return null
}

/**
 * Largest APK we are willing to write when the release publishes no size.
 * The real APK is ~60 MB; the cap only exists so a hostile or misconfigured
 * endpoint cannot fill the device while the download runs.
 */
internal const val MAX_APK_BYTES: Long = 512L * 1024L * 1024L

/**
 * Space check performed before the first byte is written. A release that
 * would not fit in the cache ([expectedSize] known) is refused immediately
 * instead of failing halfway through with a raw `ENOSPC`.
 *
 * A [File.usableSpace] of 0 is treated as "unknown" (some filesystems and the
 * JVM test sandbox do not report it) rather than "full", so this can never
 * block an update on its own.
 */
internal fun storageFailure(directory: File, expectedSize: Long?): UpdateFailure? {
    val needed = expectedSize ?: return null
    if (needed <= 0L) return null
    val free = runCatching { directory.usableSpace }.getOrDefault(0L)
    return if (free in 1 until needed) UpdateFailure.INSUFFICIENT_SPACE else null
}

/**
 * Streams [input] into [target], refusing to write more than [expectedSize]
 * (or [maxBytes] when the publisher did not state a size), then verifies the
 * SHA-256 of the written file.
 *
 * The partial file is deleted on every failure — a truncated, oversized or
 * tampered download must never be left behind where a retry or the installer
 * could pick it up. Throws [UpdateException] with
 * [UpdateFailure.DOWNLOAD_FAILED] / [UpdateFailure.CHECKSUM_MISMATCH].
 */
internal fun writeVerifiedDownload(
    input: InputStream,
    target: File,
    expectedSha256: String,
    expectedSize: Long? = null,
    maxBytes: Long = MAX_APK_BYTES,
): File {
    val limit = when {
        expectedSize != null && expectedSize > 0L -> minOf(expectedSize, maxBytes)
        else -> maxBytes
    }
    try {
        var written = 0L
        target.outputStream().use { output ->
            val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
            while (true) {
                val read = input.read(buffer)
                if (read <= 0) break
                written += read
                if (written > limit) {
                    throw UpdateException(
                        UpdateFailure.DOWNLOAD_FAILED,
                        "response larger than $limit bytes",
                    )
                }
                output.write(buffer, 0, read)
            }
        }
        if (!Checksums.verify(target, expectedSha256)) {
            throw UpdateException(UpdateFailure.CHECKSUM_MISMATCH, "checksum mismatch")
        }
    } catch (e: Throwable) {
        target.delete()
        throw e
    }
    return target
}

/** 24h throttle rule for the automatic startup check. */
object UpdateSchedule {
    const val CHECK_INTERVAL_MS: Long = 24L * 60L * 60L * 1000L

    fun shouldCheck(
        autoCheck: Boolean,
        lastCheckMillis: Long,
        nowMillis: Long,
        intervalMillis: Long = CHECK_INTERVAL_MS,
    ): Boolean {
        if (!autoCheck) return false
        if (lastCheckMillis <= 0L) return true
        val elapsed = nowMillis - lastCheckMillis
        // A clock moved backwards must not pin the app to "checked recently".
        return elapsed < 0 || elapsed >= intervalMillis
    }
}

/** The installed `versionName` (`1.4.0`), or "" when the package is unreadable. */
fun appVersionName(context: Context): String = runCatching {
    context.packageManager.getPackageInfo(context.packageName, 0).versionName
}.getOrNull().orEmpty()

// ---------------------------------------------------------------------------
// GitHub payload
// ---------------------------------------------------------------------------

@Serializable
internal data class GithubAsset(
    val name: String = "",
    @SerialName("browser_download_url") val downloadUrl: String = "",
    val size: Long? = null,
    val digest: String? = null,
)

@Serializable
internal data class GithubRelease(
    @SerialName("tag_name") val tagName: String = "",
    @SerialName("html_url") val htmlUrl: String? = null,
    val assets: List<GithubAsset> = emptyList(),
)

private val releaseJson = Json {
    ignoreUnknownKeys = true
    isLenient = true
}

/**
 * Maps a raw `releases/latest` payload onto [UpdateInfo]. Malformed JSON
 * returns `null`; unknown fields, missing assets and unreadable digests are
 * tolerated (they only make the automatic install unavailable).
 */
internal fun parseRelease(
    payload: String,
    currentVersion: String,
    preferredAbis: List<String> = emptyList(),
): UpdateInfo? {
    val release = runCatching { releaseJson.decodeFromString<GithubRelease>(payload) }
        .getOrNull() ?: return null
    val latest = VersionCompare.normalize(release.tagName)
    val assets = release.assets
        .mapNotNull { asset ->
            val name = asset.name.trim()
            val url = asset.downloadUrl.trim()
            if (name.isEmpty() || url.isEmpty()) return@mapNotNull null
            ReleaseAsset(
                name = name,
                url = url,
                size = asset.size?.takeIf { it >= 0 },
                digest = Checksums.normalizeDigest(asset.digest),
            )
        }
    val apk = ReleaseAssets.selectApk(assets, preferredAbis)
    val notesUrl = release.htmlUrl?.trim()?.takeIf {
        it.startsWith("https://") || it.startsWith("http://")
    }
    return UpdateInfo(
        current = currentVersion,
        latest = latest,
        available = latest.isNotEmpty() && VersionCompare.isNewer(latest, currentVersion),
        notesUrl = notesUrl,
        assetName = apk?.name,
        assetUrl = apk?.url,
        assetSize = apk?.size,
        sha256 = apk?.digest,
    )
}

// ---------------------------------------------------------------------------
// Updater manifest (`latest.json`, published with every release)
// ---------------------------------------------------------------------------

/** The manifest schema this client understands; anything else is refused. */
internal const val MANIFEST_SCHEMA: Int = 1

/**
 * Lenient reader: a manifest from a newer publisher may carry extra fields,
 * and a future schema is rejected by the explicit [MANIFEST_SCHEMA] check
 * rather than by parse failure alone.
 */
private val manifestJson = Json {
    ignoreUnknownKeys = true
    isLenient = true
}

/** [JsonPrimitive] contents, or `null` for objects/arrays/JSON null. */
private fun JsonObject.stringOrNull(key: String): String? =
    (this[key] as? JsonPrimitive)?.contentOrNull

/**
 * Pick the entry describing the APK an Android device should install.
 *
 * The publisher writes a single `android` key when one APK serves every
 * device and `android-<abi>` / `android-universal` keys when several are
 * published, so the device's own ABI has to win over a generic one:
 *
 * 1. `android` (the canonical single-APK entry),
 * 2. `android-<abi>` for each ABI in [supportedAbis], in device order,
 * 3. `android-universal`,
 * 4. any `android-*` key whose suffix matches an ABI of the device in either
 *    direction (so `android-arm64` serves an `arm64-v8a` device),
 * 5. the first `android-*` key in key order — the API path does the same
 *    (`ReleaseAssets.selectApk` falls back to the first project APK).
 *
 * Keys are matched case-insensitively. Returns `null` when the manifest has
 * no Android entry at all, in which case the caller must not trust the
 * document (see [parseManifest]).
 */
private fun selectAndroidEntry(platforms: JsonObject, supportedAbis: List<String>): JsonObject? {
    val keys = platforms.keys.map { it to it.lowercase() }

    fun objectFor(lowerKey: String): JsonObject? =
        keys.firstOrNull { (_, lower) -> lower == lowerKey }
            ?.let { (original, _) -> platforms[original] as? JsonObject }

    objectFor("android")?.let { return it }

    val abis = supportedAbis
        .map { it.trim().lowercase() }
        .filter { it.isNotEmpty() }
        .distinct()
    for (abi in abis) {
        objectFor("android-$abi")?.let { return it }
    }
    objectFor("android-universal")?.let { return it }

    for (abi in abis) {
        keys.firstOrNull { (_, lower) ->
            lower.startsWith("android-") &&
                lower.removePrefix("android-").let { suffix ->
                    suffix.isNotEmpty() && (suffix.contains(abi) || abi.contains(suffix))
                }
        }?.let { (original, _) -> platforms[original] as? JsonObject }?.let { return it }
    }

    return keys
        .filter { (_, lower) -> lower.startsWith("android-") }
        .sortedBy { (_, lower) -> lower }
        .firstNotNullOfOrNull { (original, _) -> platforms[original] as? JsonObject }
}

/**
 * Maps an updater manifest (`latest.json`) onto [UpdateInfo].
 *
 * The manifest is the small, stable document the desktop and CLI clients read
 * first: unlike `releases/latest` it is not rate limited and carries the
 * SHA-256 of every asset. Android reads the `platforms` entry selected by
 * [supportedAbis] (`android`, then `android-<abi>`, see
 * [selectAndroidEntry]).
 *
 * Returns `null` when the document cannot be trusted as a whole — a future
 * `schema`, a missing version, no `platforms` map, no Android entry at all,
 * or an Android entry that fails the https/64-hex-digest requirements. The
 * caller then falls back to the GitHub API rather than silently concluding
 * "up to date": a manifest that forgot to list the APK must not suppress an
 * update for the next 24 hours.
 *
 * [currentVersion] defaults to empty so the document can be inspected without
 * a running version; callers that decide `available` must pass it.
 */
internal fun parseManifest(
    payload: String,
    currentVersion: String = "",
    supportedAbis: List<String> = emptyList(),
): UpdateInfo? {
    val root = runCatching { manifestJson.parseToJsonElement(payload) }.getOrNull()
        as? JsonObject ?: return null

    // A future schema may move the digest elsewhere; reading it the old way
    // would silently defeat the checksum, so it is refused outright.
    val schema = root.stringOrNull("schema")?.trim()?.toIntOrNull()
    if (schema != MANIFEST_SCHEMA) return null

    val version = root.stringOrNull("version")?.trim().orEmpty()
    if (version.isEmpty()) return null
    val latest = VersionCompare.normalize(version)

    val platforms = root["platforms"] as? JsonObject ?: return null
    if (platforms.isEmpty()) return null

    val notesUrl = root.stringOrNull("notes_url")?.trim()?.takeIf {
        it.startsWith("https://") || it.startsWith("http://")
    }

    // No Android entry means this document says nothing about phones. That is
    // not proof that no APK was published (the generator may have missed it),
    // so it is treated as unusable and the API gets the final word.
    val android = selectAndroidEntry(platforms, supportedAbis) ?: return null

    val name = android.stringOrNull("name")?.trim().orEmpty()
    if (name.isEmpty()) return null
    val url = android.stringOrNull("url")?.trim().orEmpty()
    if (!url.startsWith("https://", ignoreCase = true)) return null
    // Unlike the API payload there is no "missing digest" state here: the
    // manifest is generated by us, so an unverifiable entry is a broken
    // document and the API fallback gets a chance to supply a good one.
    val sha256 = Checksums.normalizeDigest(android.stringOrNull("sha256")) ?: return null
    val size = android.stringOrNull("size")?.trim()?.toLongOrNull()?.takeIf { it > 0 }

    return UpdateInfo(
        current = currentVersion,
        latest = latest,
        available = VersionCompare.isNewer(latest, currentVersion),
        notesUrl = notesUrl,
        assetName = name,
        assetUrl = url,
        assetSize = size,
        sha256 = sha256,
    )
}

/**
 * Looks up the newest GitHub release and picks the APK for the device.
 * Every failure (offline, HTTP error, malformed body) is reported as `null` so
 * the caller can stay silent about a check it could not complete.
 */
object UpdateChecker {

    /**
     * Stable URL of the updater manifest published with every release. Unlike
     * the GitHub API it never changes and is not rate limited, so it is asked
     * first; the API stays as the fallback for releases predating it.
     */
    const val MANIFEST_URL: String =
        "https://github.com/lingyi-ling/novel-downloader/releases/latest/download/latest.json"

    const val RELEASES_URL: String =
        "https://api.github.com/repos/lingyi-ling/novel-downloader/releases/latest"

    /** Directory under `cacheDir` holding downloads; mirrors `file_paths.xml`. */
    const val UPDATES_DIR: String = "updates"

    private const val USER_AGENT = "novel-downloader-android"

    private val jsonHeaders = mapOf(
        "Accept" to "application/vnd.github+json",
    )

    /** The manifest is a plain asset download; GitHub redirects to a CDN. */
    private val manifestHeaders = mapOf(
        "Accept" to "application/json",
    )

    /**
     * Client used for every updater request. Redirects are limited to the same
     * scheme: the pre-flight only validates the *initial* URL, so an https
     * asset that 302s to plain http must fail instead of silently downgrading
     * the transport after the check. HTTPS→HTTPS redirects (GitHub → CDN) are
     * still followed.
     *
     * [callTimeoutMs] defaults to 0 ("no overall deadline"): a metadata check
     * passes its short deadline, while an APK download must be allowed to run
     * as long as it makes progress. Connect/read timeouts still bound a dead
     * peer in every case.
     *
     * Exposed to unit tests so the policy is asserted without a TLS server.
     */
    internal fun updaterHttpClient(
        connectTimeoutMs: Long,
        readTimeoutMs: Long,
        callTimeoutMs: Long = 0L,
    ): OkHttpClient {
        val builder = updaterBaseClient.newBuilder()
            .connectTimeout(connectTimeoutMs, TimeUnit.MILLISECONDS)
            .readTimeout(readTimeoutMs, TimeUnit.MILLISECONDS)
            .followSslRedirects(false)
        if (callTimeoutMs > 0L) {
            builder.callTimeout(callTimeoutMs, TimeUnit.MILLISECONDS)
        }
        return builder.build()
    }

    /** Shares the connection pool/dispatcher between checks and downloads. */
    private val updaterBaseClient: OkHttpClient = OkHttpClient.Builder()
        .followSslRedirects(false)
        .build()

    /** GET [url], returning the bounded body or `null` for any failure. */
    private suspend fun fetchBody(
        http: OkHttpClient,
        url: String,
        headers: Map<String, String>,
        maxBytes: Long = MAX_METADATA_RESPONSE_BYTES,
    ): String? = try {
        val request = Request.Builder()
            .url(url)
            .header("User-Agent", USER_AGENT)
            .apply { headers.forEach { (name, value) -> header(name, value) } }
            .build()
        // Cancellable: leaving the screen mid-check must not pin an OkHttp
        // dispatcher thread until the read timeout.
        http.executeCancellable(request).use { response ->
            if (!response.isSuccessful) {
                null
            } else {
                response.readBoundedText(maxBytes).takeIf { it.isNotEmpty() }
            }
        }
    } catch (e: CancellationException) {
        throw e
    } catch (e: Exception) {
        null
    }

    /**
     * [client] is a test seam: unit tests inject a client with an
     * `EventListener` to observe which thread actually performs the HTTP call
     * (it must be one of the `Dispatchers.IO` workers, never the UI thread).
     * [manifestUrl] is exposed for the same reason — tests point it at a
     * loopback server, so no unit test ever touches the real endpoint.
     */
    suspend fun check(
        currentVersion: String,
        timeoutMs: Long = 8_000,
        apiUrl: String = RELEASES_URL,
        manifestUrl: String = MANIFEST_URL,
        preferredAbis: List<String> = emptyList(),
        client: OkHttpClient? = null,
    ): UpdateInfo? = withContext(Dispatchers.IO) {
        val http = client ?: updaterHttpClient(timeoutMs, timeoutMs, callTimeoutMs = timeoutMs)

        // 1. The manifest: one small request that also carries the checksums.
        // A multi-APK release publishes `android-<abi>` keys, so the device
        // ABI list decides which entry is the right one.
        fetchBody(http, manifestUrl, manifestHeaders)
            ?.let { body -> parseManifest(body, currentVersion, deviceAbis()) }
            ?.let { info -> return@withContext info }

        // 2. Fallback: releases published before the manifest existed, or a
        // manifest that could not be trusted (including one without an
        // Android entry). The API is the final authority on "up to date".
        fetchBody(http, apiUrl, jsonHeaders)
            ?.let { body -> parseRelease(body, currentVersion, preferredAbis) }
    }

    /**
     * Device ABIs for manifest entry selection. The read is guarded because
     * JVM unit tests see the stub `android.os.Build`, where the field is
     * absent — a check must degrade to "no ABI hint", never crash.
     */
    private fun deviceAbis(): List<String> =
        runCatching { Build.SUPPORTED_ABIS.toList() }.getOrDefault(emptyList())

    /**
     * Downloads [UpdateInfo.assetUrl] into `cacheDir/updates/` and verifies its
     * SHA-256. Throws [UpdateException] (an [IOException]) on any failure; the
     * partially written file is removed before the exception escapes.
     */
    suspend fun download(context: Context, info: UpdateInfo): File = withContext(Dispatchers.IO) {
        downloadRequestFailure(info)?.let { reason -> throw UpdateException(reason, reason.message()) }
        val url = info.assetUrl.orEmpty().trim()
        val expected = Checksums.normalizeDigest(info.sha256)
            ?: throw UpdateException(UpdateFailure.NO_CHECKSUM, "missing checksum")

        val directory = prepareUpdatesDir(File(context.cacheDir, UPDATES_DIR))
        // Refuse before opening the connection when the APK cannot possibly
        // fit: a mid-transfer ENOSPC would otherwise leave the user with a
        // generic failure after minutes of downloading.
        storageFailure(directory, info.assetSize)?.let { reason ->
            throw UpdateException(reason, reason.message())
        }
        // Leftovers from an interrupted run were removed by prepareUpdatesDir:
        // they were never verified and must not be reused on a retry.
        val target = downloadTarget(directory, info.assetName)

        try {
            val request = Request.Builder()
                .url(url)
                .header("User-Agent", USER_AGENT)
                .build()
            // No call timeout: a large APK on a slow link must still finish.
            // The call is cancellable, so leaving the updater stops it.
            val http = updaterHttpClient(30_000, 60_000)
            http.executeCancellable(request).use { response ->
                if (!response.isSuccessful) {
                    throw UpdateException(
                        UpdateFailure.DOWNLOAD_FAILED,
                        "HTTP ${response.code}",
                    )
                }
                val body = response.body
                    ?: throw UpdateException(UpdateFailure.DOWNLOAD_FAILED, "empty response")
                // A stated size that disagrees with the announced Content-Length
                // means the release document and the asset diverged; fail now
                // rather than writing a wrong file and rejecting it later.
                val announced = body.contentLength()
                if (info.assetSize != null && info.assetSize > 0L &&
                    announced > 0L && announced != info.assetSize
                ) {
                    throw UpdateException(
                        UpdateFailure.DOWNLOAD_FAILED,
                        "size mismatch: expected ${info.assetSize}, got $announced",
                    )
                }
                writeVerifiedDownload(
                    input = body.byteStream(),
                    target = target,
                    expectedSha256 = expected,
                    expectedSize = info.assetSize,
                )
            }
        } catch (e: CancellationException) {
            target.delete()
            throw e
        } catch (e: UpdateException) {
            target.delete()
            throw e
        } catch (e: Exception) {
            target.delete()
            val reason = UpdateFailure.DOWNLOAD_FAILED
            throw UpdateException(reason, e.message ?: "download failed", e)
        }
        target
    }
}
