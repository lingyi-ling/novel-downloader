package com.lingyi.noveldl.ui.settings

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Button
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import com.lingyi.noveldl.R
import com.lingyi.noveldl.data.Api
import com.lingyi.noveldl.data.ApiSettings
import com.lingyi.noveldl.data.SettingsStore
import kotlinx.coroutines.launch

@Composable
fun SettingsScreenView(store: SettingsStore, onMessage: (String) -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val settings by store.settings.collectAsState(initial = ApiSettings())
    var baseUrl by remember(settings) { mutableStateOf(settings.baseUrl) }
    var apiKey by remember(settings) { mutableStateOf(settings.apiKey) }
    var testing by remember { mutableStateOf(false) }

    fun save() {
        scope.launch {
            store.save(baseUrl, apiKey)
            Api.configure(baseUrl, apiKey)
            if (baseUrl.isBlank()) {
                onMessage(context.getString(R.string.settings_url_empty, Api.baseUrl))
            } else {
                onMessage(context.getString(R.string.settings_saved))
            }
        }
    }

    fun testConnection() {
        if (testing) return
        testing = true
        scope.launch {
            Api.configure(baseUrl, apiKey)
            Api.call { Api.service().health() }
                .onSuccess {
                    onMessage(context.getString(R.string.settings_test_ok, it.status, it.version))
                }
                .onFailure {
                    onMessage(context.getString(R.string.state_error, it.message ?: ""))
                }
            testing = false
        }
    }

    Column(modifier = Modifier.fillMaxSize().padding(16.dp)) {
        OutlinedTextField(
            value = baseUrl,
            onValueChange = { baseUrl = it },
            label = { Text(stringResource(R.string.settings_base_url)) },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(
            value = apiKey,
            onValueChange = { apiKey = it },
            label = { Text(stringResource(R.string.settings_api_key)) },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(8.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = { save() }) {
                Text(stringResource(R.string.action_save))
            }
            Button(onClick = { testConnection() }, enabled = !testing) {
                Text(stringResource(R.string.settings_test_connection))
            }
        }
        Spacer(Modifier.height(16.dp))
        Text(
            stringResource(R.string.settings_hint),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}