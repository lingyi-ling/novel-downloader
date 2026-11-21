package com.lingyi.noveldl.data

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Locks the Android verifier to the publisher's Python implementation.
 *
 * The vectors in `src/test/resources/update-signature/` were produced by
 * `scripts/build_update_manifest.py` itself (`build_manifest` + `sign_document`)
 * from a throwaway key: only the public key, the signed manifest, its canonical
 * signing bytes and the `key_id` are checked in — the seed was discarded.
 * `manifest.canonical.json` is the exact byte string Python signed, so the
 * canonical-JSON tests fail the moment Kotlin and Python disagree about key
 * order, escaping, Unicode or number formatting.
 */
class ManifestSignatureTest {

    private val json = Json {
        ignoreUnknownKeys = true
        isLenient = true
    }

    private val signedText: String by lazy {
        String(resourceBytes("manifest.signed.json"), Charsets.UTF_8)
    }
    private val signed: JsonObject by lazy { json.parseToJsonElement(signedText).jsonObject }
    private val publishedCanonicalBytes: ByteArray by lazy { resourceBytes("manifest.canonical.json") }
    private val publicKeyHex: String by lazy {
        String(resourceBytes("public-key.hex"), Charsets.UTF_8).trim()
    }
    private val expectedKeyId: String by lazy {
        String(resourceBytes("key-id.txt"), Charsets.UTF_8).trim()
    }
    private val trusted = mapOf(expectedKeyId to publicKeyHex)

    private val apkSha = "7133f789a9a3b43d8aae5897fd41f62a5e91d45dba6f82d80fcc4e5a2cce0ac7"

    private fun resourceBytes(name: String): ByteArray {
        val stream = javaClass.classLoader!!.getResourceAsStream("update-signature/$name")
            ?: error("missing test resource update-signature/$name")
        return stream.use { it.readBytes() }
    }

    // ------------------------------------------------------------------
    // Canonical JSON: byte-for-byte parity with Python
    // ------------------------------------------------------------------

    @Test
    fun canonicalBytesMatchThePublisherByteForByte() {
        val actual = ManifestSignature.signingBytes(signed)

        // Text comparison first: a failure names the first differing position
        // instead of dumping two 1500-byte arrays.
        assertEquals(
            String(publishedCanonicalBytes, Charsets.UTF_8),
            String(actual, Charsets.UTF_8),
        )
        assertArrayEquals(publishedCanonicalBytes, actual)
    }

    @Test
    fun canonicalEncodingPinsPythonEscapesUnicodeAndNumbers() {
        val text = String(ManifestSignature.signingBytes(signed), Charsets.UTF_8)

        // `ensure_ascii=False`: non-ASCII is emitted as UTF-8, quotes,
        // backslashes and C0 controls are escaped Python's way.
        assertTrue(text, text.contains("\"emoji\":\"update \uD83D\uDE80 done\""))
        assertTrue(text, text.contains("\"channel\":\"稳定版 \\\"stable\\\" \\\\ path\\nsecond\\tline\\u0001\\b\\f"))
        // U+2028/U+2029 are only escaped by the JavaScript encoder.
        assertTrue(text, text.contains("\u2028 \u2029"))
        // Integers keep full precision; `-0` normalises to `0`.
        assertTrue(text, text.contains("\"big_int\":123456789012345678901234567890"))
        assertTrue(text, text.contains("\"neg_zero_int\":0"))
        // Floats use Python's repr: shortest digits, `.0`, `e+16`, `e-05`.
        assertTrue(text, text.contains("\"one_tenth\":0.1"))
        assertTrue(text, text.contains("\"tiny\":0.0001"))
        assertTrue(text, text.contains("\"scientific\":1e+16"))
        assertTrue(text, text.contains("\"small_scientific\":1e-05"))
        assertTrue(text, text.contains("\"neg_zero_float\":-0.0"))
        assertTrue(text, text.contains("\"simple_float\":1.5"))
        // Nested objects are sorted as well, arrays keep their order.
        assertTrue(text, text.contains("\"nested\":{\"a\":{\"deep\":[0.5]},\"b\":[1,2,{\"a\":null,\"z\":true}]}"))
        // Object keys sort by code point: upper case first, and the
        // non-ASCII platform key lands after every ASCII one.
        assertTrue(text.indexOf("\"Alpha\"") < text.indexOf("\"channel\""))
        assertTrue(
            text.indexOf("\"windows-x64-portable\"") < text.indexOf("\"中文-extra\""),
        )
        // No insignificant whitespace anywhere.
        assertFalse(text, text.contains("\": ") || text.contains(", "))
    }

