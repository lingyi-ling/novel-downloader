package com.lingyi.noveldl.data

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import java.math.BigDecimal
import java.math.BigInteger
import java.math.MathContext
import java.math.RoundingMode
import java.security.MessageDigest
import java.util.Base64

/**
 * Trusted Ed25519 update-signing keys: `key_id` -> raw public key as hex.
 *
 * This mirrors `TRUSTED_UPDATE_KEYS` in
 * `src/novel_downloader/apps/common/updater.py` and is **empty by default**, so
 * manifests are accepted unsigned exactly as before.  Once a key is added
 * here, an unsigned manifest, an unknown `key_id`, a bad signature or a
 * manifest that cannot be fetched is rejected and the GitHub API is *not*
 * consulted as a fallback — otherwise deleting `latest.json` would bypass the
 * signature check.
 *
 * The table is compiled into the APK on purpose: a running client cannot be
 * talked into trusting another key.  Enabling it requires shipping a new build
 * (see `docs/guide/update-signing.md`).
 */
val TRUSTED_UPDATE_KEYS: Map<String, String> = emptyMap()

/** A manifest that is unsigned, signed by an unknown key, or badly signed. */
internal class ManifestSignatureException(message: String) : Exception(message)

/**
 * Ed25519 verification of the updater manifest (`latest.json`).
 *
 * The signature covers the *parsed* document re-serialised the way the
 * publisher does it:
 *
 * ```python
 * json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
 * ```
 *
 * where `body` is the manifest without `signature`/`key_id`.  The bytes on
 * disk are not what is signed (the file is canonical JSON plus a trailing
 * newline, and whitespace never participates), so [CanonicalJson] reproduces
 * Python's encoder exactly: recursive key sorting, no insignificant
 * whitespace, UTF-8 output for non-ASCII, Python's escape set, and Python's
 * number formatting.
 *
 * The public key is looked up by the untrusted `key_id` selector, exactly as
 * the Python verifier does; the signature itself is what proves the document.
 */
object ManifestSignature {

    /** Length of `key_id`: the first 16 hex chars of `sha256(public key)`. */
    internal const val KEY_ID_LENGTH: Int = 16

    /**
     * Canonical bytes a signature covers: [root] without `signature` and
     * `key_id`, serialised like the publisher's `canonical_bytes`.
     */
    fun signingBytes(root: JsonObject): ByteArray = CanonicalJson.signingBytes(root)

    /** `sha256(raw public key).hex()[:16]`, the manifest `key_id`. */
    fun keyId(publicKeyHex: String): String {
        val raw = publicKeyHex.decodeHexOrNull()
            ?: throw IllegalArgumentException("not a hex public key")
        val digest = MessageDigest.getInstance("SHA-256").digest(raw)
        return digest.toHex().take(KEY_ID_LENGTH)
    }

    /**
     * Whether [root] carries a signature [trustedKeys] accepts.
     *
     * Returns `true` when no key is configured (the unsigned default),
     * `false` for every rejection.  Use [verify] when the reason matters.
     *
     * Anything unexpected is a rejection, never a crash: the manifest is
     * remote, attacker-controlled input and a 24h update check must survive a
     * pathological document (absurd nesting depth included).
     */
    fun isTrusted(
        root: JsonObject,
        trustedKeys: Map<String, String> = TRUSTED_UPDATE_KEYS,
    ): Boolean = runCatching { verify(root, trustedKeys) }.isSuccess

    /**
     * Check the manifest signature, throwing [ManifestSignatureException] with
     * a specific reason on any failure.  Does nothing when [trustedKeys] is
     * empty, matching the publisher's "signing is opt-in" contract.
     */
    fun verify(
        root: JsonObject,
        trustedKeys: Map<String, String> = TRUSTED_UPDATE_KEYS,
    ) {
        if (trustedKeys.isEmpty()) return

        val keyId = root.stringValue("key_id")
        val encoded = root.stringValue("signature")
        if (keyId == null || encoded == null) {
            throw ManifestSignatureException(
                "manifest is not signed but a trusted key is configured",
            )
        }
        val publicHex = trustedKeys[keyId]
            ?: throw ManifestSignatureException("manifest is signed with an unknown key: $keyId")

        val publicKey = publicHex.decodeHexOrNull()
            ?: throw ManifestSignatureException("trusted key $keyId is not a hex public key")
        if (publicKey.size != Ed25519.PUBLIC_KEY_BYTES) {
            throw ManifestSignatureException(
                "trusted key $keyId is ${publicKey.size} bytes, expected ${Ed25519.PUBLIC_KEY_BYTES}",
            )
        }
        // Python's base64.b64decode(..., validate=True): the standard alphabet,
        // no whitespace, no URL-safe substitutes, correct padding.  Java's
        // decoder is laxer about missing padding, so that case is refused
        // explicitly to keep both clients in step.
        if (encoded.length % 4 != 0) {
            throw ManifestSignatureException("signature is not padded base64")
        }
        val signature = try {
            Base64.getDecoder().decode(encoded)
        } catch (e: IllegalArgumentException) {
            throw ManifestSignatureException("signature is not valid base64: ${e.message}")
        }
        if (!Ed25519.verify(publicKey, signingBytes(root), signature)) {
            throw ManifestSignatureException("manifest signature does not verify")
        }
    }
}

