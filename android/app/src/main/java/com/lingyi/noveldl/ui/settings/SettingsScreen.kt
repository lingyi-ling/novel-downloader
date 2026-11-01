package com.lingyi.noveldl.ui.settings

import android.content.Intent
import android.net.Uri
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import com.lingyi.noveldl.R
import com.lingyi.noveldl.data.Api
import com.lingyi.noveldl.data.ApiSettings
import com.lingyi.noveldl.data.DownloadTarget
import com.lingyi.noveldl.data.EmbeddedEngine
import com.lingyi.noveldl.data.SettingsStore
import com.lingyi.noveldl.data.UpdateCenter
import com.lingyi.noveldl.data.UpdateFailure
import com.lingyi.noveldl.data.UpdatePhase
import com.lingyi.noveldl.data.appVersionName
import com.lingyi.noveldl.data.configureTargetUrl
import com.lingyi.noveldl.data.SitesSummary
import com.lingyi.noveldl.data.resolveDownloadTarget
import com.lingyi.noveldl.data.standaloneTargetUrl
import com.lingyi.noveldl.data.toSitesSummary
import com.lingyi.noveldl.data.treeUriDisplayName
import com.lingyi.noveldl.ui.healthSummary
import kotlinx.coroutines.launch
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

@Composable
fun SettingsScreenView(store: SettingsStore, onMessage: (String) -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val settings by store.settings.collectAsState(initial = ApiSettings())
    val updatePhase by UpdateCenter.phase.collectAsState()
    // Keyed on the saved URL/key only: picking a download directory re-emits
    // `settings` and must not wipe unsaved text-field edits.
    var baseUrl by remember(settings.baseUrl, settings.apiKey) { mutableStateOf(settings.baseUrl) }
    var apiKey by remember(settings.baseUrl, settings.apiKey) { mutableStateOf(settings.apiKey) }
    var testing by remember { mutableStateOf(false) }
    // Persistent "status · version" line from the latest successful health call.
    var health by remember { mutableStateOf<String?>(null) }

    var sitesSummary by remember { mutableStateOf<SitesSummary?>(null) }
    var sitesLoading by remember { mutableStateOf(false) }
    var sitesError by remember { mutableStateOf<String?>(null) }

    fun save() {
        scope.launch {
            // In standalone mode the engine stays the API target; the typed
            // URL is persisted for when the user switches back.
            val target = settings.configureTargetUrl(baseUrl)
            val usable = Api.configure(target, apiKey)
            store.save(baseUrl, apiKey)
            when {
                baseUrl.isBlank() ->
                    onMessage(context.getString(R.string.settings_url_empty, Api.baseUrl))
                !usable ->
                    onMessage(
                        context.getString(
                            R.string.settings_url_invalid,
                            Api.baseUrl,
                        ),
                    )
                else -> onMessage(context.getString(R.string.settings_saved))
            }
        }
    }

    fun testConnection() {
        if (testing) return
        testing = true
        scope.launch {
            Api.configure(settings.configureTargetUrl(baseUrl), apiKey)
            Api.call { Api.service().health() }
                .onSuccess {
                    health = healthSummary(it.status, it.version)
                    onMessage(context.getString(R.string.settings_test_ok, it.status, it.version))
                }
                .onFailure {
                    onMessage(context.getString(R.string.state_error, it.message ?: ""))
                }
            testing = false
        }
    }

    fun loadSites() {
        if (sitesLoading) return
        sitesLoading = true
        scope.launch {
            Api.call { Api.service().sites() }
                .onSuccess {
                    sitesSummary = it.toSitesSummary()
                    sitesError = null
                }
                .onFailure { sitesError = it.message }
            sitesLoading = false
        }
    }

    LaunchedEffect(Unit) { loadSites() }

    val treePicker = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenDocumentTree(),
    ) { uri ->
        if (uri == null) return@rememberLauncherForActivityResult
        scope.launch {
            // Persist the grant so the permission survives reboots; if the
            // provider refuses, the download falls back to a clear error
            // instead of writing somewhere the user did not choose.
            runCatching {
                context.contentResolver.takePersistableUriPermission(
                    uri,
                    Intent.FLAG_GRANT_READ_URI_PERMISSION or
                        Intent.FLAG_GRANT_WRITE_URI_PERMISSION,
                )
            }
            store.saveDownloadDirectory(uri.toString())
            onMessage(context.getString(R.string.settings_download_dir_saved))
        }
    }

    fun resetDownloadDirectory() {
        scope.launch {
            val previous = settings.downloadTreeUri
            store.saveDownloadDirectory(null)
            runCatching {
                context.contentResolver.releasePersistableUriPermission(
                    Uri.parse(previous),
                    Intent.FLAG_GRANT_READ_URI_PERMISSION or
                        Intent.FLAG_GRANT_WRITE_URI_PERMISSION,
                )
            }
            onMessage(context.getString(R.string.settings_download_dir_reset))
        }
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(16.dp),
    ) {
        // ----- Connection -----
        Text(
            stringResource(R.string.settings_section_connection),
            style = MaterialTheme.typography.titleSmall,
        )
        Spacer(Modifier.height(8.dp))
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
        Spacer(Modifier.height(8.dp))
        Text(
            health
                ?.let { stringResource(R.string.settings_connection_status, it) }
                ?: stringResource(R.string.settings_connection_untested),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(8.dp))
        Text(
            stringResource(R.string.settings_hint),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )

        Spacer(Modifier.height(12.dp))
        // ----- Standalone engine -----
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    stringResource(R.string.settings_standalone_title),
                    style = MaterialTheme.typography.bodyMedium,
                )
                Text(
                    stringResource(R.string.settings_standalone_hint),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            Switch(
                checked = settings.standalone,
                onCheckedChange = { enabled ->
                    scope.launch {
                        store.saveStandalone(enabled)
                        if (enabled) EmbeddedEngine.ensureStarted(context)
                        // The toggle must take effect immediately: without
                        // reconfiguring, the app would keep talking to the
                        // previous base URL until the process restarts.
                        val usable = Api.configure(
                            standaloneTargetUrl(enabled, settings),
                            settings.apiKey,
                        )
                        if (enabled && !usable) {
                            // The engine URL is always valid; unreachable in
                            // practice, kept as a guard.
                            onMessage(
                                context.getString(
                                    R.string.settings_url_invalid,
                                    Api.baseUrl,
                                ),
                            )
                        } else {
                            onMessage(
                                context.getString(
                                    if (enabled) {
                                        R.string.settings_standalone_on
                                    } else {
                                        R.string.settings_standalone_off
                                    },
                                ),
                            )
                        }
                    }
                },
            )
        }
        if (settings.standalone && EmbeddedEngine.lastError != null) {
            Text(
                stringResource(R.string.settings_standalone_error, EmbeddedEngine.lastError.orEmpty()),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.error,
            )
        }

        Spacer(Modifier.height(16.dp))
        HorizontalDivider()
        Spacer(Modifier.height(16.dp))

        // ----- Sites overview (counts only, no site names) -----
        Text(
            stringResource(R.string.settings_section_sites),
            style = MaterialTheme.typography.titleSmall,
        )
        Spacer(Modifier.height(8.dp))
        when {
            sitesError != null -> {
                Text(
                    stringResource(R.string.settings_sites_failed, sitesError.orEmpty()),
                    color = MaterialTheme.colorScheme.error,
                    style = MaterialTheme.typography.bodySmall,
                )
                TextButton(onClick = { loadSites() }) {
                    Text(stringResource(R.string.action_retry))
                }
            }
            sitesLoading -> {
                Text(
                    stringResource(R.string.settings_sites_loading),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            else -> {
                val summary = sitesSummary
                if (summary == null || summary.isEmpty) {
                    Text(
                        stringResource(R.string.settings_sites_empty),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                } else {
                    Text(
                        stringResource(R.string.settings_sites_search_count, summary.searchCount),
                        style = MaterialTheme.typography.bodySmall,
                    )
                    Text(
                        stringResource(
                            R.string.settings_sites_download_count,
                            summary.downloadCount,
                        ),
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
            }
        }

        Spacer(Modifier.height(16.dp))
        HorizontalDivider()
        Spacer(Modifier.height(16.dp))

        // ----- Download directory -----
        Text(
            stringResource(R.string.settings_section_download),
            style = MaterialTheme.typography.titleSmall,
        )
        Spacer(Modifier.height(8.dp))
        val target = resolveDownloadTarget(settings.downloadTreeUri)
        val targetLabel = when (target) {
            is DownloadTarget.CustomTree -> stringResource(
                R.string.settings_download_target_custom,
                treeUriDisplayName(target.treeUri),
            )
            DownloadTarget.SystemDownloads -> stringResource(
                R.string.settings_download_target_default,
            )
        }
        Text(
            targetLabel,
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(8.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            Button(onClick = { treePicker.launch(null) }) {
                Text(stringResource(R.string.settings_download_pick_directory))
            }
            Button(
                onClick = { resetDownloadDirectory() },
                enabled = target is DownloadTarget.CustomTree,
            ) {
                Text(stringResource(R.string.settings_download_reset_directory))
            }
        }
        Spacer(Modifier.height(8.dp))
        Text(
            stringResource(R.string.settings_download_hint),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )

        Spacer(Modifier.height(16.dp))
        HorizontalDivider()
        Spacer(Modifier.height(16.dp))

        // ----- Updates -----
        Text(
            stringResource(R.string.settings_section_update),
            style = MaterialTheme.typography.titleSmall,
        )
        Spacer(Modifier.height(8.dp))
        val versionName = appVersionName(context)
        Text(
            stringResource(R.string.settings_current_version, versionName),
            style = MaterialTheme.typography.bodySmall,
        )
        Text(
            if (settings.lastUpdateCheck > 0L) {
                stringResource(
                    R.string.settings_last_check,
                    formatUpdateTime(settings.lastUpdateCheck),
                )
            } else {
                stringResource(R.string.settings_last_check_never)
            },
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(8.dp))
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                stringResource(R.string.settings_auto_check),
                style = MaterialTheme.typography.bodyMedium,
                modifier = Modifier.weight(1f),
            )
            Switch(
                checked = settings.autoCheckUpdates,
                onCheckedChange = { enabled ->
                    scope.launch { store.saveAutoCheckUpdates(enabled) }
                },
            )
        }
        Spacer(Modifier.height(8.dp))
        Row(
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            val busy = updatePhase is UpdatePhase.Checking ||
                updatePhase is UpdatePhase.Downloading
            Button(
                onClick = {
                    UpdateCenter.check(versionName, notifyDialog = false) { info ->
                        if (info != null) {
                            scope.launch {
                                store.recordUpdateCheck(System.currentTimeMillis())
                            }
                        }
                    }
                },
                enabled = !busy,
            ) {
                Text(stringResource(R.string.settings_check_now))
            }
            // The download action only makes sense once a release payload is
            // known; a failed check carries no asset to fetch.
            val pendingUpdate = when (val phase = updatePhase) {
                is UpdatePhase.Available -> phase.info
                is UpdatePhase.Failed -> phase.info
                else -> null
            }
            when {
                updatePhase is UpdatePhase.Ready -> TextButton(
                    onClick = { UpdateCenter.install(context) },
                ) {
                    Text(stringResource(R.string.update_action_install))
                }
                pendingUpdate != null -> TextButton(
                    onClick = { UpdateCenter.download() },
                ) {
                    Text(stringResource(R.string.update_action_download))
                }
                else -> Unit
            }
        }
        val updateStatus = when (val phase = updatePhase) {
            is UpdatePhase.Checking -> stringResource(R.string.update_state_checking)
            is UpdatePhase.UpToDate -> stringResource(
                R.string.settings_update_latest,
                phase.info.current,
            )
            is UpdatePhase.Available -> stringResource(
                R.string.settings_update_available,
                phase.info.latest,
            )
            is UpdatePhase.Downloading -> stringResource(R.string.update_dialog_downloading)
            is UpdatePhase.Ready -> stringResource(R.string.update_install_hint)
            is UpdatePhase.Failed -> updateFailureText(phase)
            UpdatePhase.Idle -> null
        }
        if (updateStatus != null) {
            Text(
                updateStatus,
                style = MaterialTheme.typography.bodySmall,
                color = if (updatePhase is UpdatePhase.Failed) {
                    MaterialTheme.colorScheme.error
                } else {
                    MaterialTheme.colorScheme.onSurfaceVariant
                },
            )
        }
    }
}

/** Human-readable text of an update failure, shared by the dialog and settings. */
@Composable
internal fun updateFailureText(failed: UpdatePhase.Failed): String = when (failed.reason) {
    UpdateFailure.CHECK_FAILED -> stringResource(R.string.update_failed_check)
    UpdateFailure.NO_ASSET -> stringResource(R.string.update_failed_no_asset)
    UpdateFailure.NO_CHECKSUM -> stringResource(R.string.update_failed_no_checksum)
    UpdateFailure.CHECKSUM_MISMATCH -> stringResource(R.string.update_failed_checksum)
    UpdateFailure.INSECURE_URL -> stringResource(R.string.update_failed_insecure)
    UpdateFailure.INSTALL_FAILED -> stringResource(R.string.update_failed_install)
    UpdateFailure.DOWNLOAD_FAILED -> stringResource(
        R.string.update_failed_download,
        failed.detail.orEmpty(),
    )
}

internal fun formatUpdateTime(millis: Long): String =
    SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.getDefault()).format(Date(millis))