    @Test
    fun keysSortByCodePointNotByUtf16CodeUnit() {
        // Python compares str by character, so U+FF21 < U+1F600 and the
        // full-width letter comes first. `String.compareTo` would order the
        // emoji's leading surrogate (0xD83D) before U+FF21 and get it wrong.
        val element = json.parseToJsonElement("{\"\uD83D\uDE00\":1,\"\uFF21\":2}")

        assertEquals("{\"\uFF21\":2,\"\uD83D\uDE00\":1}", CanonicalJson.encode(element))
    }

    @Test
    fun floatFormattingMatchesPythonRepr() {
        val cases = listOf(
            0.1 to "0.1",
            0.3 to "0.3",
            1.5 to "1.5",
            1000.0 to "1000.0",
            0.0001 to "0.0001",
            1e16 to "1e+16",
            1e15 to "1000000000000000.0",
            1e-5 to "1e-05",
            5e-324 to "5e-324",
            1e300 to "1e+300",
            2.2250738585072014e-308 to "2.2250738585072014e-308",
            1.7976931348623157e308 to "1.7976931348623157e+308",
            1.2345678901234567 to "1.2345678901234567",
            1234567890123456789.0 to "1.2345678901234568e+18",
            62714163.0 to "62714163.0",
            -0.0 to "-0.0",
        )

        for ((value, expected) in cases) {
            assertEquals("repr($value)", expected, CanonicalJson.pythonFloatRepr(value))
        }
        assertEquals("Infinity", CanonicalJson.pythonFloatRepr(Double.POSITIVE_INFINITY))
        assertEquals("-Infinity", CanonicalJson.pythonFloatRepr(Double.NEGATIVE_INFINITY))
        assertEquals("NaN", CanonicalJson.pythonFloatRepr(Double.NaN))
    }

    // ------------------------------------------------------------------
    // key_id and signature verification
    // ------------------------------------------------------------------

    @Test
    fun keyIdIsTheFirstSixteenHexOfThePublicKeyDigest() {
        assertEquals(expectedKeyId, ManifestSignature.keyId(publicKeyHex))
        // Hex parsing is case-insensitive, like Python's bytes.fromhex.
        assertEquals(expectedKeyId, ManifestSignature.keyId(publicKeyHex.uppercase()))
    }

    @Test
    fun thePublisherSignatureVerifies() {
        ManifestSignature.verify(signed, trusted)

        assertTrue(ManifestSignature.isTrusted(signed, trusted))
        // A trailing newline in the configured hex constant is tolerated.
        assertTrue(ManifestSignature.isTrusted(signed, mapOf(expectedKeyId to "$publicKeyHex\n")))
    }

    @Test
    fun anEmptyTableKeepsTheUnsignedDefault() {
        assertTrue("signing must stay opt-in", TRUSTED_UPDATE_KEYS.isEmpty())

        // No configured key: verification is a no-op, even for a document
        // without any signature fields.
        ManifestSignature.verify(signed, emptyMap())
        ManifestSignature.verify(JsonObject(emptyMap()), emptyMap())
        assertTrue(ManifestSignature.isTrusted(JsonObject(emptyMap()), emptyMap()))
    }

    @Test
    fun tamperingWithASignedFieldIsRejected() {
        val tamperedDigest = json.parseToJsonElement(
            signedText.replace(apkSha, "0".repeat(64)),
        ).jsonObject
        assertFalse(ManifestSignature.isTrusted(tamperedDigest, trusted))

        val tamperedSize = json.parseToJsonElement(
            signedText.replace("\"size\": 62714163", "\"size\": 62714164"),
        ).jsonObject
        assertFalse(ManifestSignature.isTrusted(tamperedSize, trusted))

        // Adding a field invalidates the signature too: every field is signed.
        val injected = JsonObject(signed + mapOf("injected" to JsonPrimitive(true)))
        assertFalse(ManifestSignature.isTrusted(injected, trusted))

        // Removing a platform is equally visible.
        val stripped = json.parseToJsonElement(
            signedText.replace("\"windows-x64-portable\"", "\"windows-x64-renamed\""),
        ).jsonObject
        assertFalse(ManifestSignature.isTrusted(stripped, trusted))

        val error = assertThrows(ManifestSignatureException::class.java) {
            ManifestSignature.verify(tamperedDigest, trusted)
        }
        assertTrue(error.message!!.contains("does not verify"))
    }

    @Test
    fun anUnknownKeyIdIsRejected() {
        val relabelled = JsonObject(
            signed + mapOf("key_id" to JsonPrimitive("0123456789abcdef")),
        )

        val error = assertThrows(ManifestSignatureException::class.java) {
            ManifestSignature.verify(relabelled, trusted)
        }
        assertTrue(error.message!!, error.message!!.contains("unknown key"))
        assertFalse(ManifestSignature.isTrusted(relabelled, trusted))
    }

