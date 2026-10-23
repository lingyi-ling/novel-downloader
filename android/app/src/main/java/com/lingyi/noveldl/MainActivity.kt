package com.lingyi.noveldl

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.lifecycle.lifecycleScope
import com.lingyi.noveldl.data.Api
import com.lingyi.noveldl.data.SettingsStore
import com.lingyi.noveldl.ui.NovelApp
import com.lingyi.noveldl.ui.NovelTheme
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val store = SettingsStore(this)
        lifecycleScope.launch {
            val settings = store.settings.first()
            Api.configure(settings.baseUrl, settings.apiKey)
            setContent {
                NovelTheme {
                    NovelApp(store)
                }
            }
        }
    }
}
