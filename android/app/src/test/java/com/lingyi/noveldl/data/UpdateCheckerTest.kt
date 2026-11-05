package com.lingyi.noveldl.data

import androidx.datastore.preferences.core.preferencesOf
import androidx.datastore.preferences.core.stringPreferencesKey
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.runBlocking
import okhttp3.Call
import okhttp3.EventListener
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File
import java.util.concurrent.atomic.AtomicReference

/**
 * Pure logic behind the in-app updater: version ordering, APK selection,
 * SHA-256 verification, file-name hardening and the 24h throttle. Nothing here
 * touches the network or the Android framework, so the whole file runs on the
 * JVM (`testDebugUnitTest`) with fabricated release payloads.
 */
class UpdateCheckerTest {

    @get:Rule
    val folder = TemporaryFolder()

    private val digest = "a".repeat(64)

    // ------------------------------------------------------------------
    // VersionCompare
    // ------------------------------------------------------------------

    @Test
    fun newerPatchIsAnUpdate() {
        assertTrue(VersionCompare.isNewer("1.4.1", "1.4.0"))
        assertFalse(VersionCompare.isNewer("1.4.0", "1.4.1"))
        assertFalse(VersionCompare.isNewer("1.4.0", "1.4.0"))
    }

    @Test
    fun shorterVersionIsZeroPadded() {
        assertEquals(listOf(1, 4), VersionCompare.parse("1.4"))
        assertEquals(listOf(1, 4, 0), VersionCompare.parse("1.4.0"))
        // 1.4 == 1.4.0, so neither side is "newer".
        assertFalse(VersionCompare.isNewer("1.4", "1.4.0"))
        assertFalse(VersionCompare.isNewer("1.4.0", "1.4"))
        // A missing segment is not a new version either.
        assertTrue(VersionCompare.isNewer("1.5", "1.4.9"))
        assertFalse(VersionCompare.isNewer("1.4.9", "1.5"))
    }

    @Test
    fun leadingVPrefixIsIgnored() {
        assertEquals(listOf(1, 4, 1), VersionCompare.parse("v1.4.1"))
        assertEquals(listOf(1, 4, 1), VersionCompare.parse("V1.4.1"))
        assertTrue(VersionCompare.isNewer("v1.4.1", "1.4.0"))
        assertEquals("1.4.1", VersionCompare.normalize("v1.4.1"))
        assertEquals("1.4.1", VersionCompare.normalize(" 1.4.1 "))
    }

    @Test
    fun suffixIsIgnored() {
        assertEquals(listOf(1, 4, 1), VersionCompare.parse("1.4.1-rc1"))
        assertTrue(VersionCompare.isNewer("1.4.1-rc1", "1.4.0"))
        // A release candidate of the running version is not an upgrade.
        assertFalse(VersionCompare.isNewer("1.4.0-rc2", "1.4.0"))
    }

    @Test
    fun garbageNeverLooksNewer() {
        assertEquals(listOf(0), VersionCompare.parse("abc"))
        assertEquals(listOf(0), VersionCompare.parse(""))
        assertEquals(listOf(0), VersionCompare.parse("-"))
        assertFalse(VersionCompare.isNewer("abc", "1.4.0"))
        assertFalse(VersionCompare.isNewer("", "1.4.0"))
        assertFalse(VersionCompare.isNewer("garbage", "garbage"))
        // Trailing non-numeric segments stop parsing instead of throwing.
        assertEquals(listOf(1, 4), VersionCompare.parse("1.4.x"))
        assertFalse(VersionCompare.isNewer("1.4.x", "1.4.0"))
    }

    // ------------------------------------------------------------------
    // Asset selection
    // ------------------------------------------------------------------

    private fun apk(name: String) = ReleaseAsset(
        name = name,
        url = "https://github.com/lingyi-ling/novel-downloader/releases/download/v1.4.1/$name",
        size = 1024L,
        digest = "sha256:$digest",
    )

    @Test
    fun singleApkReleaseIsSelected() {
        val assets = listOf(
            apk("novel-downloader-1.4.1-android.apk"),
            ReleaseAsset(
                name = "novel-downloader-1.4.1-windows-x64.zip",
                url = "https://example.invalid/bundle.zip",
                size = 2048L,
                digest = null,
            ),
        )

        val chosen = ReleaseAssets.selectApk(assets)

        assertEquals("novel-downloader-1.4.1-android.apk", chosen?.name)
    }

    @Test
    fun multiAbiReleasePrefersTheDeviceAbi() {
        val assets = listOf(
            apk("novel-downloader-1.4.1-android-arm64-v8a.apk"),
            apk("novel-downloader-1.4.1-android-x86_64.apk"),
        )

        assertEquals(
            "novel-downloader-1.4.1-android-x86_64.apk",
            ReleaseAssets.selectApk(assets, preferredAbis = listOf("x86_64", "arm64-v8a"))?.name,
        )
        assertEquals(
            "novel-downloader-1.4.1-android-arm64-v8a.apk",
            ReleaseAssets.selectApk(assets, preferredAbis = listOf("arm64-v8a"))?.name,
        )
        // No ABI hint (or an unknown one) still yields a project APK.
        assertEquals(
            "novel-downloader-1.4.1-android-arm64-v8a.apk",
            ReleaseAssets.selectApk(assets, preferredAbis = listOf("mips"))?.name,
        )
    }

