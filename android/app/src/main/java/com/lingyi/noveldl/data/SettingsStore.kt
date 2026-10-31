package com.lingyi.noveldl.data

import android.content.Context
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.emptyPreferences
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.catch
import kotlinx.coroutines.flow.map
import java.io.IOException

private val Context.settingsDataStore by preferencesDataStore(name = "novel_dl_settings")

internal val baseUrlKey = stringPreferencesKey("base_url")
internal val apiKeyKey = stringPreferencesKey("api_key")
internal val downloadTreeUriKey = stringPreferencesKey("download_tree_uri")
internal val standaloneKey = booleanPreferencesKey("standalone_engine")

data class ApiSettings(
    val baseUrl: String = Api.DEFAULT_BASE_URL,
    val apiKey: String = "",
    /**
     * Persisted SAF tree URI (`ACTION_OPEN_DOCUMENT_TREE`) of the custom
     * download directory, or blank for the system Downloads directory.
     */
    val downloadTreeUri: String = "",
    /**
     * When true the embedded Python engine serves the API on the device
     * itself and [effectiveBaseUrl] targets it, so the app works without a
     * computer running novel-api.
     */
    val standalone: Boolean = false,
) {
    /** The base URL the app should talk to given the current mode. */
    val effectiveBaseUrl: String
        get() = if (standalone) EmbeddedEngine.BASE_URL else baseUrl
}

/**
 * The base URL [Api.configure] should receive when the user confirms
 * [editedBaseUrl] (save or test): in standalone mode the embedded engine must
 * stay the active target no matter what the text field says.
 */
fun ApiSettings.configureTargetUrl(editedBaseUrl: String): String =
    if (standalone) EmbeddedEngine.BASE_URL else editedBaseUrl

/**
 * The base URL to configure after the standalone switch moves to [enabled]:
 * the embedded engine when turning it on, otherwise the persisted remote URL.
 */
fun standaloneTargetUrl(enabled: Boolean, settings: ApiSettings): String =
    if (enabled) EmbeddedEngine.BASE_URL else settings.baseUrl

/**
 * Maps the persisted preferences onto [ApiSettings]. A read failure (disk
 * error, corrupted file — both surface as [IOException]) falls back to
 * defaults instead of killing the flow: MainActivity reads the first value
 * to build the UI, and an exception there would leave a blank screen.
 */
internal fun Flow<Preferences>.toApiSettings(): Flow<ApiSettings> = catch { e ->
    if (e is IOException) emit(emptyPreferences()) else throw e
}.map { prefs ->
    ApiSettings(
        baseUrl = prefs[baseUrlKey] ?: Api.DEFAULT_BASE_URL,
        apiKey = prefs[apiKeyKey].orEmpty(),
        downloadTreeUri = prefs[downloadTreeUriKey].orEmpty(),
        standalone = prefs[standaloneKey] ?: false,
    )
}

class SettingsStore(private val context: Context) {

    val settings: Flow<ApiSettings> = context.settingsDataStore.data.toApiSettings()

    suspend fun save(baseUrl: String, apiKey: String) {
        context.settingsDataStore.edit { prefs ->
            prefs[baseUrlKey] = baseUrl.trim()
            prefs[apiKeyKey] = apiKey.trim()
        }
    }

    /** Toggles the embedded-engine mode (see [ApiSettings.standalone]). */
    suspend fun saveStandalone(enabled: Boolean) {
        context.settingsDataStore.edit { prefs ->
            prefs[standaloneKey] = enabled
        }
    }

    /**
     * Stores or clears the custom download directory. A null/blank [uri]
     * removes the key so the app falls back to the system Downloads directory.
     */
    suspend fun saveDownloadDirectory(uri: String?) {
        context.settingsDataStore.edit { prefs ->
            val trimmed = uri?.trim().orEmpty()
            if (trimmed.isEmpty()) {
                prefs.remove(downloadTreeUriKey)
            } else {
                prefs[downloadTreeUriKey] = trimmed
            }
        }
    }
}
