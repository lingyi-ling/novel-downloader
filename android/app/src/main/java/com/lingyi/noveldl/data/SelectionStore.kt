package com.lingyi.noveldl.data

import android.content.Context

/**
 * Remembers the last search-site selection and music platform across launches.
 *
 * SharedPreferences is enough for two small values and avoids another DataStore
 * file; the encoding itself is delegated to the pure helpers in
 * [SiteSelection.kt] so it can be unit tested.
 */
class SelectionStore(context: Context) {

    private val prefs = context.applicationContext
        .getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    /** Previously selected search sites; empty means "all sites". */
    fun selectedSites(): List<String> =
        decodeSelectedSites(prefs.getString(KEY_SITES, null))

    fun saveSelectedSites(sites: Collection<String>) {
        prefs.edit().putString(KEY_SITES, encodeSelectedSites(sites)).apply()
    }

    /** Previously selected music platform key; empty when nothing was chosen yet. */
    fun selectedPlatform(): String = prefs.getString(KEY_PLATFORM, "").orEmpty()

    fun saveSelectedPlatform(platform: String) {
        prefs.edit().putString(KEY_PLATFORM, platform.trim()).apply()
    }

    companion object {
        const val PREFS_NAME = "novel_dl_selection"
        const val KEY_SITES = "search_sites"
        const val KEY_PLATFORM = "music_platform"
    }
}