    @Test
    fun canonicalNameWinsOverAbiVariants() {
        val assets = listOf(
            apk("novel-downloader-1.4.1-android-x86_64.apk"),
            apk("novel-downloader-1.4.1-android.apk"),
        )

        assertEquals(
            "novel-downloader-1.4.1-android.apk",
            ReleaseAssets.selectApk(assets, preferredAbis = listOf("x86_64"))?.name,
        )
    }

    @Test
    fun nonApkAssetsAreIgnored() {
        val assets = listOf(
            ReleaseAsset("novel-downloader-1.4.1-windows-x64.zip", "https://e.invalid/a.zip", 1L, null),
            ReleaseAsset("SHA256SUMS.txt", "https://e.invalid/sums.txt", 1L, null),
            // An APK from another project must never be offered as our update.
            ReleaseAsset("other-app-1.4.1.apk", "https://e.invalid/b.apk", 1L, null),
        )

        assertNull(ReleaseAssets.selectApk(assets))
        assertNull(ReleaseAssets.selectApk(emptyList()))
    }

    // ------------------------------------------------------------------
    // Release payload mapping
    // ------------------------------------------------------------------

    private val releasePayload = """
        {
          "tag_name": "v1.4.1",
          "html_url": "https://github.com/lingyi-ling/novel-downloader/releases/tag/v1.4.1",
          "draft": false,
          "prerelease": false,
          "body": "ignored",
          "assets": [
            {
              "name": "novel-downloader-1.4.1-android-arm64-v8a.apk",
              "browser_download_url": "https://github.com/lingyi-ling/novel-downloader/releases/download/v1.4.1/novel-downloader-1.4.1-android-arm64-v8a.apk",
              "size": 12345678,
              "digest": "sha256:$digest"
            },
            {
              "name": "novel-downloader-1.4.1-android-x86_64.apk",
              "browser_download_url": "https://github.com/lingyi-ling/novel-downloader/releases/download/v1.4.1/novel-downloader-1.4.1-android-x86_64.apk",
              "size": 22345678,
              "digest": null
            },
            {
              "name": "novel-downloader-1.4.1-windows-x64.zip",
              "browser_download_url": "https://github.com/lingyi-ling/novel-downloader/releases/download/v1.4.1/bundle.zip",
              "size": 999,
              "digest": null
            }
          ]
        }
    """.trimIndent()

    @Test
    fun payloadMapsOntoUpdateInfo() {
        val info = parseRelease(releasePayload, "1.4.0", listOf("arm64-v8a"))!!

        assertEquals("1.4.0", info.current)
        assertEquals("1.4.1", info.latest)
        assertTrue(info.available)
        assertEquals(
            "https://github.com/lingyi-ling/novel-downloader/releases/tag/v1.4.1",
            info.notesUrl,
        )
        assertEquals("novel-downloader-1.4.1-android-arm64-v8a.apk", info.assetName)
        assertEquals(12_345_678L, info.assetSize)
        assertEquals(digest, info.sha256)
        assertTrue(info.assetUrl!!.startsWith("https://"))
    }

    @Test
    fun missingDigestMeansTheAssetCannotBeVerified() {
        // The x86_64 asset has digest = null.
        val info = parseRelease(releasePayload, "1.4.0", listOf("x86_64"))!!

        assertEquals("novel-downloader-1.4.1-android-x86_64.apk", info.assetName)
        assertNull(info.sha256)
    }

    @Test
    fun currentVersionIsNotAnUpdate() {
        val info = parseRelease(releasePayload, "1.4.1", listOf("arm64-v8a"))!!

        assertFalse(info.available)
    }

    @Test
    fun malformedPayloadReturnsNull() {
        assertNull(parseRelease("not json", "1.4.0"))
        assertNull(parseRelease("", "1.4.0"))
    }

    @Test
    fun releaseWithoutApkStillReportsTheVersion() {
        val info = parseRelease(
            """
            {"tag_name":"v2.0.0","assets":[{"name":"bundle.zip",
            "browser_download_url":"https://e.invalid/bundle.zip","size":1}]}
            """.trimIndent(),
            "1.4.0",
        )!!

        assertTrue(info.available)
        assertNull(info.assetName)
        assertNull(info.assetUrl)
        assertNull(info.sha256)
    }

    // ------------------------------------------------------------------
    // Updater manifest (`latest.json`)
    // ------------------------------------------------------------------

    /** Digest of the real 1.5.0 Android APK, taken from the published manifest. */
    private val apkSha = "7133f789a9a3b43d8aae5897fd41f62a5e91d45dba6f82d80fcc4e5a2cce0ac7"

    private val apkUrl =
        "https://github.com/lingyi-ling/novel-downloader/releases/download/v1.5.0/" +
            "novel-downloader-1.5.0-android.apk"

