package com.lingyi.noveldl.data

import android.content.Context
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.map

private val Context.settingsDataStore by preferencesDataStore(name = "novel_dl_settings")

data class ApiSettings(
    val baseUrl: String = Api.DEFAULT_BASE_URL,
    val apiKey: String = "",
)

class SettingsStore(private val context: Context) {
    private val baseUrlKey = stringPreferencesKey("base_url")
    private val apiKeyKey = stringPreferencesKey("api_key")

    val settings: Flow<ApiSettings> = context.settingsDataStore.data.map { prefs ->
        ApiSettings(
            baseUrl = prefs[baseUrlKey] ?: Api.DEFAULT_BASE_URL,
            apiKey = prefs[apiKeyKey].orEmpty(),
        )
    }

    suspend fun save(baseUrl: String, apiKey: String) {
        context.settingsDataStore.edit { prefs ->
            prefs[baseUrlKey] = baseUrl.trim()
            prefs[apiKeyKey] = apiKey.trim()
        }
    }
}
