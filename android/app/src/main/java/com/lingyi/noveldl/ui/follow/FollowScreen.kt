package com.lingyi.noveldl.ui.follow

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
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
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
import com.lingyi.noveldl.data.WatchCheckItem
import com.lingyi.noveldl.data.WatchEntry
import com.lingyi.noveldl.data.WatchlistAddRequest
import com.lingyi.noveldl.data.WatchlistCheckRequest
import kotlinx.coroutines.launch

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun FollowScreen(onMessage: (String) -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var books by remember { mutableStateOf<List<WatchEntry>>(emptyList()) }
    var error by remember { mutableStateOf<String?>(null) }
    var refreshKey by remember { mutableIntStateOf(0) }
    var refreshing by remember { mutableStateOf(false) }
    var checking by remember { mutableStateOf(false) }
    var adding by remember { mutableStateOf(false) }
    var showAddDialog by remember { mutableStateOf(false) }
    // Check results whose chapter changed and that carry a ready-to-post task
    // body. The server only *attaches* the body; the client must still submit
    // it via POST /api/v1/tasks (see [queue]).
    var queueable by remember { mutableStateOf<List<WatchCheckItem>>(emptyList()) }
    // Entries whose task body has been submitted to the download queue.
    // Screen-level state, so the disabled button survives pull-to-refresh
    // and further check runs within this visit.
    var queuedKeys by remember { mutableStateOf<Set<String>>(emptySet()) }
    // Entries with a submission currently in flight.
    var queueingKeys by remember { mutableStateOf<Set<String>>(emptySet()) }

    LaunchedEffect(refreshKey) {
        Api.call { Api.service().watchlist() }
            .onSuccess {
                books = it.books
                error = null
            }
            .onFailure { error = it.message }
        refreshing = false
    }

    fun refreshNow() {
        if (refreshing) return
        refreshing = true
        refreshKey++
    }

    fun add(site: String, bookId: String, title: String?) {
        if (adding) return
        adding = true
        error = null
        scope.launch {
            Api.call {
                Api.service().addWatch(WatchlistAddRequest(site = site, book_id = bookId, title = title))
            }
                .onSuccess {
                    showAddDialog = false
                    refreshNow()
                }
                .onFailure { error = it.message }
            adding = false
        }
    }

    fun remove(entry: WatchEntry) {
        scope.launch {
            Api.call { Api.service().removeWatch(entry.site, entry.book_id) }
                .onSuccess {
                    queueable = queueable.filterNot {
                        it.site == entry.site && it.book_id == entry.book_id
                    }
                    // Drop the local queue memory too, so re-following the
                    // same book can be queued again if it changes later.
                    val key = watchItemKey(entry.site, entry.book_id)
                    queuedKeys = queuedKeys - key
                    queueingKeys = queueingKeys - key
                    onMessage(context.getString(R.string.follow_remove_done))
                    refreshNow()
                }
                .onFailure { onMessage(context.getString(R.string.state_error, it.message ?: "")) }
        }
    }

    fun checkUpdates() {
        if (checking) return
        checking = true
        error = null
        scope.launch {
            Api.call { Api.service().checkWatchlist(WatchlistCheckRequest(download = true)) }
                .onSuccess { result ->
                    queueable = result.needs_download.filter { it.task != null }
                    onMessage(
                        context.getString(R.string.follow_check_done, result.updated, result.failed),
                    )
                    refreshNow()
                }
                .onFailure { onMessage(context.getString(R.string.state_error, it.message ?: "")) }
            checking = false
        }
    }

    fun queue(entry: WatchCheckItem) {
        // Only submit entries the server gave a concrete task body for.
        val target = queueTarget(entry) ?: return
        val key = entry.key()
        if (key in queuedKeys || key in queueingKeys) return
        queueingKeys = queueingKeys + key
        scope.launch {
            val outcome = Api.call { Api.service().addTask(target) }
            queueingKeys = queueingKeys - key
            outcome
                .onSuccess {
                    queuedKeys = queuedKeys + key
                    onMessage(
                        context.getString(
                            R.string.follow_queue_done,
                            entry.task?.title ?: entry.title,
                        ),
                    )
                }
                .onFailure {
                    onMessage(context.getString(R.string.state_error, it.message ?: ""))
                }
        }
    }

    PullToRefreshBox(
        isRefreshing = refreshing,
        onRefresh = { refreshNow() },
        modifier = Modifier.fillMaxSize(),
    ) {
        Column(modifier = Modifier.fillMaxSize().padding(16.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp, Alignment.End),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Button(onClick = { showAddDialog = true }) {
                    Text(stringResource(R.string.follow_add))
                }
                TextButton(onClick = { checkUpdates() }, enabled = !checking) {
                    Text(stringResource(R.string.follow_check_updates))
                }
                TextButton(onClick = { refreshNow() }) {
                    Text(stringResource(R.string.action_refresh))
                }
            }
            error?.let {
                Text(
                    stringResource(R.string.state_error, it),
                    color = MaterialTheme.colorScheme.error,
                    style = MaterialTheme.typography.bodySmall,
                )
                TextButton(onClick = { refreshNow() }) {
                    Text(stringResource(R.string.action_retry))
                }
                Spacer(Modifier.height(8.dp))
            }
            if (books.isEmpty() && error == null) {
                Text(
                    stringResource(R.string.follow_empty),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Spacer(Modifier.height(8.dp))
            } else if (books.isNotEmpty()) {
                Spacer(Modifier.height(8.dp))
                Text(
                    stringResource(R.string.follow_books, books.size),
                    style = MaterialTheme.typography.bodySmall,
                )
            }
            LazyColumn(
                modifier = Modifier.weight(1f),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                items(books) { entry ->
                    val key = watchItemKey(entry.site, entry.book_id)
                    WatchCard(
                        entry = entry,
                        queueable = queueable.any {
                            it.site == entry.site && it.book_id == entry.book_id
                        },
                        queued = key in queuedKeys,
                        queueing = key in queueingKeys,
                        onRemove = { remove(entry) },
                        onQueue = { queueable.firstOrNull {
                            it.site == entry.site && it.book_id == entry.book_id
                        }?.let(::queue) },
                    )
                }
            }
        }
    }

    if (showAddDialog) {
        AddFollowDialog(
            onDismiss = { if (!adding) showAddDialog = false },
            onConfirm = ::add,
        )
    }
}