    private fun androidEntry(
        key: String = "android",
        name: String? = "novel-downloader-1.5.0-android.apk",
        url: String? = apkUrl,
        sha256: String? = apkSha,
        size: Long? = 62_714_163L,
    ): String {
        val fields = buildList {
            name?.let { add("\"name\": \"$it\"") }
            sha256?.let { add("\"sha256\": \"$it\"") }
            size?.let { add("\"size\": $it") }
            url?.let { add("\"url\": \"$it\"") }
        }
        return "\"$key\": {" + fields.joinToString(",") + "}"
    }

    /** A per-ABI entry such as `android-arm64-v8a`, distinguishable by name. */
    private fun abiEntry(abi: String): String {
        val name = "novel-downloader-1.5.0-android-$abi.apk"
        return androidEntry(
            key = "android-$abi",
            name = name,
            url = "https://github.com/lingyi-ling/novel-downloader/releases/download/v1.5.0/$name",
        )
    }

    private val windowsEntry = """
        "windows-x64-portable": {
          "name": "novel-downloader-1.5.0-windows-x64.zip",
          "sha256": "${"b".repeat(64)}",
          "size": 1234,
          "url": "https://github.com/lingyi-ling/novel-downloader/releases/download/v1.5.0/portable.zip"
        }
    """.trimIndent()

    private fun manifest(
        schema: Int = 1,
        version: String = "1.5.0",
        platforms: String = androidEntry(),
        withSchema: Boolean = true,
    ): String = buildString {
        append("{\n")
        if (withSchema) append("  \"schema\": $schema,\n")
        append("  \"version\": \"$version\",\n")
        append("  \"tag\": \"v$version\",\n")
        append("  \"repo\": \"lingyi-ling/novel-downloader\",\n")
        append("  \"notes_url\": ")
        append("\"https://github.com/lingyi-ling/novel-downloader/releases/tag/v$version\",\n")
        append("  \"platforms\": {\n$platforms\n  }\n")
        append("}")
    }

    @Test
    fun manifestMapsOntoUpdateInfo() {
        // The real published document also carries the Windows targets; the
        // Android client must ignore them and pick `platforms["android"]`.
        val payload = manifest(platforms = androidEntry() + ",\n" + windowsEntry)

        val info = parseManifest(payload, "1.4.1")!!

        assertEquals("1.4.1", info.current)
        assertEquals("1.5.0", info.latest)
        assertTrue(info.available)
        assertEquals(
            "https://github.com/lingyi-ling/novel-downloader/releases/tag/v1.5.0",
            info.notesUrl,
        )
        assertEquals("novel-downloader-1.5.0-android.apk", info.assetName)
        assertEquals(
            "https://github.com/lingyi-ling/novel-downloader/releases/download/v1.5.0/" +
                "novel-downloader-1.5.0-android.apk",
            info.assetUrl,
        )
        assertEquals(62_714_163L, info.assetSize)
        assertEquals(apkSha, info.sha256)
        // The parsed info passes the download pre-flight unchanged.
        assertNull(downloadRequestFailure(info))
    }

    @Test
    fun manifestSchemaMustBeExactlyOne() {
        // A future format may move the digest; guessing would defeat the
        // checksum, so the whole document is rejected and the API is used.
        assertNull(parseManifest(manifest(schema = 2), "1.4.1"))
        assertNull(parseManifest(manifest(schema = 0), "1.4.1"))
        assertNull(parseManifest(manifest(withSchema = false), "1.4.1"))
        assertNull(parseManifest("", "1.4.1"))
        assertNull(parseManifest("not json", "1.4.1"))
        assertNull(parseManifest("[]", "1.4.1"))
    }

    @Test
    fun manifestWithoutPlatformsIsRejected() {
        // No platform map at all is a malformed document, not "no APK": the
        // API fallback still has a chance to provide a usable asset.
        assertNull(
            parseManifest(
                """
                {"schema":1,"version":"1.5.0",
                 "notes_url":"https://github.com/lingyi-ling/novel-downloader/releases/tag/v1.5.0"}
                """.trimIndent(),
                "1.4.1",
            ),
        )
        assertNull(parseManifest(manifest(platforms = ""), "1.4.1"))
        assertNull(
            parseManifest("""{"schema":1,"version":"1.5.0","platforms":[]}""", "1.4.1"),
        )
        // A manifest without a version cannot be compared to anything.
        assertNull(
            parseManifest("""{"schema":1,"platforms":{"android":{}}}""", "1.4.1"),
        )
    }

    @Test
    fun manifestWithoutAndroidEntryIsRejected() {
        // The desktop/CLI manifest can legitimately list only Windows targets,
        // but for Android that is *not* "no update": the generator may simply
        // have missed the APK. Returning null makes check() ask the API, so a
        // missing key can never pin the app to "up to date" for 24 hours.
        assertNull(parseManifest(manifest(platforms = windowsEntry), "1.4.1"))
        assertNull(
            parseManifest(
                """{"schema":1,"version":"1.5.0","platforms":{"windows-x64-installer":{}}}""",
                "1.4.1",
            ),
        )
        // Also null when `android` is present but is not an object.
        assertNull(parseManifest(manifest(platforms = "\"android\": \"x.apk\""), "1.4.1"))
        // A non-Android key whose name contains a device ABI must never be
        // mistaken for an APK by the partial ABI match.
        assertNull(
            parseManifest(
                manifest(
                    platforms = "\"windows-arm64\": {\"name\":\"a.exe\"," +
                        "\"url\":\"https://e.invalid/a.exe\"," +
                        "\"sha256\":\"${"c".repeat(64)}\"}",
                ),
                "1.4.1",
                listOf("arm64-v8a"),
            ),
        )
    }