/** Contents of a JSON string member, or `null` for anything else. */
internal fun JsonObject.stringValue(key: String): String? {
    val primitive = this[key] as? JsonPrimitive ?: return null
    return if (primitive.isString) primitive.content else null
}

/** Case-insensitive hex decoding, or `null` when the text is not hex. */
internal fun String.decodeHexOrNull(): ByteArray? {
    val text = trim()
    if (text.length % 2 != 0) return null
    val bytes = ByteArray(text.length / 2)
    for (index in bytes.indices) {
        val high = hexDigit(text[index * 2])
        val low = hexDigit(text[index * 2 + 1])
        if (high < 0 || low < 0) return null
        bytes[index] = ((high shl 4) or low).toByte()
    }
    return bytes
}

private fun hexDigit(char: Char): Int = when (char) {
    in '0'..'9' -> char - '0'
    in 'a'..'f' -> char - 'a' + 10
    in 'A'..'F' -> char - 'A' + 10
    else -> -1
}

internal fun ByteArray.toHex(): String {
    val digits = "0123456789abcdef"
    val out = StringBuilder(size * 2)
    for (byte in this) {
        val value = byte.toInt() and 0xff
        out.append(digits[value ushr 4]).append(digits[value and 0x0f])
    }
    return out.toString()
}

/**
 * Python-compatible canonical JSON.
 *
 * `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`
 * is a byte-exact contract between the publisher and every client, so the
 * details matter:
 *
 * * object keys sort by **Unicode code point** (not UTF-16 code unit, which
 *   only differs above U+FFFF);
 * * strings escape exactly `"`, `\`, `\b`, `\f`, `\n`, `\r`, `\t` and the
 *   remaining C0 controls as lowercase `\u00xx`; everything else, U+2028 and
 *   U+2029 included, is emitted as UTF-8;
 * * integers keep full precision (`-0` becomes `0`), floats use Python's
 *   `repr` (shortest round-trip digits, `1e+16` / `1e-05` exponents, a `.0`
 *   suffix for integral values).
 *
 * JSON that Python could not have decoded (an unquoted literal, a malformed
 * number) raises [ManifestSignatureException] — its signature could never
 * verify anyway.
 */
internal object CanonicalJson {

    private val SIGNATURE_FIELDS = setOf("signature", "key_id")

    private val INTEGER = Regex("-?(?:0|[1-9][0-9]*)")

    /** Canonical bytes of [root] without the two signature members. */
    fun signingBytes(root: JsonObject): ByteArray {
        val body = JsonObject(root.filterKeys { it !in SIGNATURE_FIELDS })
        return encode(body).toByteArray(Charsets.UTF_8)
    }

    fun encode(element: JsonElement): String = buildString { write(element, this) }

    private fun write(element: JsonElement, out: StringBuilder) {
        when (element) {
            is JsonNull -> out.append("null")
            is JsonObject -> {
                out.append('{')
                var first = true
                for (key in element.keys.sortedWith(CODE_POINT_ORDER)) {
                    if (!first) out.append(',')
                    first = false
                    appendString(key, out)
                    out.append(':')
                    write(element.getValue(key), out)
                }
                out.append('}')
            }
            is JsonArray -> {
                out.append('[')
                element.forEachIndexed { index, item ->
                    if (index > 0) out.append(',')
                    write(item, out)
                }
                out.append(']')
            }
            is JsonPrimitive -> {
                if (element.isString) {
                    appendString(element.content, out)
                } else {
                    appendLiteral(element.content, out)
                }
            }
        }
    }

