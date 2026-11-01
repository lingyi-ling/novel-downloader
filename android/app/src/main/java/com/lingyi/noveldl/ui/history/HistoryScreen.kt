package com.lingyi.noveldl.ui.history

import android.app.DownloadManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.net.Uri
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateMapOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import com.lingyi.noveldl.R
import com.lingyi.noveldl.data.Api
import com.lingyi.noveldl.data.ApiSettings
import com.lingyi.noveldl.data.DownloadTarget
import com.lingyi.noveldl.data.HistoryDownloader
import com.lingyi.noveldl.data.HistoryFile
import com.lingyi.noveldl.data.SettingsStore
import com.lingyi.noveldl.data.customDownloadDirLabel
import com.lingyi.noveldl.data.downloadFileName
import com.lingyi.noveldl.data.resolveDownloadTarget
import com.lingyi.noveldl.ui.formatTimestamp
import com.lingyi.noveldl.ui.humanSize
import kotlinx.coroutines.launch

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun HistoryScreen(store: SettingsStore, onMessage: (String) -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val settings by store.settings.collectAsState(initial = ApiSettings())
    var files by remember { mutableStateOf<List<HistoryFile>>(emptyList()) }
    var error by remember { mutableStateOf<String?>(null) }
    var loading by remember { mutableStateOf(false) }
    val pendingDownloads = remember { mutableStateMapOf<Long, String>() }
    val latestMessage by rememberUpdatedState(onMessage)

    // DownloadManager reports the final result as a broadcast. Only ids we
    // enqueued from this screen are considered, so other apps' downloads are
    // ignored.
    DisposableEffect(context) {
        val appContext = context.applicationContext
        val receiver = object : BroadcastReceiver() {
            override fun onReceive(receiverContext: Context?, intent: Intent?) {
                if (intent?.action != DownloadManager.ACTION_DOWNLOAD_COMPLETE) return
                val id = intent.getLongExtra(DownloadManager.EXTRA_DOWNLOAD_ID, -1L)
                val fileName = pendingDownloads.remove(id) ?: return
                val manager = appContext.getSystemService(DownloadManager::class.java) ?: return
                val (status, reason) = queryDownload(manager, id)
                when (status) {
                    DownloadManager.STATUS_SUCCESSFUL ->
                        latestMessage(
                            appContext.getString(R.string.history_download_done, fileName),
                        )
                    DownloadManager.STATUS_FAILED ->
                        latestMessage(
                            appContext.getString(
                                R.string.history_download_failed,
                                fileName,
                                appContext.getString(
                                    R.string.history_download_failure_reason,
                                    reason,
                                ),
                            ),
                        )
                    else -> Unit
                }
            }
        }
        ContextCompat.registerReceiver(
            appContext,
            receiver,
            IntentFilter(DownloadManager.ACTION_DOWNLOAD_COMPLETE),
            // ACTION_DOWNLOAD_COMPLETE is a protected system broadcast: it is
            // still delivered to a NOT_EXPORTED receiver, while a hostile app
            // cannot spoof the action to consume one of our pending ids
            // (which would swallow the real completion message).
            ContextCompat.RECEIVER_NOT_EXPORTED,
        )
        onDispose { runCatching { appContext.unregisterReceiver(receiver) } }
    }

    fun load() {
        if (loading) return
        loading = true
        scope.launch {
            Api.call { Api.service().history() }
                .onSuccess {
                    files = it.files
                    error = null
                }
                .onFailure { error = it.message }
            loading = false
        }
    }

    LaunchedEffect(Unit) { load() }

    fun download(file: HistoryFile) {
        val displayName = file.name.ifBlank {
            downloadFileName(file.name, file.download_url, file.ext)
        }
        val failureMessage: (String) -> String = { reason ->
            context.getString(
                R.string.history_download_failed,
                displayName,
                reason.ifBlank { context.getString(R.string.history_download_unknown_error) },
            )
        }
        when (val target = resolveDownloadTarget(settings.downloadTreeUri)) {
            is DownloadTarget.CustomTree -> scope.launch {
                // DownloadManager cannot write to SAF tree URIs, so the custom
                // directory path streams the file itself; the result is known
                // as soon as the call returns.
                HistoryDownloader.downloadToDirectory(
                    context,
                    file,
                    Uri.parse(target.treeUri),
                )
                    .onSuccess {
                        onMessage(
                            context.getString(
                                R.string.history_download_custom_done,
                                customDownloadDirLabel(target.treeUri),
                                displayName,
                            ),
                        )
                    }
                    .onFailure { onMessage(failureMessage(it.message.orEmpty())) }
            }
            DownloadTarget.SystemDownloads -> HistoryDownloader.enqueue(context, file)
                .onSuccess { id ->
                    pendingDownloads[id] = displayName
                    onMessage(context.getString(R.string.history_download_started, displayName))
                }
                .onFailure { onMessage(failureMessage(it.message.orEmpty())) }
        }
    }

    PullToRefreshBox(
        isRefreshing = loading,
        onRefresh = { load() },
        modifier = Modifier.fillMaxSize(),
    ) {
        Column(modifier = Modifier.fillMaxSize().padding(16.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text(
                    stringResource(R.string.history_files, files.size),
                    style = MaterialTheme.typography.bodySmall,
                )
                Button(onClick = { load() }, enabled = !loading) {
                    Text(stringResource(R.string.action_refresh))
                }
            }
            error?.let {
                Spacer(Modifier.height(8.dp))
                Text(
                    stringResource(R.string.state_error, it),
                    color = MaterialTheme.colorScheme.error,
                    style = MaterialTheme.typography.bodySmall,
                )
                TextButton(onClick = { load() }) {
                    Text(stringResource(R.string.action_retry))
                }
            }
            Spacer(Modifier.height(8.dp))
            if (files.isEmpty() && !loading && error == null) {
                Text(
                    stringResource(R.string.state_empty),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            LazyColumn(
                modifier = Modifier.weight(1f),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                items(files) { file ->
                    Card(modifier = Modifier.fillMaxWidth()) {
                        Column(Modifier.padding(12.dp)) {
                            Text(file.name, style = MaterialTheme.typography.titleSmall)
                            Spacer(Modifier.height(4.dp))
                            Text(
                                "${formatTimestamp(file.mtime)} · " +
                                    humanSize(file.size) + " · ${file.ext}",
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                            Spacer(Modifier.height(4.dp))
                            Button(onClick = { download(file) }) {
                                Text(stringResource(R.string.action_download))
                            }
                        }
                    }
                }
            }
        }
    }
}

/** Reads the final status/reason of a download; -1 when it is unknown. */
private fun queryDownload(manager: DownloadManager, id: Long): Pair<Int, Int> {
    return try {
        val cursor = manager.query(DownloadManager.Query().setFilterById(id))
        cursor.use {
            if (!it.moveToFirst()) return -1 to 0
            val statusIndex = it.getColumnIndex(DownloadManager.COLUMN_STATUS)
            val reasonIndex = it.getColumnIndex(DownloadManager.COLUMN_REASON)
            val status = if (statusIndex >= 0) it.getInt(statusIndex) else -1
            val reason = if (reasonIndex >= 0) it.getInt(reasonIndex) else 0
            status to reason
        }
    } catch (_: Exception) {
        -1 to 0
    }
}