    @Test
    fun manifestSingleApkEntryWinsOverAbiEntries() {
        // A canonical single APK serves every device, so it is preferred over
        // any `android-<abi>` variant regardless of the device ABI list.
        val payload = manifest(
            platforms = listOf(androidEntry(), abiEntry("arm64-v8a"), abiEntry("x86_64"))
                .joinToString(",\n"),
        )

        for (abis in listOf(emptyList(), listOf("arm64-v8a"), listOf("x86_64"))) {
            val info = parseManifest(payload, "1.4.1", abis)!!
            assertEquals("novel-downloader-1.5.0-android.apk", info.assetName)
        }
    }

    @Test
    fun manifestMultiApkSelectsTheDeviceAbi() {
        val payload = manifest(
            platforms = listOf(abiEntry("arm64-v8a"), abiEntry("x86_64")).joinToString(",\n"),
        )

        // The first supported ABI wins, mirroring the API path's preference.
        assertEquals(
            "novel-downloader-1.5.0-android-x86_64.apk",
            parseManifest(payload, "1.4.1", listOf("x86_64", "arm64-v8a"))!!.assetName,
        )
        assertEquals(
            "novel-downloader-1.5.0-android-arm64-v8a.apk",
            parseManifest(payload, "1.4.1", listOf("arm64-v8a"))!!.assetName,
        )
        // A shorter key (`android-arm64`) still matches the fuller ABI name.
        val shortened = manifest(platforms = abiEntry("arm64"))
        assertEquals(
            "novel-downloader-1.5.0-android-arm64.apk",
            parseManifest(shortened, "1.4.1", listOf("arm64-v8a"))!!.assetName,
        )
        // The URL of the chosen entry travels with it.
        assertEquals(
            "https://github.com/lingyi-ling/novel-downloader/releases/download/v1.5.0/" +
                "novel-downloader-1.5.0-android-x86_64.apk",
            parseManifest(payload, "1.4.1", listOf("x86_64"))!!.assetUrl,
        )
    }

    @Test
    fun manifestMultiApkFallsBackWhenTheAbiIsUnknown() {
        val withUniversal = manifest(
            platforms = listOf(
                abiEntry("arm64-v8a"),
                abiEntry("x86_64"),
                abiEntry("universal"),
            ).joinToString(",\n"),
        )
        // No exact or partial ABI match: the ABI-neutral entry is the safe pick.
        assertEquals(
            "novel-downloader-1.5.0-android-universal.apk",
            parseManifest(withUniversal, "1.4.1", listOf("mips"))!!.assetName,
        )

        // Without a universal entry the first android-* key is used, exactly
        // like ReleaseAssets.selectApk falls back to the first project APK.
        val abiOnly = manifest(
            platforms = listOf(abiEntry("x86_64"), abiEntry("arm64-v8a")).joinToString(",\n"),
        )
        assertEquals(
            "novel-downloader-1.5.0-android-arm64-v8a.apk",
            parseManifest(abiOnly, "1.4.1", listOf("mips"))!!.assetName,
        )
        // No ABI hint at all (JVM tests, unusual devices) resolves the same way.
        assertEquals(
            "novel-downloader-1.5.0-android-arm64-v8a.apk",
            parseManifest(abiOnly, "1.4.1")!!.assetName,
        )
    }

    @Test
    fun manifestAndroidEntryMustCarryNameHttpsUrlAndSha256() {
        fun rejected(vararg entries: String): Boolean =
            parseManifest(manifest(platforms = entries.joinToString(",\n")), "1.4.1") == null

        // sha256 must be exactly 64 hex characters.
        assertTrue(rejected(androidEntry(sha256 = "a".repeat(63))))
        assertTrue(rejected(androidEntry(sha256 = "a".repeat(65))))
        assertTrue(rejected(androidEntry(sha256 = "z".repeat(64))))
        // No digest at all: the API path may publish one instead.
        assertTrue(rejected(androidEntry(sha256 = null)))
        // Plain http is refused even with a valid digest.
        assertTrue(rejected(androidEntry(url = apkUrl.replace("https://", "http://"))))
        // A missing name or url makes the entry unusable as well.
        assertTrue(rejected(androidEntry(name = null)))
        assertTrue(rejected(androidEntry(name = "  ")))
        assertTrue(rejected(androidEntry(url = null)))
        // `android` that is not an object is equally unusable.
        assertTrue(rejected("\"android\": \"novel-downloader.apk\""))
        // A well-formed entry is accepted (guards the assertions above).
        assertFalse(rejected(androidEntry()))
    }

