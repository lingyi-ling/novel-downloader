package com.lingyi.noveldl.data

import java.math.BigInteger
import java.security.MessageDigest

/**
 * Minimal RFC 8032 **Ed25519 verification**, implemented without a provider.
 *
 * `Signature.getInstance("Ed25519")` only exists from Android API 33, while this
 * app supports API 26; a crypto provider would add megabytes to the APK for one
 * once-a-day check.  The verification equation is therefore evaluated here on
 * top of [BigInteger] and `MessageDigest("SHA-512")`, both present on every
 * supported API level and on the JVM used by the unit tests.
 *
 * Only verification exists: a client never holds the private seed.  The curve
 * arithmetic is not constant time, which is fine for verifying public data
 * (signatures and manifests) where there is no secret to leak.
 */
internal object Ed25519 {

    /** `2^255 - 19`, the field prime. */
    private val P: BigInteger = BigInteger.ONE.shiftLeft(255).subtract(BigInteger.valueOf(19))

    /** The group order `L = 2^252 + 27742317777372353535851937790883648493`. */
    private val L: BigInteger = BigInteger.ONE.shiftLeft(252)
        .add(BigInteger("27742317777372353535851937790883648493"))

    /** Curve constant `d = -121665 / 121666`. */
    private val D: BigInteger = BigInteger.valueOf(-121665).mod(P)
        .multiply(BigInteger.valueOf(121666).modInverse(P)).mod(P)

    /** `2 * d`, used by the extended-coordinate addition formula. */
    private val TWO_D: BigInteger = D.shiftLeft(1).mod(P)

    /** A square root of `-1` in the field: `2^((p-1)/4)`. */
    private val SQRT_M1: BigInteger =
        BigInteger.valueOf(2).modPow(P.subtract(BigInteger.ONE).shiftRight(2), P)

    private val ONE: BigInteger = BigInteger.ONE
    // `BigInteger.valueOf(2)` rather than `BigInteger.TWO`: the constant only
    // exists from Java 9 on, and minSdk is 26.
    private val TWO: BigInteger = BigInteger.valueOf(2)

    /** One point in extended twisted-Edwards coordinates `(X : Y : Z : T)`. */
    private data class Point(
        val x: BigInteger,
        val y: BigInteger,
        val z: BigInteger,
        val t: BigInteger,
    )

    private val IDENTITY = Point(BigInteger.ZERO, ONE, ONE, BigInteger.ZERO)

    /** The RFC 8032 base point: `y = 4/5` with an even `x`. */
    private val BASE: Point = run {
        val y = BigInteger.valueOf(4).multiply(BigInteger.valueOf(5).modInverse(P)).mod(P)
        val x = requireNotNull(recoverX(y, sign = 0)) { "base point does not decode" }
        Point(x, y, ONE, x.multiply(y).mod(P))
    }

    /**
     * Whether [signature] is a valid Ed25519 signature of [message] by the
     * holder of [publicKey].
     *
     * Mirrors the RFC 8032 §5.1.7 check exactly as the publisher's Python
     * verifier (`Crypto.Signature.eddsa`, `rfc8032`) performs it:
     * `[S]B == R + [H(R || A || M) mod L]A`, with `S < L` enforced.
     *
     * Any malformed input (wrong lengths, a point that is not on the curve, a
     * non-canonical encoding) returns `false`; nothing throws.
     */
    fun verify(publicKey: ByteArray, message: ByteArray, signature: ByteArray): Boolean {
        if (publicKey.size != PUBLIC_KEY_BYTES || signature.size != SIGNATURE_BYTES) return false
        val publicPoint = decodePoint(publicKey) ?: return false
        val encodedR = signature.copyOfRange(0, PUBLIC_KEY_BYTES)
        val r = decodePoint(encodedR) ?: return false
        val s = littleEndianToInteger(signature, PUBLIC_KEY_BYTES, SIGNATURE_BYTES)
        // RFC 8032 requires a canonical S; a larger one is a malleable
        // duplicate and must not be accepted.
        if (s >= L) return false

        val digest = MessageDigest.getInstance("SHA-512")
        digest.update(encodedR)
        digest.update(publicKey)
        digest.update(message)
        // RFC 8032 §5.1.7: the 64-octet digest is read as a *little-endian*
        // integer before the reduction modulo L.
        val hash = digest.digest()
        val k = littleEndianToInteger(hash, 0, hash.size).mod(L)

        val left = scalarMultiply(BASE, s)
        val right = add(r, scalarMultiply(publicPoint, k))
        return samePoint(left, right)
    }

    // ------------------------------------------------------------------
    // Point arithmetic (Hisil–Wong–Carter–Dawson extended coordinates)
    // ------------------------------------------------------------------

