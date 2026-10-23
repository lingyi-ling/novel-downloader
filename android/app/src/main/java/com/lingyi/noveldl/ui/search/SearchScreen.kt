package com.lingyi.noveldl.ui.search

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
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import com.lingyi.noveldl.R
import com.lingyi.noveldl.data.AddTaskRequest
import com.lingyi.noveldl.data.Api
import com.lingyi.noveldl.data.SearchRequest
import com.lingyi.noveldl.data.SearchResult
import kotlinx.coroutines.launch

@Composable
fun SearchScreen(onMessage: (String) -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var keyword by rememberSaveable { mutableStateOf("") }
    var sitesText by rememberSaveable { mutableStateOf("") }
    var loading by remember { mutableStateOf(false) }
    var total by remember { mutableStateOf(0) }
    var error by remember { mutableStateOf<String?>(null) }
    var results by remember { mutableStateOf<List<SearchResult>>(emptyList()) }

    fun runSearch() {
        val query = keyword.trim()
        if (query.isEmpty() || loading) return
        loading = true
        error = null
        scope.launch {
            val sites = sitesText.split(',').map { it.trim() }.filter { it.isNotEmpty() }
            val outcome = Api.call {
                Api.service().search(
                    SearchRequest(
                        keyword = query,
                        sites = sites.ifEmpty { null },
                    ),
                )
            }
            outcome
                .onSuccess {
                    results = it.results
                    total = it.total
                }
                .onFailure { error = it.message }
            loading = false
        }
    }

    fun addTask(result: SearchResult) {
        scope.launch {
            Api.call {
                Api.service().addTask(
                    AddTaskRequest(
                        site = result.site,
                        book_id = result.book_id,
                        title = result.title,
                    ),
                )
            }.onSuccess {
                onMessage(context.getString(R.string.search_task_added))
            }.onFailure {
                onMessage(context.getString(R.string.state_error, it.message ?: ""))
            }
        }
    }

    Column(modifier = Modifier.fillMaxSize().padding(16.dp)) {
        OutlinedTextField(
            value = keyword,
            onValueChange = { keyword = it },
            label = { Text(stringResource(R.string.search_hint)) },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(
            value = sitesText,
            onValueChange = { sitesText = it },
            label = { Text(stringResource(R.string.search_sites_hint)) },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(8.dp))
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Button(onClick = { runSearch() }, enabled = !loading) {
                Text(stringResource(R.string.search_button))
            }
            if (loading) {
                CircularProgressIndicator(modifier = Modifier.height(20.dp))
                Text(
                    stringResource(R.string.search_searching),
                    style = MaterialTheme.typography.bodySmall,
                )
            }
        }
        error?.let {
            Spacer(Modifier.height(8.dp))
            Text(
                stringResource(R.string.state_error, it),
                color = MaterialTheme.colorScheme.error,
                style = MaterialTheme.typography.bodySmall,
            )
            TextButton(onClick = { runSearch() }) {
                Text(stringResource(R.string.action_retry))
            }
        }
        Spacer(Modifier.height(8.dp))
        if (results.isNotEmpty()) {
            Text(
                stringResource(R.string.search_results, total),
                style = MaterialTheme.typography.bodySmall,
            )
        } else if (!loading && error == null) {
            Text(
                stringResource(R.string.search_no_results),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        Spacer(Modifier.height(8.dp))
        LazyColumn(verticalArrangement = Arrangement.spacedBy(8.dp)) {
            items(results) { result ->
                Card(colors = CardDefaults.cardColors()) {
                    Column(Modifier.padding(12.dp)) {
                        Text(result.title, style = MaterialTheme.typography.titleMedium)
                        Spacer(Modifier.height(4.dp))
                        Text(
                            stringResource(
                                R.string.search_meta,
                                result.author.ifBlank { "-" },
                                result.word_count.ifBlank { "-" },
                                result.update_date.ifBlank { "-" },
                            ),
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                        if (result.latest_chapter.isNotBlank()) {
                            Text(
                                result.latest_chapter,
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                        }
                        Spacer(Modifier.height(4.dp))
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.SpaceBetween,
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Text(
                                "${result.site} · ID ${result.book_id}",
                                style = MaterialTheme.typography.labelSmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                            )
                            TextButton(onClick = { addTask(result) }) {
                                Text(stringResource(R.string.action_add_to_queue))
                            }
                        }
                    }
                }
            }
        }
    }
}
