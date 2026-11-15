package com.lingyi.noveldl.ui.history

import android.app.DownloadManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.net.Uri
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
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
import androidx.compose.material3.LinearProgressIndicator
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
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import com.lingyi.noveldl.R
import com.lingyi.noveldl.ui.apiErrorMessage
import com.lingyi.noveldl.data.Api
import com.lingyi.noveldl.data.ApiSettings
import com.lingyi.noveldl.data.DownloadFailureException
import com.lingyi.noveldl.data.DownloadProgress
import com.lingyi.noveldl.data.DownloadTarget
import com.lingyi.noveldl.data.HistoryDownloader
import com.lingyi.noveldl.data.HistoryFile
import com.lingyi.noveldl.data.SettingsStore
import com.lingyi.noveldl.data.customDownloadDirLabel
import com.lingyi.noveldl.data.downloadFileName
import com.lingyi.noveldl.data.resolveDownloadTarget
import com.lingyi.noveldl.ui.formatTimestamp
import com.lingyi.noveldl.ui.humanSize
import com.lingyi.noveldl.ui.listPlaceholderRes
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

@OptIn(ExperimentalMaterial3Api::class, ExperimentalLayoutApi::class)
@Composable
fun HistoryScreen(store: SettingsStore, onMessage: (String) -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val settings by store.settings.collectAsState(initial = ApiSettings())
    var files by remember { mutableStateOf<List<HistoryFile>>(emptyList()) }
    var error by remember { mutableStateOf<String?>(null) }
    var loading by remember { mutableStateOf(false) }
    // True once the first response arrived, so the empty state is not shown
    // while the initial load is still in flight.
    var loaded by remember { mutableStateOf(false) }
    val pendingDownloads = remember { mutableStateMapOf<Long, String>() }
    // Byte progress of active custom-directory downloads, keyed by file. The
    // streaming loop reports from Dispatchers.IO; a StateFlow makes those
    // writes thread-safe and Compose collects them on the main thread without
    // one dispatcher hop per chunk. A key mapped to null means "started, no
    // byte sample yet".
    val progressByFile = remember {
        MutableStateFlow<Map<String, DownloadProgress?>>(emptyMap())
    }
    val activeProgress by progressByFile.collectAsState()
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
                                // Name the common causes (space, file write,
                                // HTTP) instead of only the numeric code.
                                downloadFailureReasonRes(reason)?.let(appContext::getString)
                                    ?: appContext.getString(
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
                .onFailure { error = apiErrorMessage(context, it) }
            loading = false
            loaded = true
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
        // A classified SAF write failure gets its own wording (permission /
        // space / provider); everything else keeps the message it carries.
        val failureReason: (Throwable) -> String = { error ->
            if (error is DownloadFailureException) {
                context.getString(downloadFailureMessageRes(error.kind))
            } else {
                error.message.orEmpty()
            }
        }
        // Shared by the "no custom directory" case and the runtime fallback
        // after a revoked/deleted tree. The fallback flag picks a notice that
        // names the destination change instead of a plain "started" toast.
        fun enqueueSystemDownloads(fallback: Boolean) {
            HistoryDownloader.enqueue(context, file)
                .onSuccess { id ->
                    pendingDownloads[id] = displayName
                    onMessage(
                        if (fallback) {
                            context.getString(R.string.settings_download_dir_fallback)
                        } else {
                            context.getString(R.string.history_download_started, displayName)
                        },
                    )
                }
                .onFailure { onMessage(failureMessage(it.message.orEmpty())) }
        }

        scope.launch {
            // persistedUriPermissions is a binder call; keep it off the main
            // thread, and resolve against the latest grants so a directory
            // revoked since the screen opened is not attempted.
            val writeUris = withContext(Dispatchers.IO) {
                HistoryDownloader.writeGrantedTreeUris(context)
            }
            val storedTree = settings.downloadTreeUri
            when (val target = resolveDownloadTarget(storedTree, writeUris)) {
                is DownloadTarget.CustomTree -> {
                    // Mark the row active before the first byte arrives, so a
                    // slow connect already shows an indeterminate bar instead
                    // of an apparently dead button.
                    val key = downloadKey(file)
                    progressByFile.update { it + (key to null) }
                    try {
                        HistoryDownloader.downloadToDirectory(
                            context,
                            file,
                            Uri.parse(target.treeUri),
                            onProgress = { sample ->
                                progressByFile.update { it + (key to sample) }
                            },
                        )
                            .onSuccess { savedName ->
                                onMessage(
                                    context.getString(
                                        R.string.history_download_custom_done,
                                        customDownloadDirLabel(target.treeUri),
                                        savedName,
                                    ),
                                )
                            }
                            .onFailure { error ->
                                // A tree can disappear between the grant
                                // snapshot and the actual write (folder
                                // deleted, provider crashed, grant revoked
                                // mid-transfer). Do not fail the download:
                                // retry into the system Downloads directory
                                // and say so. A full volume or a broken
                                // provider is reported instead, because a
                                // silent destination change would hide the
                                // real problem.
                                val fallback = shouldFallBackToSystemDownloads(error)
                                if (fallback) {
                                    enqueueSystemDownloads(fallback = true)
                                } else {
                                    onMessage(failureMessage(failureReason(error)))
                                }
                            }
                    } finally {
                        // Progress is transient: the completion/failure notice
                        // is the durable result.
                        progressByFile.update { it - key }
                    }
                }
                DownloadTarget.SystemDownloads ->
                    enqueueSystemDownloads(fallback = storedTree.isNotBlank())
            }
        }
    }

    PullToRefreshBox(
        isRefreshing = loading,
        onRefresh = { load() },
        modifier = Modifier.fillMaxSize(),
    ) {
        Column(modifier = Modifier.fillMaxSize().padding(16.dp)) {
            FlowRow(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalArrangement = Arrangement.spacedBy(8.dp),
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
                    modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite },
                )
                TextButton(onClick = { load() }) {
                    Text(stringResource(R.string.action_retry))
                }
            }
            Spacer(Modifier.height(8.dp))
            if (files.isEmpty()) {
                listPlaceholderRes(
                    loading = !loaded || loading,
                    hasError = error != null,
                    emptyRes = R.string.state_no_items,
                )?.let { placeholder ->
                    Text(
                        stringResource(placeholder),
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
            }
            LazyColumn(
                modifier = Modifier.weight(1f),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                items(files) { file ->
                    // Same fallback as the download path, so a nameless entry
                    // still has a visible title and an actionable label.
                    val displayName = file.name.ifBlank {
                        downloadFileName(file.name, file.download_url, file.ext)
                    }
                    // Every row's button says "Download"; the description
                    // names the file so screen reader users can tell them
                    // apart, including in the controls list.
                    val downloadLabel = stringResource(
                        R.string.history_download_named,
                        displayName,
                    )
                    // Custom-directory downloads report their bytes here, so a
                    // large file never looks frozen. null = not downloading;
                    // a present entry with a null sample = connecting.
                    val rowKey = downloadKey(file)
                    val isActive = activeProgress.containsKey(rowKey)
                    val sample = activeProgress[rowKey]
                    val progressText = when {
                        !isActive -> null
                        sample == null -> stringResource(
                            R.string.history_download_progress_waiting,
                        )
                        sample.totalBytes == null -> stringResource(
                            R.string.history_download_progress_unknown,
                            humanSize(sample.bytesWritten),
                        )
                        else -> stringResource(
                            R.string.history_download_progress,
                            humanSize(sample.bytesWritten),
                            humanSize(sample.totalBytes),
                            sample.percent ?: 0,
                        )
                    }
                    Card(modifier = Modifier.fillMaxWidth()) {
                        Column(Modifier.padding(12.dp)) {
                            Text(displayName, style = MaterialTheme.typography.titleSmall)
                            Spacer(Modifier.height(4.dp))
                            Text(
                                "${formatTimestamp(file.mtime)} · " +
                                    humanSize(file.size) + " · ${file.ext}",
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                            if (progressText != null) {
                                Spacer(Modifier.height(6.dp))
                                val fraction = sample?.fraction
                                if (fraction != null) {
                                    LinearProgressIndicator(
                                        progress = { fraction },
                                        modifier = Modifier.fillMaxWidth().semantics {
                                            contentDescription = progressText
                                        },
                                    )
                                } else {
                                    // Unknown Content-Length: keep the bar
                                    // moving instead of claiming 0 %.
                                    LinearProgressIndicator(
                                        modifier = Modifier.fillMaxWidth().semantics {
                                            contentDescription = progressText
                                        },
                                    )
                                }
                                Spacer(Modifier.height(4.dp))
                                Text(
                                    progressText,
                                    style = MaterialTheme.typography.labelSmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                )
                            }
                            Spacer(Modifier.height(4.dp))
                            Button(
                                onClick = { download(file) },
                                // One transfer per file: a second tap while
                                // this row is streaming would write a second
                                // document and split the progress state.
                                enabled = !isActive,
                                modifier = Modifier.semantics {
                                    contentDescription = downloadLabel
                                },
                            ) {
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

/**
 * Key for per-file progress state. The download URL identifies a file; the
 * name covers entries the API returns without one (they still have to keep
 * separate progress rows).
 */
private fun downloadKey(file: HistoryFile): String =
    file.download_url.ifBlank { "name:${file.name}" }