    @Test
    fun manifestToleratesExtraKeysAndChecksumPrefix() {
        val info = parseManifest(
            manifest(
                platforms = "\"android\": {" +
                    "\"name\": \"novel-downloader-1.5.0-android.apk\"," +
                    "\"sha256\": \"sha256:${apkSha.uppercase()}\"," +
                    "\"url\": \"$apkUrl\"," +
                    "\"future_field\": 42}",
            ),
            "1.4.1",
        )!!

        assertEquals(apkSha, info.sha256)
        assertNull(info.assetSize)
        assertTrue(info.available)
    }

    @Test
    fun manifestVersionComparisonDecidesAvailability() {
        val payload = manifest()

        assertTrue(parseManifest(payload, "1.4.1")!!.available)
        assertFalse(parseManifest(payload, "1.5.0")!!.available)
        assertFalse(parseManifest(payload, "1.6.0")!!.available)
        // A leading `v` in `version` is normalised like a release tag.
        val tagged = parseManifest(manifest(version = "v1.5.0"), "1.4.1")!!
        assertEquals("1.5.0", tagged.latest)
        assertTrue(tagged.available)
    }

    // ------------------------------------------------------------------
    // Checksums
    // ------------------------------------------------------------------

    @Test
    fun sha256MatchesForAnIntactFile() {
        val file = folder.newFile("good.apk")
        file.writeText("hello")
        val expected = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"

        assertEquals(expected, Checksums.sha256Hex(file))
        assertTrue(Checksums.verify(file, expected))
        assertTrue(Checksums.verify(file, "sha256:$expected"))
        assertTrue(Checksums.verify(file, expected.uppercase()))
    }

    @Test
    fun sha256RejectsATamperedOrShortFile() {
        val file = folder.newFile("tampered.apk")
        file.writeText("hello")
        val expected = "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"

        file.writeText("hello!")

        assertFalse(Checksums.verify(file, expected))
        assertFalse(Checksums.verify(file, "0".repeat(64)))
    }

    @Test
    fun missingFileFailsVerificationInsteadOfThrowing() {
        val missing = File(folder.root, "nope.apk")

        assertFalse(Checksums.verify(missing, "a".repeat(64)))
        assertFalse(Checksums.verify(missing, null))
        // A malformed digest can never verify, even for an existing file.
        assertFalse(Checksums.verify(folder.newFile("x.apk"), "sha256:nothex"))
    }

    @Test
    fun digestIsNormalisedBeforeComparing() {
        assertEquals(digest, Checksums.normalizeDigest("sha256:$digest"))
        assertEquals(digest, Checksums.normalizeDigest(digest.uppercase()))
        assertNull(Checksums.normalizeDigest(null))
        assertNull(Checksums.normalizeDigest("sha256:"))
        assertNull(Checksums.normalizeDigest("md5:$digest"))
        assertNull(Checksums.normalizeDigest("a".repeat(63)))
    }

    // ------------------------------------------------------------------
    // Download pre-flight
    // ------------------------------------------------------------------

    private fun info(
        assetUrl: String? = "https://e.invalid/novel-downloader-1.4.1-android.apk",
        sha256: String? = "a".repeat(64),
    ) = UpdateInfo(
        current = "1.4.0",
        latest = "1.4.1",
        available = true,
        notesUrl = null,
        assetName = "novel-downloader-1.4.1-android.apk",
        assetUrl = assetUrl,
        assetSize = null,
        sha256 = sha256,
    )

    @Test
    fun downloadIsRefusedWithoutAVerifiableChecksum() {
        // No digest published by the release: manual update only.
        assertEquals(UpdateFailure.NO_CHECKSUM, downloadRequestFailure(info(sha256 = null)))
        assertEquals(
            UpdateFailure.NO_CHECKSUM,
            downloadRequestFailure(info(sha256 = "sha256:not-a-digest")),
        )
        // An http:// asset is refused even when the digest is present.
        assertEquals(
            UpdateFailure.INSECURE_URL,
            downloadRequestFailure(info(assetUrl = "http://e.invalid/app.apk")),
        )
        assertEquals(UpdateFailure.NO_ASSET, downloadRequestFailure(info(assetUrl = null)))
        assertEquals(UpdateFailure.NO_ASSET, downloadRequestFailure(info(assetUrl = "  ")))
        // A complete, verifiable https asset passes the pre-flight.
        assertNull(downloadRequestFailure(info()))
    }

    // ------------------------------------------------------------------
    // File names
    // ------------------------------------------------------------------

    @Test
    fun hostileAssetNamesCannotEscapeTheUpdatesDirectory() {
        assertEquals("_evil.apk", safeFileName("../evil.apk"))
        assertEquals("_evil.apk", safeFileName("..\\evil.apk"))
        assertEquals("a_b.apk", safeFileName("a/b.apk"))
        assertEquals("novel-downloader-1.4.1-android-arm64-v8a.apk",
            safeFileName("novel-downloader-1.4.1-android-arm64-v8a.apk"))
        assertEquals("___", safeFileName("/\\:"))
        assertEquals("update.apk", safeFileName(""))
        assertEquals("update.apk", safeFileName("..."))
    }

