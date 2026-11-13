package com.lingyi.noveldl.ui.settings

import android.content.Intent
import android.net.Uri
import android.provider.Settings
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.toggleable
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
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
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.clearAndSetSemantics
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import com.lingyi.noveldl.R
import com.lingyi.noveldl.notifications.TaskNotifier
import com.lingyi.noveldl.ui.apiErrorMessage
import com.lingyi.noveldl.data.Api
import com.lingyi.noveldl.data.ApiSettings
import com.lingyi.noveldl.data.DownloadTarget
import com.lingyi.noveldl.data.EmbeddedEngine
import com.lingyi.noveldl.data.HistoryDownloader
import com.lingyi.noveldl.data.SettingsStore
import com.lingyi.noveldl.data.UpdateCenter
import com.lingyi.noveldl.data.UpdateFailure
import com.lingyi.noveldl.data.UpdatePhase
import com.lingyi.noveldl.data.appVersionName
import com.lingyi.noveldl.data.configureTargetUrl
import com.lingyi.noveldl.data.SitesSummary
import com.lingyi.noveldl.data.customTreeUnavailable
import com.lingyi.noveldl.data.resolveDownloadTarget
import com.lingyi.noveldl.data.standaloneTargetUrl
import com.lingyi.noveldl.data.toSitesSummary
import com.lingyi.noveldl.data.treeUriDisplayName
import com.lingyi.noveldl.ui.healthSummary
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

