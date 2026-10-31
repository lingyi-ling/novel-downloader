package com.lingyi.noveldl.data

import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.emptyPreferences
import androidx.datastore.preferences.core.preferencesOf
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Test
import java.io.IOException

/**
 * Pure mapping rules behind [SettingsStore]: the DataStore flow must keep
 * emitting defaults when a read fails (a disk error at app start must not
 * leave MainActivity on a blank screen), and the settings-aware helpers must
 * keep the embedded engine as the API target while standalone mode is on.
 */
class SettingsStoreTest {

    @Test
    fun mapsPersistedPreferencesOntoApiSettings() = runBlocking {
        val flow: Flow<Preferences> = flowOf(
            preferencesOf(
                baseUrlKey to "http://10.1.1.5:8081/",
                apiKeyKey to "secret",
                downloadTreeUriKey to "content://com.android.externalstorage.documents/tree/primary:Books",
                standaloneKey to true,
            ),
        )

        val settings = flow.toApiSettings().first()

        assertEquals("http://10.1.1.5:8081/", settings.baseUrl)
        assertEquals("secret", settings.apiKey)
        assertEquals(
            "content://com.android.externalstorage.documents/tree/primary:Books",
            settings.downloadTreeUri,
        )
        assertEquals(true, settings.standalone)
        assertEquals(EmbeddedEngine.BASE_URL, settings.effectiveBaseUrl)
    }

    @Test
    fun emptyPreferencesMapOntoDefaults() = runBlocking {
        val settings = flowOf(emptyPreferences())
            .toApiSettings()
            .first()

        assertEquals(Api.DEFAULT_BASE_URL, settings.baseUrl)
        assertEquals("", settings.apiKey)
        assertEquals("", settings.downloadTreeUri)
        assertEquals(false, settings.standalone)
    }

    @Test
    fun missingKeysKeepDefaults() = runBlocking {
        val settings = flowOf(preferencesOf(baseUrlKey to "http://phone:9000/"))
            .toApiSettings()
            .first()

        assertEquals("http://phone:9000/", settings.baseUrl)
        assertEquals("", settings.apiKey)
        assertEquals(false, settings.standalone)
        assertEquals("http://phone:9000/", settings.effectiveBaseUrl)
    }

    @Test
    fun ioFailureOnReadFallsBackToDefaultsInsteadOfThrowing() = runBlocking {
        val failing: Flow<Preferences> = flow {
            throw IOException("disk error")
        }

        val settings = failing.toApiSettings().first()

        assertEquals(Api.DEFAULT_BASE_URL, settings.baseUrl)
        assertEquals("", settings.apiKey)
        assertEquals(false, settings.standalone)
    }

    // ------------------------------------------------------------------
    // Standalone mode must keep the embedded engine as the configure target;
    // saving or testing with the remote text field must not silently flip
    // the app out of engine mode, and toggling the switch must reconfigure.
    // ------------------------------------------------------------------

    @Test
    fun configureTargetUrlStaysOnTheEngineWhileStandalone() {
        val settings = ApiSettings(baseUrl = "http://remote:8081/", standalone = true)

        assertEquals(
            EmbeddedEngine.BASE_URL,
            settings.configureTargetUrl("http://edited:9000/"),
        )
    }

    @Test
    fun configureTargetUrlUsesTheEditedUrlWhenRemote() {
        val settings = ApiSettings(baseUrl = "http://remote:8081/", standalone = false)

        assertEquals(
            "http://edited:9000/",
            settings.configureTargetUrl("http://edited:9000/"),
        )
    }

    @Test
    fun standaloneToggleTargetsTheEngineWhenOnAndTheSavedUrlWhenOff() {
        val saved = ApiSettings(baseUrl = "http://remote:8081/")

        assertEquals(EmbeddedEngine.BASE_URL, standaloneTargetUrl(true, saved))
        assertEquals("http://remote:8081/", standaloneTargetUrl(false, saved))
    }
}