    private fun appendString(value: String, out: StringBuilder) {
        out.append('"')
        for (char in value) {
            when {
                char == '"' -> out.append("\\\"")
                char == '\\' -> out.append("\\\\")
                char == '\n' -> out.append("\\n")
                char == '\r' -> out.append("\\r")
                char == '\t' -> out.append("\\t")
                char == '\b' -> out.append("\\b")
                char == '\u000C' -> out.append("\\f")
                // `ensure_ascii=False` still escapes the remaining C0 controls
                // (lowercase hex), and nothing else.
                char < ' ' -> {
                    out.append("\\u")
                    val code = char.code
                    out.append(HEX[code shr 12 and 0x0f])
                    out.append(HEX[code shr 8 and 0x0f])
                    out.append(HEX[code shr 4 and 0x0f])
                    out.append(HEX[code and 0x0f])
                }
                else -> out.append(char)
            }
        }
        out.append('"')
    }

    private fun appendLiteral(content: String, out: StringBuilder) {
        when (content) {
            "true", "false" -> out.append(content)
            else -> {
                if (INTEGER.matches(content)) {
                    // Python parses an integer token as an arbitrary-precision
                    // int; re-serialising normalises `-0` to `0`.
                    out.append(BigInteger(content).toString())
                } else {
                    val value = content.toDoubleOrNull()
                        ?: throw ManifestSignatureException(
                            "manifest contains a non-JSON literal: $content",
                        )
                    out.append(pythonFloatRepr(value))
                }
            }
        }
    }

    /**
     * `repr()` of a Python float, i.e. the shortest decimal that round-trips,
     * in fixed notation for `-4 < decpt <= 16` and `d.dddde±XX` outside it.
     */
    fun pythonFloatRepr(value: Double): String {
        if (value.isNaN()) return "NaN"
        if (value == Double.POSITIVE_INFINITY) return "Infinity"
        if (value == Double.NEGATIVE_INFINITY) return "-Infinity"
        // Compare the sign bit so -0.0 keeps its minus.
        val negative = (value.toRawBits() ushr 63) != 0L
        val sign = if (negative) "-" else ""
        val magnitude = Math.abs(value)
        if (magnitude == 0.0) return sign + "0.0"
        val (digits, exponent) = shortestDigits(magnitude)
        return sign + formatShortest(digits, exponent)
    }

    /**
     * Shortest decimal digits of [value] (non-zero, finite) and the position
     * of the decimal point: `value = 0.<digits> * 10^decpt`.
     *
     * Rounding the *exact* binary value to `p` significant digits and keeping
     * the first `p` that round-trips yields the same shortest representation
     * Python's `repr` prints (ties resolve half-to-even, as both do).
     */
    private fun shortestDigits(value: Double): Pair<String, Int> {
        val exact = BigDecimal(value)
        var chosen = exact
        for (precision in 1..17) {
            val rounded = exact.round(MathContext(precision, RoundingMode.HALF_EVEN))
            if (rounded.toDouble() == value) {
                chosen = rounded
                break
            }
        }
        val stripped = chosen.stripTrailingZeros()
        val digits = stripped.unscaledValue().abs().toString()
        return digits to digits.length - stripped.scale()
    }

    private fun formatShortest(digits: String, decpt: Int): String {
        if (decpt in -3..16) {
            return when {
                decpt <= 0 -> "0." + "0".repeat(-decpt) + digits
                decpt >= digits.length ->
                    digits + "0".repeat(decpt - digits.length) + ".0"
                else -> digits.substring(0, decpt) + "." + digits.substring(decpt)
            }
        }
        val exponent = decpt - 1
        val mantissa = if (digits.length == 1) {
            digits
        } else {
            digits.substring(0, 1) + "." + digits.substring(1)
        }
        val exponentSign = if (exponent < 0) "-" else "+"
        val magnitude = Math.abs(exponent).toString().padStart(2, '0')
        return "${mantissa}e$exponentSign$magnitude"
    }

    private const val HEX = "0123456789abcdef"

    /**
     * Python sorts object keys by code point.  `String.compareTo` compares
     * UTF-16 code units, which orders a supplementary character before
     * U+E000..U+FFFF instead of after it, so it is not usable here.
     */
    private val CODE_POINT_ORDER = Comparator<String> { left, right ->
        var i = 0
        var j = 0
        while (i < left.length && j < right.length) {
            val a = left.codePointAt(i)
            val b = right.codePointAt(j)
            if (a != b) return@Comparator a.compareTo(b)
            i += Character.charCount(a)
            j += Character.charCount(b)
        }
        (left.length - i).compareTo(right.length - j)
    }
}