    /** Unified addition for `a = -1`; valid for `p == q` as well. */
    private fun add(p: Point, q: Point): Point {
        val a = p.y.subtract(p.x).multiply(q.y.subtract(q.x)).mod(P)
        val b = p.y.add(p.x).multiply(q.y.add(q.x)).mod(P)
        val c = p.t.multiply(TWO_D).multiply(q.t).mod(P)
        val d = p.z.multiply(TWO).multiply(q.z).mod(P)
        val e = b.subtract(a).mod(P)
        val f = d.subtract(c).mod(P)
        val g = d.add(c).mod(P)
        val h = b.add(a).mod(P)
        return Point(
            x = e.multiply(f).mod(P),
            y = g.multiply(h).mod(P),
            z = f.multiply(g).mod(P),
            t = e.multiply(h).mod(P),
        )
    }

    /** Doubling for `a = -1`. */
    private fun double(p: Point): Point {
        val a = p.x.multiply(p.x).mod(P)
        val b = p.y.multiply(p.y).mod(P)
        val c = p.z.multiply(p.z).multiply(TWO).mod(P)
        val d = P.subtract(a).mod(P)
        val e = p.x.add(p.y).pow(2).subtract(a).subtract(b).mod(P)
        val g = d.add(b).mod(P)
        val f = g.subtract(c).mod(P)
        val h = d.subtract(b).mod(P)
        return Point(
            x = e.multiply(f).mod(P),
            y = g.multiply(h).mod(P),
            z = f.multiply(g).mod(P),
            t = e.multiply(h).mod(P),
        )
    }

    private fun scalarMultiply(point: Point, scalar: BigInteger): Point {
        var result = IDENTITY
        var addend = point
        var remaining = scalar
        while (remaining.signum() > 0) {
            if (remaining.testBit(0)) result = add(result, addend)
            addend = double(addend)
            remaining = remaining.shiftRight(1)
        }
        return result
    }

    /** Projective equality: `X1/Z1 == X2/Z2` and `Y1/Z1 == Y2/Z2`. */
    private fun samePoint(a: Point, b: Point): Boolean =
        a.x.multiply(b.z).mod(P) == b.x.multiply(a.z).mod(P) &&
            a.y.multiply(b.z).mod(P) == b.y.multiply(a.z).mod(P)

    // ------------------------------------------------------------------
    // Point compression / decompression (RFC 8032 §5.1.2–5.1.3)
    // ------------------------------------------------------------------

    private fun decodePoint(encoded: ByteArray): Point? {
        if (encoded.size != PUBLIC_KEY_BYTES) return null
        val copy = encoded.copyOf()
        val sign = (copy[PUBLIC_KEY_BYTES - 1].toInt() ushr 7) and 1
        copy[PUBLIC_KEY_BYTES - 1] = (copy[PUBLIC_KEY_BYTES - 1].toInt() and 0x7f).toByte()
        val y = littleEndianToInteger(copy, 0, copy.size)
        // `y >= p` is a non-canonical encoding of a smaller field element.
        if (y >= P) return null
        val x = recoverX(y, sign) ?: return null
        return Point(x, y, ONE, x.multiply(y).mod(P))
    }

    /**
     * Recover `x` from `y` and the sign bit, or `null` when `y` is not on the
     * curve. `x^2 = (y^2 - 1) / (d*y^2 + 1)`; a candidate is computed with the
     * `(p+3)/8` exponent and corrected by `sqrt(-1)` when it is off by `-1`.
     */
    private fun recoverX(y: BigInteger, sign: Int): BigInteger? {
        val yy = y.multiply(y).mod(P)
        val u = yy.subtract(ONE).mod(P)
        val v = D.multiply(yy).add(ONE).mod(P)

        val v3 = v.modPow(BigInteger.valueOf(3), P)
        val v7 = v.modPow(BigInteger.valueOf(7), P)
        val exponent = P.subtract(BigInteger.valueOf(5)).shiftRight(3)
        var x = u.multiply(v3).multiply(u.multiply(v7).modPow(exponent, P)).mod(P)

        val vxx = v.multiply(x.multiply(x)).mod(P)
        when {
            vxx == u -> Unit
            vxx == P.subtract(u).mod(P) -> x = x.multiply(SQRT_M1).mod(P)
            else -> return null
        }
        // `x = 0` cannot carry the "negative" sign bit.
        if (x.signum() == 0 && sign == 1) return null
        if (x.testBit(0) != (sign == 1)) x = P.subtract(x).mod(P)
        return x
    }

    /** Little-endian unsigned integer from `bytes[from until to]`. */
    private fun littleEndianToInteger(bytes: ByteArray, from: Int, to: Int): BigInteger {
        val bigEndian = ByteArray(to - from)
        for (index in from until to) {
            bigEndian[to - 1 - index] = bytes[index]
        }
        return BigInteger(1, bigEndian)
    }

    const val PUBLIC_KEY_BYTES: Int = 32
    const val SIGNATURE_BYTES: Int = 64
}