    // ------------------------------------------------------------------
    // 24h throttle
    // ------------------------------------------------------------------

    @Test
    fun startupCheckRunsAtMostOncePerDay() {
        val day = UpdateSchedule.CHECK_INTERVAL_MS
        val now = 1_700_000_000_000L

        assertTrue(UpdateSchedule.shouldCheck(autoCheck = true, lastCheckMillis = 0L, nowMillis = now))
        assertTrue(UpdateSchedule.shouldCheck(true, now - day, now))
        assertFalse(UpdateSchedule.shouldCheck(true, now - day + 1, now))
        assertFalse(UpdateSchedule.shouldCheck(true, now, now))
        // Switching the feature off always wins.
        assertFalse(UpdateSchedule.shouldCheck(false, 0L, now))
        // A clock that jumped backwards must not disable checking forever.
        assertTrue(UpdateSchedule.shouldCheck(true, now + day, now))
    }

    @Test
    fun updatePreferencesMapOntoApiSettings() = runBlocking {
        val settings = flowOf(
            preferencesOf(
                autoCheckUpdatesKey to false,
                lastUpdateCheckKey to 42L,
            ),
        ).toApiSettings().first()

        assertFalse(settings.autoCheckUpdates)
        assertEquals(42L, settings.lastUpdateCheck)

        val defaults = flowOf(preferencesOf()).toApiSettings().first()
        assertTrue(defaults.autoCheckUpdates)
        assertEquals(0L, defaults.lastUpdateCheck)
    }

    @Test
    fun garbagePersistedValuesFallBackToDefaultsInsteadOfCrashing() = runBlocking {
        // A preferences file written by another version (or corrupted) can hold
        // a String under the Long/Boolean keys. Preferences.get returns the raw
        // object, so an unchecked read would throw ClassCastException at the
        // call site and kill the whole settings flow.
        val settings = flowOf(
            preferencesOf(
                stringPreferencesKey("last_update_check") to "garbage",
                stringPreferencesKey("auto_check_updates") to "not-a-boolean",
            ),
        ).toApiSettings().first()

        assertEquals(0L, settings.lastUpdateCheck)
        assertTrue(settings.autoCheckUpdates)
        // Garbage is treated as "never checked", so the check still runs.
        assertTrue(
            UpdateSchedule.shouldCheck(
                settings.autoCheckUpdates,
                settings.lastUpdateCheck,
                nowMillis = 1_700_000_000_000L,
            ),
        )
    }

    // ------------------------------------------------------------------
    // Trust chain: schemes and install-time re-verification
    // ------------------------------------------------------------------

    private val helloDigest =
        "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"

    @Test
    fun onlyHttpsAssetUrlsPassThePreFlight() {
        assertEquals(
            UpdateFailure.INSECURE_URL,
            downloadRequestFailure(info(assetUrl = "http://cdn.example/app.apk")),
        )
        assertEquals(
            UpdateFailure.INSECURE_URL,
            downloadRequestFailure(info(assetUrl = "file:///data/local/tmp/app.apk")),
        )
        assertEquals(
            UpdateFailure.INSECURE_URL,
            downloadRequestFailure(info(assetUrl = "ftp://cdn.example/app.apk")),
        )
        assertEquals(
            UpdateFailure.INSECURE_URL,
            downloadRequestFailure(info(assetUrl = "content://evil/app.apk")),
        )
        // Scheme comparison is case insensitive.
        assertNull(downloadRequestFailure(info(assetUrl = "HTTPS://cdn.example/app.apk")))
    }

    @Test
    fun installRecomputesTheDigestRightBeforeInstalling() {
        val file = folder.newFile("ready.apk")
        file.writeText("hello")
        assertNull(installPrecheck(file, helloDigest))

        // Replaced between download and install (TOCTOU): refuse, never install.
        file.writeText("hello!")
        assertEquals(UpdateFailure.CHECKSUM_MISMATCH, installPrecheck(file, helloDigest))

        // Evicted from the cache after being marked ready: same refusal instead
        // of handing a missing file to FileProvider/the installer.
        assertTrue(file.delete())
        assertEquals(UpdateFailure.CHECKSUM_MISMATCH, installPrecheck(file, helloDigest))

        // A release without a digest can never be installed automatically.
        assertEquals(UpdateFailure.NO_CHECKSUM, installPrecheck(file, null))
    }

    // ------------------------------------------------------------------
    // Filesystem: target resolution and retries
    // ------------------------------------------------------------------

    @Test
    fun downloadTargetCannotEscapeTheUpdatesDirectory() {
        val directory = File(folder.root, UpdateChecker.UPDATES_DIR).apply { mkdirs() }

        val hostile = listOf(
            "../evil.apk",
            "../../etc/passwd",
            "a/b.apk",
            "..\\evil.apk",
            "/sdcard/evil.apk",
            "....//evil.apk",
        )
        for (name in hostile) {
            val target = downloadTarget(directory, name)
            assertEquals(
                "asset name $name escaped the updates directory",
                directory.canonicalPath,
                target.canonicalFile.parentFile!!.canonicalPath,
            )
        }
        assertEquals(directory.canonicalPath, downloadTarget(directory, null)
            .canonicalFile.parentFile!!.canonicalPath)
        assertEquals("update.apk", downloadTarget(directory, null).name)
    }