@Composable
private fun WatchCard(
    entry: WatchEntry,
    queueable: Boolean,
    queued: Boolean,
    queueing: Boolean,
    onRemove: () -> Unit,
    onQueue: () -> Unit,
) {
    Card(modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(12.dp)) {
            Text(
                entry.title.ifBlank { entry.book_id },
                style = MaterialTheme.typography.titleSmall,
            )
            Spacer(Modifier.height(4.dp))
            Text(
                "${entry.site} · ID ${entry.book_id}",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            if (entry.last_chapter.isNotBlank()) {
                Spacer(Modifier.height(4.dp))
                Text(
                    stringResource(R.string.follow_last_chapter, entry.last_chapter),
                    style = MaterialTheme.typography.bodySmall,
                )
            }
            if (entry.updated_at.isNotBlank()) {
                Spacer(Modifier.height(2.dp))
                Text(
                    stringResource(R.string.follow_updated_at, entry.updated_at),
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            if (queueable) {
                Spacer(Modifier.height(4.dp))
                Text(
                    stringResource(R.string.follow_needs_download),
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.primary,
                )
            }
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(8.dp, Alignment.End),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                if (queueable) {
                    // Disabled once submitted (queued) or while the request is
                    // in flight, so an entry can never be queued twice.
                    Button(
                        onClick = onQueue,
                        enabled = !queued && !queueing,
                    ) {
                        Text(
                            when {
                                queueing -> stringResource(R.string.follow_queue_in_progress)
                                queued -> stringResource(R.string.follow_queued)
                                else -> stringResource(R.string.action_add_to_queue)
                            },
                        )
                    }
                }
                TextButton(onClick = onRemove) {
                    Text(stringResource(R.string.follow_remove))
                }
            }
        }
    }
}

@Composable
private fun AddFollowDialog(
    onDismiss: () -> Unit,
    onConfirm: (site: String, bookId: String, title: String?) -> Unit,
) {
    // Held inside the dialog so the fields reset whenever it reopens.
    var site by remember { mutableStateOf("") }
    var bookId by remember { mutableStateOf("") }
    var title by remember { mutableStateOf("") }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(stringResource(R.string.follow_add_dialog_title)) },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(
                    value = site,
                    onValueChange = { site = it },
                    label = { Text(stringResource(R.string.follow_site_hint)) },
                    singleLine = true,
                )
                OutlinedTextField(
                    value = bookId,
                    onValueChange = { bookId = it },
                    label = { Text(stringResource(R.string.follow_book_id_hint)) },
                    singleLine = true,
                )
                OutlinedTextField(
                    value = title,
                    onValueChange = { title = it },
                    label = { Text(stringResource(R.string.follow_title_hint)) },
                    singleLine = true,
                )
            }
        },
        confirmButton = {
            TextButton(
                onClick = { onConfirm(site.trim(), bookId.trim(), title.trim().ifEmpty { null }) },
                enabled = site.isNotBlank() && bookId.isNotBlank(),
            ) {
                Text(stringResource(R.string.follow_add))
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text(stringResource(R.string.action_cancel))
            }
        },
    )
}
