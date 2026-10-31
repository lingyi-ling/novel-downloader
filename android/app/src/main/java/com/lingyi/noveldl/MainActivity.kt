package com.lingyi.noveldl

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.lingyi.noveldl.data.Api
import com.lingyi.noveldl.data.EmbeddedEngine
import com.lingyi.noveldl.data.SettingsStore
import com.lingyi.noveldl.notifications.TaskNotifier
import com.lingyi.noveldl.ui.NovelApp
import com.lingyi.noveldl.ui.NovelTheme
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {

    private val notificationPermissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        TaskNotifier.ensureChannel(this)
        requestNotificationPermissionOnce()
        val store = SettingsStore(this)
        lifecycleScope.launch {
            val settings = store.settings.first()
            if (settings.standalone) {
                EmbeddedEngine.ensureStarted(this@MainActivity)
            }
            Api.configure(settings.effectiveBaseUrl, settings.apiKey)
            setContent {
                NovelTheme {
                    NovelApp(store)
                }
            }
        }
    }

    /**
     * Android 13+ requires POST_NOTIFICATIONS for local task notifications. The
     * system dialog is shown once per install; afterwards the user controls the
     * permission from system settings.
     */
    private fun requestNotificationPermissionOnce() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) return
        if (ContextCompat.checkSelfPermission(
                this,
                Manifest.permission.POST_NOTIFICATIONS,
            ) == PackageManager.PERMISSION_GRANTED
        ) {
            return
        }
        val prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE)
        if (prefs.getBoolean(KEY_PERMISSION_REQUESTED, false)) return
        prefs.edit().putBoolean(KEY_PERMISSION_REQUESTED, true).apply()
        notificationPermissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
    }

    private companion object {
        const val PREFS_NAME = "novel_dl_ui"
        const val KEY_PERMISSION_REQUESTED = "notification_permission_requested"
    }
}
