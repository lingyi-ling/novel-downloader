package com.lingyi.noveldl.ui.search

import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.toggleable
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.ArrowDropDown
import androidx.compose.material.icons.outlined.BookmarkAdd
import androidx.compose.material.icons.outlined.BookmarkAdded
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Checkbox
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.lingyi.noveldl.R
import com.lingyi.noveldl.data.AddTaskRequest
import com.lingyi.noveldl.data.BrowseCategory
import com.lingyi.noveldl.data.BrowseRequest
import com.lingyi.noveldl.data.Api
import com.lingyi.noveldl.data.SearchRequest
import com.lingyi.noveldl.data.SearchResult
import com.lingyi.noveldl.data.SelectionStore
import com.lingyi.noveldl.data.decodeSelectedSites
import com.lingyi.noveldl.data.encodeSelectedSites
import com.lingyi.noveldl.data.selectedSitesParam
import com.lingyi.noveldl.data.siteSummary
import com.lingyi.noveldl.data.toggleSiteSelection
import com.lingyi.noveldl.ui.apiErrorMessage
import kotlinx.coroutines.launch

@Composable
fun SearchScreen(onMessage: (String) -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val selectionStore = remember(context) { SelectionStore(context) }
    var keyword by rememberSaveable { mutableStateOf("") }
    // Stored as the comma-separated format shared with SelectionStore.
    var selectedSitesEncoded by rememberSaveable {
        mutableStateOf(encodeSelectedSites(selectionStore.selectedSites()))
    }
    val selectedSites = remember(selectedSitesEncoded) {
        decodeSelectedSites(selectedSitesEncoded)
    }
    var siteLabels by remember { mutableStateOf<Map<String, String>>(emptyMap()) }
    var sitesLoading by remember { mutableStateOf(true) }
    var sitesError by remember { mutableStateOf<String?>(null) }
    var siteReloadKey by rememberSaveable { mutableIntStateOf(0) }
    var showSiteDialog by rememberSaveable { mutableStateOf(false) }
    var loading by remember { mutableStateOf(false) }
    var total by remember { mutableIntStateOf(0) }
    var error by remember { mutableStateOf<String?>(null) }
    var results by remember { mutableStateOf<List<SearchResult>>(emptyList()) }
    // Keys ("site/book_id") already in the watchlist: seeded from the server
    // on entry and extended locally on success, so a followed result keeps a
    // disabled button across new searches within this visit.
    var watchedKeys by remember { mutableStateOf<Set<String>>(emptySet()) }
    // Follow requests currently in flight, so a row cannot be followed twice.
    var watchingKeys by remember { mutableStateOf<Set<String>>(emptySet()) }

    // Browse: the site publishes a ranking page and a category tree, so the
    // search page can show content without a keyword.
    val browseSite = "fanqienovel"
    var showBrowse by rememberSaveable { mutableStateOf(false) }
    var browseGender by rememberSaveable { mutableStateOf("male") }
    var categories by remember {
        mutableStateOf<Map<String, List<BrowseCategory>>>(emptyMap())
    }
    var categoriesLoading by remember { mutableStateOf(false) }
    var categoriesError by remember { mutableStateOf<String?>(null) }

    // Best-effort seed of what the server already follows; failures stay
    // silent because the local set still prevents duplicate prompts.
    LaunchedEffect(Unit) {
        Api.call { Api.service().watchlist() }
            .onSuccess { response ->
                watchedKeys = watchedKeys +
                    response.books.map { searchWatchKey(it.site, it.book_id) }
            }
    }

    LaunchedEffect(siteReloadKey) {
        sitesLoading = true
        Api.call { Api.service().sites() }
            .onSuccess {
                siteLabels = it.search
                sitesError = null
            }
            .onFailure { sitesError = apiErrorMessage(context, it) }
        sitesLoading = false
    }

    fun updateSelection(site: String) {
        val next = toggleSiteSelection(selectedSites, site)
        selectedSitesEncoded = encodeSelectedSites(next)
        selectionStore.saveSelectedSites(next)
    }

    fun runSearch() {
        val query = keyword.trim()
        if (query.isEmpty() || loading) return
        loading = true
        error = null
        scope.launch {
            val outcome = Api.call {
                Api.service().search(
                    SearchRequest(
                        keyword = query,
                        sites = selectedSitesParam(selectedSites),
                    ),
                )
            }
            outcome
                .onSuccess {
                    results = it.results
                    total = it.total
                }
                .onFailure { error = apiErrorMessage(context, it) }
            loading = false
        }
    }

    fun runRank() {
        if (loading) return
        loading = true
        error = null
        scope.launch {
            Api.call { Api.service().browse(BrowseRequest(site = browseSite)) }
                .onSuccess {
                    results = it.results
                    total = it.total
                }
                .onFailure { error = apiErrorMessage(context, it) }
            loading = false
        }
    }

    fun loadCategories() {
        if (categoriesLoading) return
        categoriesLoading = true
        categoriesError = null
        scope.launch {
            Api.call { Api.service().browseCategories(browseSite) }
                .onSuccess { categories = it.groups }
                .onFailure { categoriesError = apiErrorMessage(context, it) }
            categoriesLoading = false
        }
    }

    fun runCategory(categoryId: String) {
        if (loading) return
        loading = true
        error = null
        scope.launch {
            Api.call {
                Api.service().browse(
                    BrowseRequest(
                        site = browseSite,
                        category_id = categoryId,
                        gender = browseGender,
                    ),
                )
            }
                .onSuccess {
                    results = it.results
                    total = it.total
                }
                .onFailure { error = apiErrorMessage(context, it) }
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
                onMessage(context.getString(R.string.state_error, apiErrorMessage(context, it)))
            }
        }
    }

    /** Follows a result via `POST /api/v1/watchlist`; no-ops when already followed. */
    fun follow(result: SearchResult) {
        val target = followRequest(result) ?: return
        val key = result.watchKey()
        if (key in watchedKeys || key in watchingKeys) return
        watchingKeys = watchingKeys + key
        scope.launch {
            val outcome = Api.call { Api.service().addWatch(target) }
            watchingKeys = watchingKeys - key
            outcome
                .onSuccess {
                    watchedKeys = watchedKeys + key
                    onMessage(context.getString(R.string.search_follow_added))
                }
                .onFailure {
                    onMessage(context.getString(R.string.state_error, apiErrorMessage(context, it)))
                }
        }
    }

    if (showSiteDialog) {
        SiteSelectionDialog(
            labels = siteLabels,
            selected = selectedSites,
            loading = sitesLoading,
            error = sitesError,
            onToggle = ::updateSelection,
            onClear = {
                selectedSitesEncoded = ""
                selectionStore.saveSelectedSites(emptyList())
            },
            onRetry = { siteReloadKey++ },
            onDismiss = { showSiteDialog = false },
        )
    }

    // The controls and the results share one LazyColumn so a landscape phone
    // or a large font scale can still scroll everything; a fixed Column plus a
    // nested list could leave the results with zero height and no way to
    // reach them.
    LazyColumn(
        modifier = Modifier.fillMaxSize().padding(16.dp),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        item(key = "search-header") {
            Column {
        OutlinedTextField(
            value = keyword,
            onValueChange = { keyword = it },
            label = { Text(stringResource(R.string.search_hint)) },
            singleLine = true,
            modifier = Modifier.fillMaxWidth(),
        )
        Spacer(Modifier.height(8.dp))
        Box {
            OutlinedTextField(
                value = if (selectedSites.isEmpty()) {
                    stringResource(R.string.search_sites_all)
                } else {
                    siteSummary(selectedSites, siteLabels)
                },
                onValueChange = {},
                readOnly = true,
                label = { Text(stringResource(R.string.search_sites_hint)) },
                trailingIcon = {
                    Icon(
                        Icons.Outlined.ArrowDropDown,
                        contentDescription = stringResource(R.string.search_sites_dialog_title),
                    )
                },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )
            // The text field is read-only; a transparent overlay makes the whole
            // field open the multi-select dialog instead of the keyboard. It
            // carries the field's description and an action label so screen
            // readers announce a labeled control instead of a bare clickable.
            val sitesFieldDescription = stringResource(R.string.search_sites_hint)
            val openSitesAction = stringResource(R.string.search_sites_dialog_title)
            Box(
                Modifier
                    .matchParentSize()
                    .semantics { contentDescription = sitesFieldDescription }
                    .clickable(onClickLabel = openSitesAction) { showSiteDialog = true },
            )
        }
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
        Spacer(Modifier.height(4.dp))
        Row(
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            TextButton(onClick = { runRank() }, enabled = !loading) {
                Text(stringResource(R.string.browse_rank))
            }
            TextButton(
                onClick = {
                    showBrowse = !showBrowse
                    if (showBrowse && categories.isEmpty()) loadCategories()
                },
            ) {
                Text(stringResource(R.string.browse_categories))
            }
            if (categoriesLoading) {
                val categoriesLoadingLabel = stringResource(R.string.state_loading)
                CircularProgressIndicator(
                    modifier = Modifier
                        .height(16.dp)
                        .semantics { contentDescription = categoriesLoadingLabel },
                )
            }
        }
        if (showBrowse) {
            categoriesError?.let {
                Text(
                    stringResource(R.string.browse_failed, it),
                    color = MaterialTheme.colorScheme.error,
                    style = MaterialTheme.typography.bodySmall,
                )
            }
            Row(
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                verticalAlignment = Alignment.CenterVertically,
            ) {
                FilterChip(
                    selected = browseGender == "male",
                    onClick = { browseGender = "male" },
                    label = { Text(stringResource(R.string.browse_male)) },
                )
                FilterChip(
                    selected = browseGender == "female",
                    onClick = { browseGender = "female" },
                    label = { Text(stringResource(R.string.browse_female)) },
                )
            }
            Spacer(Modifier.height(4.dp))
            Row(
                modifier = Modifier.fillMaxWidth().horizontalScroll(rememberScrollState()),
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                val audience = if (browseGender == "male") "male" else "female"
                (categories[audience] ?: emptyList()).forEach { category ->
                    FilterChip(
                        selected = false,
                        onClick = { runCategory(category.id) },
                        label = { Text(category.name) },
                    )
                }
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
            }
        }
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
                                maxLines = 1,
                                overflow = TextOverflow.Ellipsis,
                                modifier = Modifier.weight(1f),
                            )
                            Row(verticalAlignment = Alignment.CenterVertically) {
                                FollowIconButton(
                                    followed = result.watchKey() in watchedKeys,
                                    watching = result.watchKey() in watchingKeys,
                                    onFollow = { follow(result) },
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

/**
 * The per-row "follow" icon button. Disabled once the book is in the
 * watchlist (server seed or a successful follow) or while the request is in
 * flight, so a result can never be followed twice.
 */
@Composable
private fun FollowIconButton(
    followed: Boolean,
    watching: Boolean,
    onFollow: () -> Unit,
) {
    IconButton(onClick = onFollow, enabled = !followed && !watching) {
        Icon(
            imageVector = if (followed) Icons.Outlined.BookmarkAdded else Icons.Outlined.BookmarkAdd,
            contentDescription = stringResource(
                when {
                    followed -> R.string.search_follow_in_list
                    watching -> R.string.search_follow_in_progress
                    else -> R.string.search_follow_add
                },
            ),
            tint = if (followed) MaterialTheme.colorScheme.primary else LocalContentColor.current,
        )
    }
}

/**
 * Multi-select dialog backed by `GET /api/v1/sites`. Leaving every checkbox
 * unchecked means "all sites" and is sent as an omitted `sites` field.
 */
@Composable
private fun SiteSelectionDialog(
    labels: Map<String, String>,
    selected: List<String>,
    loading: Boolean,
    error: String?,
    onToggle: (String) -> Unit,
    onClear: () -> Unit,
    onRetry: () -> Unit,
    onDismiss: () -> Unit,
) {
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(stringResource(R.string.search_sites_dialog_title)) },
        text = {
            Column(
                modifier = Modifier
                    .heightIn(max = 420.dp)
                    .verticalScroll(rememberScrollState()),
            ) {
                Text(
                    stringResource(R.string.search_sites_dialog_hint),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Spacer(Modifier.height(8.dp))
                when {
                    loading && labels.isEmpty() -> {
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            CircularProgressIndicator(modifier = Modifier.height(20.dp))
                            Spacer(Modifier.width(8.dp))
                            Text(
                                stringResource(R.string.state_loading),
                                style = MaterialTheme.typography.bodySmall,
                            )
                        }
                    }
                    error != null && labels.isEmpty() -> {
                        Text(
                            stringResource(R.string.search_sites_load_failed, error),
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.error,
                        )
                        TextButton(onClick = onRetry) {
                            Text(stringResource(R.string.action_retry))
                        }
                    }
                    labels.isEmpty() -> {
                        Text(
                            stringResource(R.string.state_no_items),
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                    else -> {
                        labels.entries
                            .sortedBy { it.value }
                            .forEach { (key, label) -> SiteCheckboxRow(key, label, selected, onToggle) }
                        // Keep persisted keys that the server no longer reports
                        // visible so the user can clear them.
                        selected
                            .filterNot { it in labels }
                            .forEach { key -> SiteCheckboxRow(key, key, selected, onToggle) }
                    }
                }
            }
        },
        confirmButton = {
            TextButton(onClick = onDismiss) {
                Text(stringResource(R.string.action_close))
            }
        },
        dismissButton = {
            TextButton(onClick = onClear) {
                Text(stringResource(R.string.search_sites_clear))
            }
        },
    )
}

@Composable
private fun SiteCheckboxRow(
    key: String,
    label: String,
    selected: List<String>,
    onToggle: (String) -> Unit,
) {
    val checked = key in selected
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .toggleable(
                value = checked,
                role = Role.Checkbox,
                onValueChange = { onToggle(key) },
            )
            .padding(vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Checkbox(checked = checked, onCheckedChange = null)
        Spacer(Modifier.width(8.dp))
        Text(label, style = MaterialTheme.typography.bodyMedium)
    }
}