@OptIn(ExperimentalLayoutApi::class)
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
    // True once the first sites response arrived, so the initial frame does
    // not claim "no sites enabled" before the request has run.
    var sitesLoaded by remember { mutableStateOf(false) }
    // Localized fallback for the health line; a hardcoded English "unknown"
    // would leak into the Chinese UI.
    val unknownStatus = stringResource(R.string.state_unknown)
    // Re-read on every resume: the user may grant or revoke the permission
    // from system settings while this screen is in the back stack.
    var notificationsAllowed by remember {
        mutableStateOf(TaskNotifier.canPostNotifications(context))
    }
    // Tree URIs the app can currently write to. Re-read on resume: the user
    // may revoke a SAF grant (or uninstall/clear the provider app) while the
    // screen is in the back stack, and the warning must appear before the
    // next download attempt.
    var persistedWriteUris by remember {
        mutableStateOf(HistoryDownloader.writeGrantedTreeUris(context))
    }

    fun save() {
        scope.launch {
            // Re-read the persisted mode instead of trusting the collected
            // state: DataStore emits asynchronously, so a toggle followed
            // quickly by Save could still see the old `standalone` value and
            // point the app back at the remote URL.
            val latest = store.settings.first()
            // In standalone mode the engine stays the API target; the typed
            // URL is persisted for when the user switches back.
            val target = latest.configureTargetUrl(baseUrl)
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
            // Same freshness rule as save(): test the target the switch is
            // actually in, not a possibly stale snapshot of the flow.
            val latest = store.settings.first()
            Api.configure(latest.configureTargetUrl(baseUrl), apiKey)
            Api.call { Api.service().health() }
                .onSuccess {
                    health = healthSummary(it.status, it.version, unknownStatus)
                    onMessage(context.getString(R.string.settings_test_ok, it.status, it.version))
                }
                .onFailure {
                    onMessage(context.getString(R.string.state_error, apiErrorMessage(context, it)))
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
                .onFailure { sitesError = apiErrorMessage(context, it) }
            sitesLoading = false
            sitesLoaded = true
        }
    }

    LaunchedEffect(Unit) { loadSites() }

    val lifecycleOwner = LocalLifecycleOwner.current
    DisposableEffect(lifecycleOwner, context) {
        val observer = LifecycleEventObserver { _, event ->
            if (event == Lifecycle.Event.ON_RESUME) {
                notificationsAllowed = TaskNotifier.canPostNotifications(context)
                persistedWriteUris = HistoryDownloader.writeGrantedTreeUris(context)
            }
        }
        lifecycleOwner.lifecycle.addObserver(observer)
        onDispose { lifecycleOwner.lifecycle.removeObserver(observer) }
    }

    val treePicker = rememberLauncherForActivityResult(
        ActivityResultContracts.OpenDocumentTree(),
    ) { uri ->
        if (uri == null) return@rememberLauncherForActivityResult
        scope.launch {
            // Persist the grant so the permission survives reboots. Only
            // remember the directory when the provider actually granted a
            // persistable permission: otherwise the next download would fail
            // while the settings page claimed the folder was in use.
            val granted = runCatching {
                context.contentResolver.takePersistableUriPermission(
                    uri,
                    Intent.FLAG_GRANT_READ_URI_PERMISSION or
                        Intent.FLAG_GRANT_WRITE_URI_PERMISSION,
                )
            }.isSuccess
            if (granted) {
                store.saveDownloadDirectory(uri.toString())
                persistedWriteUris = HistoryDownloader.writeGrantedTreeUris(context)
                onMessage(context.getString(R.string.settings_download_dir_saved))
            } else {
                // Keep the previously selected (working) directory untouched.
                onMessage(
                    context.getString(R.string.settings_download_dir_permission_failed),
                )
            }
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
            persistedWriteUris = HistoryDownloader.writeGrantedTreeUris(context)
            onMessage(context.getString(R.string.settings_download_dir_reset))
        }
    }

    /** Applies the standalone-engine toggle; also used by the row's semantics. */
    fun setStandalone(enabled: Boolean) {
        scope.launch {
            // The persisted remote URL/API key for the "off" direction, read
            // fresh so a just-saved edit is used.
            val latest = store.settings.first()
            store.saveStandalone(enabled)
            if (enabled) EmbeddedEngine.ensureStarted(context)
            // The toggle must take effect immediately: without reconfiguring,
            // the app would keep talking to the previous base URL until the
            // process restarts.
            val usable = Api.configure(
                standaloneTargetUrl(enabled, latest),
                latest.apiKey,
            )
            if (enabled && !usable) {
                // The engine URL is always valid; unreachable in practice,
                // kept as a guard.
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
    }

    /** Opens the system screen that controls this app's notification toggle. */
    fun openNotificationSettings() {
        val direct = Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
            .putExtra(Settings.EXTRA_APP_PACKAGE, context.packageName)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        val opened = runCatching { context.startActivity(direct) }.isSuccess
        if (opened) return
        // Not every OEM exposes the per-app notification page; app details is
        // always present and links to the same toggle.
        val fallback = Intent(
            Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
            Uri.parse("package:${context.packageName}"),
        ).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        runCatching { context.startActivity(fallback) }
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
        // Wraps instead of overflowing when a locale or large font scale makes
        // both labels wider than a narrow portrait screen.
        FlowRow(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
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
        // The whole row is the switch target: it clears the 48 dp touch size
        // and merges title/hint/switch into one talkback node with a Switch
        // role, instead of an unlabeled switch floating next to plain text.
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .heightIn(min = 48.dp)
                .toggleable(
                    value = settings.standalone,
                    role = Role.Switch,
                    onValueChange = ::setStandalone,
                )
                .padding(vertical = 4.dp),
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
            // Display-only: the row is the switch target, so the control
            // itself must not expose a second focusable node.
            Switch(
                checked = settings.standalone,
                onCheckedChange = null,
                modifier = Modifier.clearAndSetSemantics {},
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
                    modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite },
                )
                TextButton(onClick = { loadSites() }) {
                    Text(stringResource(R.string.action_retry))
                }
            }
            sitesLoading || !sitesLoaded -> {
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
        val storedTree = settings.downloadTreeUri
        val target = resolveDownloadTarget(storedTree, persistedWriteUris)
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
        if (customTreeUnavailable(storedTree, persistedWriteUris)) {
            // The stored URI is kept (so "choose directory" can replace it)
            // but downloads fall back to the system Downloads directory.
            Text(
                stringResource(R.string.settings_download_dir_unavailable),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.error,
                modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite },
            )
        }
        Spacer(Modifier.height(8.dp))
        FlowRow(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Button(onClick = { treePicker.launch(null) }) {
                Text(stringResource(R.string.settings_download_pick_directory))
            }
            Button(
                onClick = { resetDownloadDirectory() },
                // Enabled even when the tree is unavailable: the user must be
                // able to clear a broken selection.
                enabled = storedTree.isNotBlank(),
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

        // ----- Notifications -----
        Text(
            stringResource(R.string.settings_section_notifications),
            style = MaterialTheme.typography.titleSmall,
        )
        Spacer(Modifier.height(8.dp))
        Text(
            stringResource(
                if (notificationsAllowed) {
                    R.string.settings_notifications_enabled
                } else {
                    R.string.settings_notifications_disabled
                },
            ),
            style = MaterialTheme.typography.bodySmall,
            color = if (notificationsAllowed) {
                MaterialTheme.colorScheme.onSurfaceVariant
            } else {
                MaterialTheme.colorScheme.error
            },
        )
        if (!notificationsAllowed) {
            TextButton(onClick = { openNotificationSettings() }) {
                Text(stringResource(R.string.settings_notifications_open))
            }
        }

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
            modifier = Modifier
                .fillMaxWidth()
                .heightIn(min = 48.dp)
                .toggleable(
                    value = settings.autoCheckUpdates,
                    role = Role.Switch,
                    onValueChange = { enabled ->
                        scope.launch { store.saveAutoCheckUpdates(enabled) }
                    },
                )
                .padding(vertical = 4.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                stringResource(R.string.settings_auto_check),
                style = MaterialTheme.typography.bodyMedium,
                modifier = Modifier.weight(1f),
            )
            Switch(
                checked = settings.autoCheckUpdates,
                onCheckedChange = null,
                modifier = Modifier.clearAndSetSemantics {},
            )
        }
        Spacer(Modifier.height(8.dp))
        FlowRow(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
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
    UpdateFailure.NOT_NEWER -> stringResource(R.string.update_failed_not_newer)
    UpdateFailure.INSUFFICIENT_SPACE ->
        stringResource(R.string.update_failed_insufficient_space)
    UpdateFailure.INSTALL_FAILED -> stringResource(R.string.update_failed_install)
    UpdateFailure.DOWNLOAD_FAILED -> stringResource(
        R.string.update_failed_download,
        failed.detail.orEmpty(),
    )
}

internal fun formatUpdateTime(millis: Long): String =
    SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.getDefault()).format(Date(millis))