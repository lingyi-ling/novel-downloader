package com.lingyi.noveldl.data

import android.content.Context
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map

private val Context.settingsDataStore by preferencesDataStore(name = "novel_dl_settings")

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

class SettingsStore(private val context: Context) {
    private val baseUrlKey = stringPreferencesKey("base_url")
    private val apiKeyKey = stringPreferencesKey("api_key")
    private val downloadTreeUriKey = stringPreferencesKey("download_tree_uri")
    private val standaloneKey = booleanPreferencesKey("standalone_engine")

    val settings: Flow<ApiSettings> = context.settingsDataStore.data.map { prefs ->
        ApiSettings(
            baseUrl = prefs[baseUrlKey] ?: Api.DEFAULT_BASE_URL,
            apiKey = prefs[apiKeyKey].orEmpty(),
            downloadTreeUri = prefs[downloadTreeUriKey].orEmpty(),
            standalone = prefs[standaloneKey] ?: false,
        )
    }

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
