package com.lingyi.noveldl.ui.tasks

import com.lingyi.noveldl.data.LoginField

/**
 * Pure rules behind the login-credential dialog, kept out of the composable
 * so they can be exercised from plain JVM tests (see [LoginFieldsTest]).
 *
 * The dialog renders server-supplied `prefill`/`default` values and posts
 * whatever the user leaves in the editor back to the server. Both directions
 * are untrusted: a broken or hostile site plugin can hand back a
 * multi-megabyte "cookie" string, and a single-line `OutlinedTextField`
 * asked to lay out such a value re-measures it on every keystroke — enough
 * to stall the UI thread. Every buffer is therefore clamped to
 * [LOGIN_FIELD_MAX_LENGTH] on the way in, and submitted values are trimmed
 * so an accidental newline or trailing space from a copy-paste cannot poison
 * the credential.
 */

/**
 * Hard cap for one field's editor buffer: generous enough for any real
 * cookie or credential (browsers cap cookie values at 4 KB) and small enough
 * that the field's layout stays cheap.
 */
const val LOGIN_FIELD_MAX_LENGTH = 4096

/**
 * The value the editor starts with for [field]: the server's prefill when it
 * carries text, else the field's `default` — clamped either way, because
 * both come from the server.
 */
fun initialLoginFieldValue(field: LoginField, prefill: Map<String, String>): String =
    clampLoginFieldValue(prefill[field.name]?.takeIf { it.isNotBlank() } ?: field.default)

/** Clamps editor input (typing or paste) to [LOGIN_FIELD_MAX_LENGTH]. */
fun clampLoginFieldValue(value: String): String =
    if (value.length <= LOGIN_FIELD_MAX_LENGTH) value else value.take(LOGIN_FIELD_MAX_LENGTH)

/**
 * Whether the submit button may react to taps: every *required* field needs
 * a non-blank answer. Optional fields never gate the submission.
 */
fun loginSubmitReady(fields: List<LoginField>, values: Map<String, String>): Boolean =
    fields.filter { it.required }.all { !values[it.name].isNullOrBlank() }

/**
 * The `values` map posted with the submit: one trimmed entry per announced
 * field. Fields the server did not announce are dropped, so unknown keys
 * cannot ride along unvalidated, and a missing buffer reads as "".
 */
fun loginSubmitValues(fields: List<LoginField>, values: Map<String, String>): Map<String, String> =
    fields.associate { field -> field.name to (values[field.name] ?: "").trim() }
