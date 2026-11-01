package com.lingyi.noveldl

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.lingyi.noveldl.data.Api
import com.lingyi.noveldl.data.EmbeddedEngine
import com.lingyi.noveldl.data.SettingsStore
import com.lingyi.noveldl.data.UpdateCenter
import com.lingyi.noveldl.data.UpdatePhase
import com.lingyi.noveldl.data.UpdateSchedule
import com.lingyi.noveldl.data.appVersionName
import com.lingyi.noveldl.notifications.TaskNotifier
import com.lingyi.noveldl.ui.NovelApp
import com.lingyi.noveldl.ui.NovelTheme
import com.lingyi.noveldl.ui.settings.updateFailureText
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.launch

class MainActivity : ComponentActivity() {

    private val notificationPermissionLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        TaskNotifier.ensureChannel(this)
        UpdateCenter.init(this)
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
                    Box {
                        NovelApp(store)
                        StartupUpdatePrompt(store)
                    }
                }
            }
        }
    }

    /**
     * Returning from the "install unknown apps" screen retries the pending
     * install; without this the user would have to tap the dialog button again.
     */
    override fun onResume() {
        super.onResume()
        UpdateCenter.onResume(this)
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

/**
 * Runs the throttled startup check once per process and renders the update
 * dialog. A failed or negative check stays silent — the user only hears about
 * a release that is actually newer.
 */
@Composable
private fun StartupUpdatePrompt(store: SettingsStore) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val phase by UpdateCenter.phase.collectAsState()
    val visible by UpdateCenter.dialogVisible.collectAsState()

    LaunchedEffect(Unit) {
        val settings = store.settings.first()
        if (UpdateSchedule.shouldCheck(
                autoCheck = settings.autoCheckUpdates,
                lastCheckMillis = settings.lastUpdateCheck,
                nowMillis = System.currentTimeMillis(),
            )
        ) {
            UpdateCenter.check(
                currentVersion = appVersionName(context),
                notifyDialog = true,
            ) { info ->
                if (info != null) {
                    scope.launch { store.recordUpdateCheck(System.currentTimeMillis()) }
                }
            }
        }
    }

    if (!visible) return
    UpdateDialog(
        phase = phase,
        onDismiss = { UpdateCenter.dismiss() },
        onDownload = { UpdateCenter.download() },
        onInstall = { UpdateCenter.install(context) },
        onOpenNotes = { url -> openUrl(context, url) },
    )
}

@Composable
private fun UpdateDialog(
    phase: UpdatePhase,
    onDismiss: () -> Unit,
    onDownload: () -> Unit,
    onInstall: () -> Unit,
    onOpenNotes: (String) -> Unit,
) {
    val info = when (phase) {
        is UpdatePhase.Available -> phase.info
        is UpdatePhase.Downloading -> phase.info
        is UpdatePhase.Ready -> phase.info
        is UpdatePhase.Failed -> phase.info
        else -> null
    } ?: return

    val downloading = phase is UpdatePhase.Downloading
    val ready = phase is UpdatePhase.Ready

    AlertDialog(
        onDismissRequest = { if (!downloading) onDismiss() },
        title = { Text(stringResource(R.string.update_dialog_title)) },
        text = {
            Column {
                Text(
                    stringResource(R.string.update_dialog_message, info.latest, info.current),
                    style = MaterialTheme.typography.bodyMedium,
                )
                val notesUrl = info.notesUrl
                if (notesUrl != null) {
                    TextButton(onClick = { onOpenNotes(notesUrl) }) {
                        Text(stringResource(R.string.update_dialog_notes))
                    }
                }
                when {
                    downloading -> {
                        Text(
                            stringResource(R.string.update_dialog_downloading),
                            style = MaterialTheme.typography.bodySmall,
                        )
                        Spacer(Modifier.height(8.dp))
                        LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
                    }
                    phase is UpdatePhase.Failed -> Text(
                        updateFailureText(phase),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.error,
                    )
                    ready -> Text(
                        stringResource(R.string.update_install_hint),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
        },
        confirmButton = {
            when {
                downloading -> Button(onClick = {}, enabled = false) {
                    Text(stringResource(R.string.update_action_downloading))
                }
                ready -> Button(onClick = onInstall) {
                    Text(stringResource(R.string.update_action_install))
                }
                phase is UpdatePhase.Failed -> Button(onClick = onDownload) {
                    Text(stringResource(R.string.action_retry))
                }
                else -> Button(onClick = onDownload) {
                    Text(stringResource(R.string.update_action_download))
                }
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss, enabled = !downloading) {
                Text(stringResource(R.string.update_action_later))
            }
        },
    )
}

private fun openUrl(context: Context, url: String) {
    val intent = Intent(Intent.ACTION_VIEW, Uri.parse(url))
        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
    runCatching { context.startActivity(intent) }
}
