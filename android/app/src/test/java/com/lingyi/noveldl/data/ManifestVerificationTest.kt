package com.lingyi.noveldl.data

import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * End-to-end behaviour of manifest signing in the Android updater: the same
 * vectors as [ManifestSignatureTest], but through [parseManifest] and
 * [UpdateChecker.check], including the "a pinned key means no API fallback"
 * rule the desktop and CLI clients already implement.
 */
class ManifestVerificationTest {

    private val json = Json {
        ignoreUnknownKeys = true
        isLenient = true
    }

    private val signedText: String by lazy {
        String(resourceBytes("manifest.signed.json"), Charsets.UTF_8)
    }
    private val publicKeyHex: String by lazy {
        String(resourceBytes("public-key.hex"), Charsets.UTF_8).trim()
    }
    private val keyId: String by lazy {
        String(resourceBytes("key-id.txt"), Charsets.UTF_8).trim()
    }
    private val trusted = mapOf(keyId to publicKeyHex)

    /** The same document with `signature`/`key_id` removed. */
    private val unsignedText: String by lazy {
        val root = json.parseToJsonElement(signedText).jsonObject
        JsonObject(root.filterKeys { it != "signature" && it != "key_id" }).toString()
    }

    private val apkSha = "7133f789a9a3b43d8aae5897fd41f62a5e91d45dba6f82d80fcc4e5a2cce0ac7"

    private val apiRelease = """
        {"tag_name":"v1.4.1","assets":[{
          "name":"novel-downloader-1.4.1-android.apk",
          "browser_download_url":"https://e.invalid/a.apk",
          "size":1,
          "digest":"sha256:${"a".repeat(64)}"}]}
    """.trimIndent()

    private fun resourceBytes(name: String): ByteArray {
        val stream = javaClass.classLoader!!.getResourceAsStream("update-signature/$name")
            ?: error("missing test resource update-signature/$name")
        return stream.use { it.readBytes() }
    }

    // ------------------------------------------------------------------
    // parseManifest
    // ------------------------------------------------------------------

    @Test
    fun theCompiledKeyTableIsEmptyByDefault() {
        assertTrue(
            "signing must stay opt-in until a key is compiled in",
            TRUSTED_UPDATE_KEYS.isEmpty(),
        )
        // With no key configured the published unsigned document is still
        // accepted, exactly as before signing existed.
        assertNotNull(parseManifest(unsignedText, "1.35.0"))
    }

    @Test
    fun aConfiguredKeyAcceptsTheSignedManifest() {
        val info = parseManifest(signedText, "1.35.0", listOf("arm64-v8a"), trusted)!!

        assertEquals("1.36.0", info.latest)
        assertTrue(info.available)
        assertEquals("novel-downloader-1.36.0-android.apk", info.assetName)
        assertEquals(apkSha, info.sha256)
        assertEquals(62_714_163L, info.assetSize)
        assertNull(downloadRequestFailure(info))
    }

    @Test
    fun aConfiguredKeyRejectsUnsignedAndTamperedManifests() {
        // Unsigned but otherwise valid: the API used to be the fallback here,
        // and the signature is what must now stop it.
        assertNull(parseManifest(unsignedText, "1.35.0", emptyList(), trusted))

        // Signature present but the signed payload changed.
        val tampered = signedText.replace(apkSha, "0".repeat(64))
        assertNull(parseManifest(tampered, "1.35.0", emptyList(), trusted))

        // Signed by a key that is not in the table.
        val unknownKey = mapOf("0123456789abcdef" to publicKeyHex)
        assertNull(parseManifest(signedText, "1.35.0", emptyList(), unknownKey))

        // And the same document without a pinned key is still parsed, which
        // proves the rejections above come from the signature check alone.
        assertNotNull(parseManifest(tampered, "1.35.0"))
    }

    // ------------------------------------------------------------------
    // UpdateChecker.check: the no-fallback rule
    // ------------------------------------------------------------------

    @Test
    fun aPinnedKeyAcceptsASignedManifestWithoutTouchingTheApi() = runBlocking {
        val server = MockWebServer()
        server.start()
        try {
            server.enqueue(MockResponse().setBody(signedText))
            // Only reached if the API fallback were (wrongly) taken.
            server.enqueue(MockResponse().setBody(apiRelease))

            val info = UpdateChecker.check(
                "1.35.0",
                apiUrl = server.url("/releases/latest").toString(),
                manifestUrl = server.url("/latest.json").toString(),
                trustedKeys = trusted,
            )

            assertEquals("1.36.0", info?.latest)
            assertEquals("novel-downloader-1.36.0-android.apk", info?.assetName)
            assertEquals(1, server.requestCount)
        } finally {
            server.shutdown()
        }
    }

    @Test
    fun aPinnedKeyRefusesAnUnsignedManifestInsteadOfAskingTheApi() = runBlocking {
        val server = MockWebServer()
        server.start()
        try {
            server.enqueue(MockResponse().setBody(unsignedText))
            server.enqueue(MockResponse().setBody(apiRelease))

            val info = UpdateChecker.check(
                "1.35.0",
                apiUrl = server.url("/releases/latest").toString(),
                manifestUrl = server.url("/latest.json").toString(),
                trustedKeys = trusted,
            )

            // Without pinning this same document yields 1.4.1 from the API.
            assertNull(info)
            assertEquals(1, server.requestCount)
        } finally {
            server.shutdown()
        }
    }

    @Test
    fun aPinnedKeyFailsClosedWhenTheManifestCannotBeFetched() = runBlocking {
        val server = MockWebServer()
        server.start()
        try {
            server.enqueue(MockResponse().setResponseCode(500))
            server.enqueue(MockResponse().setBody(apiRelease))

            assertNull(
                UpdateChecker.check(
                    "1.35.0",
                    apiUrl = server.url("/releases/latest").toString(),
                    manifestUrl = server.url("/latest.json").toString(),
                    trustedKeys = trusted,
                ),
            )
            // Deleting latest.json must not downgrade the client to the API.
            assertEquals(1, server.requestCount)
        } finally {
            server.shutdown()
        }
    }

    @Test
    fun withoutAConfiguredKeyTheApiFallbackStillWorks() = runBlocking {
        val server = MockWebServer()
        server.start()
        try {
            server.enqueue(MockResponse().setResponseCode(500))
            server.enqueue(MockResponse().setBody(apiRelease))

            val info = UpdateChecker.check(
                "1.4.0",
                apiUrl = server.url("/releases/latest").toString(),
                manifestUrl = server.url("/latest.json").toString(),
                trustedKeys = emptyMap(),
            )

            assertEquals("1.4.1", info?.latest)
            assertEquals(2, server.requestCount)
        } finally {
            server.shutdown()
        }
    }
}