    @Test
    fun leftoversFromAFailedDownloadAreRemovedBeforeRetrying() {
        val directory = File(folder.root, UpdateChecker.UPDATES_DIR).apply { mkdirs() }
        val stale = File(directory, "update.apk").apply { writeText("tampered") }

        prepareUpdatesDir(directory)

        assertFalse(stale.exists())
        assertTrue(directory.isDirectory)
    }

    @Test
    fun repeatedDownloadOfTheSameNameOverwritesInsteadOfFailing() {
        val directory = File(folder.root, UpdateChecker.UPDATES_DIR).apply { mkdirs() }
        val name = "novel-downloader-1.4.1-android.apk"
        downloadTarget(directory, name).writeText("first")

        prepareUpdatesDir(directory)
        val second = downloadTarget(directory, name)
        second.writeText("second")

        assertEquals("second", second.readText())
    }

    @Test
    fun longAssetNamesAreTruncatedToAFilesystemSafeLength() {
        val name = safeFileName("a".repeat(300) + ".apk")

        assertTrue("length ${name.length}", name.length <= 120)
        assertTrue(name, name.endsWith(".apk"))
        // Without a recognisable extension the cap still applies.
        assertEquals(120, safeFileName("b".repeat(300)).length)
        // Android runs on Linux, so DOS device names are ordinary names.
        assertEquals("con.apk", safeFileName("con.apk"))
    }

    // ------------------------------------------------------------------
    // Payload edge cases
    // ------------------------------------------------------------------

    @Test
    fun numericVersionSegmentsAreNotComparedAsText() {
        assertTrue(VersionCompare.isNewer("1.10.0", "1.9.0"))
        assertFalse(VersionCompare.isNewer("1.9.0", "1.10.0"))
        assertTrue(VersionCompare.isNewer("2.0", "1.99.99"))
        assertTrue(VersionCompare.isNewer("v1.4.0", "1.3.9"))
        assertFalse(VersionCompare.isNewer("v1.4.0", "1.4.0"))
    }

    @Test
    fun missingOrMalformedPayloadFieldsAreTolerated() {
        // `assets` is not an array: nothing usable, treated as a failed check.
        assertNull(parseRelease("""{"tag_name":"v2.0.0","assets":{}}""", "1.4.0"))

        // `assets` missing: the version is still reported, without an asset.
        val noAssets = parseRelease("""{"tag_name":"v2.0.0"}""", "1.4.0")!!
        assertTrue(noAssets.available)
        assertNull(noAssets.assetName)
        assertNull(noAssets.assetUrl)

        // `tag_name` missing: empty version, never an update.
        val noTag = parseRelease("""{"assets":[]}""", "1.4.0")!!
        assertEquals("", noTag.latest)
        assertFalse(noTag.available)
        assertEquals(UpdateFailure.NO_ASSET, downloadRequestFailure(noTag))
    }

    @Test
    fun nonSha256DigestMeansManualUpdateOnly() {
        val info = parseRelease(
            """
            {"tag_name":"v1.4.1","assets":[{
              "name":"novel-downloader-1.4.1-android.apk",
              "browser_download_url":"https://e.invalid/a.apk",
              "size":1,
              "digest":"md5:5d41402abc4b2a76b9719d911017c592"}]}
            """.trimIndent(),
            "1.4.0",
        )!!

        assertNull(info.sha256)
        assertEquals(UpdateFailure.NO_CHECKSUM, downloadRequestFailure(info))
    }

    // ------------------------------------------------------------------
    // Transport: manifest first, API fallback, and the IO dispatcher
    // ------------------------------------------------------------------

    private val manifestForCheck: String
        get() = """
            {
              "schema": 1,
              "version": "1.5.0",
              "notes_url": "https://github.com/lingyi-ling/novel-downloader/releases/tag/v1.5.0",
              "platforms": {
                "android": {
                  "name": "novel-downloader-1.5.0-android.apk",
                  "sha256": "$apkSha",
                  "size": 62714163,
                  "url": "$apkUrl"
                }
              }
            }
        """.trimIndent()

    private val apiReleaseForCheck: String
        get() = """
            {"tag_name":"v1.4.1","assets":[{
              "name":"novel-downloader-1.4.1-android.apk",
              "browser_download_url":"https://e.invalid/a.apk",
              "size":1,
              "digest":"sha256:$digest"}]}
        """.trimIndent()

