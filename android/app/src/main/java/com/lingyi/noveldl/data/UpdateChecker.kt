package com.lingyi.noveldl.data

import android.content.Context
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.File
import java.io.IOException
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
     */
    fun parse(value: String): List<Int> {
        val head = value.trim().substringBefore('-').trimStart('v', 'V')
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

/**
 * Looks up the newest GitHub release and picks the APK for the device.
 * Every failure (offline, HTTP error, malformed body) is reported as `null` so
 * the caller can stay silent about a check it could not complete.
 */
object UpdateChecker {

    const val RELEASES_URL: String =
        "https://api.github.com/repos/lingyi-ling/novel-downloader/releases/latest"

    /** Directory under `cacheDir` holding downloads; mirrors `file_paths.xml`. */
    const val UPDATES_DIR: String = "updates"

    private const val USER_AGENT = "novel-downloader-android"

    private val jsonHeaders = mapOf(
        "Accept" to "application/vnd.github+json",
        "User-Agent" to USER_AGENT,
    )

    private fun client(connectTimeoutMs: Long, readTimeoutMs: Long): OkHttpClient =
        OkHttpClient.Builder()
            .connectTimeout(connectTimeoutMs, TimeUnit.MILLISECONDS)
            .readTimeout(readTimeoutMs, TimeUnit.MILLISECONDS)
            .build()

    /**
     * [client] is a test seam: unit tests inject a client with an
     * `EventListener` to observe which thread actually performs the HTTP call
     * (it must be one of the `Dispatchers.IO` workers, never the UI thread).
     */
    suspend fun check(
        currentVersion: String,
        timeoutMs: Long = 8_000,
        apiUrl: String = RELEASES_URL,
        preferredAbis: List<String> = emptyList(),
        client: OkHttpClient? = null,
    ): UpdateInfo? = withContext(Dispatchers.IO) {
        try {
            val request = Request.Builder()
                .url(apiUrl)
                .apply { jsonHeaders.forEach { (name, value) -> header(name, value) } }
                .build()
            (client ?: client(timeoutMs, timeoutMs)).newCall(request).execute().use { response ->
                if (!response.isSuccessful) return@withContext null
                val body = response.body?.string().orEmpty()
                if (body.isEmpty()) return@withContext null
                parseRelease(body, currentVersion, preferredAbis)
            }
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            null
        }
    }

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
        // Leftovers from an interrupted run were removed by prepareUpdatesDir:
        // they were never verified and must not be reused on a retry.
        val target = downloadTarget(directory, info.assetName)

        try {
            val request = Request.Builder()
                .url(url)
                .header("User-Agent", USER_AGENT)
                .build()
            // No call timeout: a large APK on a slow link must still finish.
            client(30_000, 60_000).newCall(request).execute().use { response ->
                if (!response.isSuccessful) {
                    throw UpdateException(
                        UpdateFailure.DOWNLOAD_FAILED,
                        "HTTP ${response.code}",
                    )
                }
                val body = response.body
                    ?: throw UpdateException(UpdateFailure.DOWNLOAD_FAILED, "empty response")
                body.byteStream().use { input ->
                    target.outputStream().use { output -> input.copyTo(output) }
                }
            }
            if (!Checksums.verify(target, expected)) {
                target.delete()
                throw UpdateException(UpdateFailure.CHECKSUM_MISMATCH, "checksum mismatch")
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