    @Test
    fun aKnownKeyIdPointingAtAnotherPublicKeyIsRejected() {
        // The key id is only a selector: a valid signature relabelled to a
        // *different trusted* key fails, because the looked-up key differs.
        val wrongKey = mapOf(expectedKeyId to RFC8032_PUBLIC_1)

        assertFalse(ManifestSignature.isTrusted(signed, wrongKey))
    }

    @Test
    fun missingOrMalformedSignatureFieldsAreRejected() {
        val missingSignature = JsonObject(signed - "signature")
        val missingKeyId = JsonObject(signed - "key_id")
        val emptySignature = JsonObject(signed + mapOf("signature" to JsonPrimitive("")))
        val nullSignature = JsonObject(signed + mapOf("signature" to JsonPrimitive(null as String?)))
        val numericSignature = JsonObject(signed + mapOf("signature" to JsonPrimitive(42)))
        val badBase64 = JsonObject(
            signed + mapOf("signature" to JsonPrimitive("not base64!!")),
        )
        val truncated = JsonObject(
            signed + mapOf("signature" to JsonPrimitive("c2hvcnQ=")),
        )

        for (document in listOf(
            missingSignature,
            missingKeyId,
            emptySignature,
            nullSignature,
            numericSignature,
            badBase64,
            truncated,
        )) {
            assertFalse(document.toString(), ManifestSignature.isTrusted(document, trusted))
        }

        val unsigned = assertThrows(ManifestSignatureException::class.java) {
            ManifestSignature.verify(missingSignature, trusted)
        }
        assertTrue(unsigned.message!!.contains("not signed"))
    }

    @Test
    fun aMalformedTrustedKeyFailsClosedInsteadOfCrashing() {
        val badKeys = listOf(
            expectedKeyId to "zzzz",
            expectedKeyId to "aabb",
            expectedKeyId to "",
        )

        for ((id, key) in badKeys) {
            val rejected = try {
                ManifestSignature.verify(signed, mapOf(id to key))
                false
            } catch (e: ManifestSignatureException) {
                true
            }
            assertTrue("key $key must be rejected", rejected)
        }
    }

    // ------------------------------------------------------------------
    // Ed25519 itself (RFC 8032 §7.1 test vectors)
    // ------------------------------------------------------------------

    @Test
    fun rfc8032TestVectorsVerify() {
        val vectors = listOf(
            Triple(RFC8032_PUBLIC_1, ByteArray(0), RFC8032_SIGNATURE_1),
            Triple(RFC8032_PUBLIC_2, byteArrayOf(0x72), RFC8032_SIGNATURE_2),
            Triple(RFC8032_PUBLIC_3, hex("af82"), RFC8032_SIGNATURE_3),
        )

        for ((publicKey, message, signature) in vectors) {
            assertTrue(
                "RFC 8032 vector must verify",
                Ed25519.verify(hex(publicKey), message, hex(signature)),
            )
            // Any single flipped bit must break it.
            val broken = hex(signature)
            broken[0] = (broken[0].toInt() xor 0x01).toByte()
            assertFalse(Ed25519.verify(hex(publicKey), message, broken))
            // A different message must not verify either.
            assertFalse(
                Ed25519.verify(hex(publicKey), message + 0x00.toByte(), hex(signature)),
            )
        }
    }

    @Test
    fun ed25519RejectsNonCanonicalOrTruncatedInput() {
        val publicKey = hex(RFC8032_PUBLIC_1)
        val signature = hex(RFC8032_SIGNATURE_1)

        assertFalse(Ed25519.verify(ByteArray(0), ByteArray(0), signature))
        assertFalse(Ed25519.verify(publicKey, ByteArray(0), signature.copyOf(63)))
        assertFalse(Ed25519.verify(publicKey.copyOf(31), ByteArray(0), signature))
        assertFalse(Ed25519.verify(publicKey, ByteArray(0), signature.copyOf(65)))
        // A public key whose y coordinate is >= p is not a curve point.
        assertFalse(Ed25519.verify(ByteArray(32) { 0xff.toByte() }, ByteArray(0), signature))
    }

    private fun hex(text: String): ByteArray = requireNotNull(text.decodeHexOrNull())

    // Public key, message and signature only: the RFC's secret seeds are not
    // part of a verification vector and are deliberately not reproduced here.
    private companion object {
        const val RFC8032_PUBLIC_1 =
            "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
        const val RFC8032_SIGNATURE_1 =
            "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155" +
                "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"

        const val RFC8032_PUBLIC_2 =
            "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c"
        const val RFC8032_SIGNATURE_2 =
            "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da" +
                "085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"

        const val RFC8032_PUBLIC_3 =
            "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025"
        const val RFC8032_SIGNATURE_3 =
            "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac" +
                "18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"
    }
}