    @Test
    fun checkPrefersTheManifestOverTheApi() = runBlocking {
        val server = MockWebServer()
        server.start()
        try {
            server.enqueue(MockResponse().setBody(manifestForCheck))
            // Second response only guards against a hang if the API is hit by
            // mistake; requestCount below proves it never was.
            server.enqueue(MockResponse().setResponseCode(500))

            val info = UpdateChecker.check(
                "1.4.1",
                apiUrl = server.url("/releases/latest").toString(),
                manifestUrl = server.url("/latest.json").toString(),
            )

            assertEquals("1.5.0", info?.latest)
            assertTrue(info?.available == true)
            assertEquals(62_714_163L, info?.assetSize)
            assertEquals(apkSha, info?.sha256)
            // Exactly one request: the API must not be touched when the
            // manifest already answered.
            assertEquals(1, server.requestCount)
            val request = server.takeRequest()
            assertEquals("/latest.json", request.path)
            assertEquals("application/json", request.getHeader("Accept"))
            assertEquals("novel-downloader-android", request.getHeader("User-Agent"))
        } finally {
            server.shutdown()
        }
    }

    @Test
    fun manifestHttpFailureFallsBackToTheApi() = runBlocking {
        val server = MockWebServer()
        server.start()
        try {
            server.enqueue(MockResponse().setResponseCode(500))
            server.enqueue(MockResponse().setBody(apiReleaseForCheck))

            val info = UpdateChecker.check(
                "1.4.0",
                apiUrl = server.url("/releases/latest").toString(),
                manifestUrl = server.url("/latest.json").toString(),
            )

            // The API payload is older than the manifest would have said, which
            // proves the second response is the one that was used.
            assertEquals("1.4.1", info?.latest)
            assertTrue(info?.available == true)

            assertEquals(2, server.requestCount)
            assertEquals("/latest.json", server.takeRequest().path)
            val apiRequest = server.takeRequest()
            assertEquals("/releases/latest", apiRequest.path)
            assertEquals("application/vnd.github+json", apiRequest.getHeader("Accept"))
        } finally {
            server.shutdown()
        }
    }

    @Test
    fun unusableManifestFallsBackToTheApi() = runBlocking {
        val server = MockWebServer()
        server.start()
        try {
            // schema 2 is never guessed at, so the API gets its turn.
            server.enqueue(
                MockResponse().setBody(
                    """{"schema":2,"version":"9.9.9","platforms":{}}""",
                ),
            )
            server.enqueue(MockResponse().setBody(apiReleaseForCheck))

            val info = UpdateChecker.check(
                "1.4.0",
                apiUrl = server.url("/releases/latest").toString(),
                manifestUrl = server.url("/latest.json").toString(),
            )

            assertEquals("1.4.1", info?.latest)
            // The API asset size is 1, proving the manifest's 62714163 was not used.
            assertEquals(1L, info?.assetSize)
            assertEquals(2, server.requestCount)
        } finally {
            server.shutdown()
        }
    }

    @Test
    fun manifestWithoutAndroidEntryFallsBackToTheApi() = runBlocking {
        val server = MockWebServer()
        server.start()
        try {
            // Valid manifest that simply forgot the APK: the API gets asked.
            server.enqueue(MockResponse().setBody(manifest(platforms = windowsEntry)))
            server.enqueue(MockResponse().setBody(apiReleaseForCheck))

            val info = UpdateChecker.check(
                "1.4.0",
                apiUrl = server.url("/releases/latest").toString(),
                manifestUrl = server.url("/latest.json").toString(),
            )

            assertEquals("1.4.1", info?.latest)
            assertEquals(1L, info?.assetSize)
            assertEquals(2, server.requestCount)
        } finally {
            server.shutdown()
        }
    }

    @Test
    fun checkPerformsItsHttpCallOnTheIoDispatcher() = runBlocking {
        val server = MockWebServer()
        server.start()
        val callThread = AtomicReference("")
        val client = OkHttpClient.Builder()
            .eventListener(object : EventListener() {
                override fun callStart(call: Call) {
                    callThread.set(Thread.currentThread().name)
                }
            })
            .build()
        try {
            server.enqueue(MockResponse().setResponseCode(404))
            server.enqueue(MockResponse().setBody(apiReleaseForCheck))

            val info = UpdateChecker.check(
                "1.4.0",
                apiUrl = server.url("/latest").toString(),
                manifestUrl = server.url("/latest.json").toString(),
                client = client,
            )

            assertEquals("1.4.1", info?.latest)
            assertTrue(info?.available == true)
            assertEquals("/latest.json", server.takeRequest().path)
            val request = server.takeRequest()
            assertEquals("application/vnd.github+json", request.getHeader("Accept"))
            assertEquals("novel-downloader-android", request.getHeader("User-Agent"))
            assertTrue(
                "HTTP call must run on Dispatchers.IO, was on ${callThread.get()}",
                callThread.get().startsWith("DefaultDispatcher-worker"),
            )
        } finally {
            server.shutdown()
        }
    }

    @Test
    fun failedChecksReturnNullInsteadOfThrowing() = runBlocking {
        val server = MockWebServer()
        server.start()
        try {
            server.enqueue(MockResponse().setResponseCode(500))
            server.enqueue(MockResponse().setResponseCode(500))
            assertNull(
                UpdateChecker.check(
                    "1.4.0",
                    apiUrl = server.url("/latest").toString(),
                    manifestUrl = server.url("/latest.json").toString(),
                ),
            )
            assertEquals(2, server.requestCount)
        } finally {
            server.shutdown()
        }
    }
}
