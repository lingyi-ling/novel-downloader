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
    // Transport: the HTTP call must leave the main thread
    // ------------------------------------------------------------------

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
            server.enqueue(
                MockResponse().setBody(
                    """
                    {"tag_name":"v1.4.1","assets":[{
                      "name":"novel-downloader-1.4.1-android.apk",
                      "browser_download_url":"https://e.invalid/a.apk",
                      "size":1,
                      "digest":"sha256:$digest"}]}
                    """.trimIndent(),
                ),
            )

            val info = UpdateChecker.check(
                "1.4.0",
                apiUrl = server.url("/latest").toString(),
                client = client,
            )

            assertEquals("1.4.1", info?.latest)
            assertTrue(info?.available == true)
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
            assertNull(
                UpdateChecker.check("1.4.0", apiUrl = server.url("/latest").toString()),
            )
        } finally {
            server.shutdown()
        }
    }
}
