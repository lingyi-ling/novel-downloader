package com.lingyi.noveldl.ui.search

import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
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
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.selection.toggleable
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.ArrowDropDown
import androidx.compose.material.icons.outlined.BookmarkAdd
import androidx.compose.material.icons.outlined.BookmarkAdded
import androidx.compose.material.icons.outlined.Clear
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Checkbox
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DatePicker
import androidx.compose.material3.DatePickerDialog
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LocalContentColor
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.rememberDatePickerState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
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
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.clearAndSetSemantics
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import com.lingyi.noveldl.R
import com.lingyi.noveldl.data.AddTaskRequest
import com.lingyi.noveldl.data.BrowseCategory
import com.lingyi.noveldl.data.BrowseRequest
import com.lingyi.noveldl.data.Api
import com.lingyi.noveldl.data.SearchResult
import com.lingyi.noveldl.data.SelectionStore
import com.lingyi.noveldl.data.decodeSelectedSites
import com.lingyi.noveldl.data.encodeSelectedSites
import com.lingyi.noveldl.data.selectedSitesParam
import com.lingyi.noveldl.data.siteSummary
import com.lingyi.noveldl.data.toggleSiteSelection
import com.lingyi.noveldl.ui.apiErrorMessage
import com.lingyi.noveldl.ui.searchPlaceholderRes
import kotlinx.coroutines.launch

@OptIn(ExperimentalLayoutApi::class)
@Composable
fun SearchScreen(onMessage: (String) -> Unit) {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val selectionStore = remember(context) { SelectionStore(context) }
    // The filter text lives in a ViewModel + SavedStateHandle (not in this
    // composable) so a tab switch or process death restores the panel instead
    // of silently running the next search unfiltered.
    val filterModel: SearchFilterViewModel = viewModel(
        factory = SearchFilterViewModel.factory(),
    )
    val filters by filterModel.state.collectAsState()
    val filterValidation = filters.validate()
    var showFilters by rememberSaveable { mutableStateOf(false) }
    var showDatePicker by remember { mutableStateOf(false) }
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
    // Add-to-queue requests in flight, so a double tap cannot enqueue twice.
    var addingKeys by remember { mutableStateOf<Set<String>>(emptySet()) }
    // Whether the visible state is the result of a completed query. An
    // untouched screen must not claim "no results found".
    var searched by remember { mutableStateOf(false) }
    // The action that produced the current error, so Retry repeats it. A
    // failed rankings or category load cannot be retried by re-running a
    // keyword search (which no-ops when the keyword field is empty).
    var retryAction by remember { mutableStateOf<(() -> Unit)?>(null) }

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

    fun runSearch(query: String) {
        if (query.isEmpty() || loading) return
        // Build the body from the live filter state: a re-search and every
        // retry therefore carry the current bounds without copying them around.
        // Invalid input is not sent (the server would answer 422); the panel is
        // opened so the inline error is visible even when it was collapsed.
        val request = filters.toSearchRequest(query, selectedSitesParam(selectedSites))
        if (request == null) {
            showFilters = true
            return
        }
        loading = true
        error = null
        scope.launch {
            val outcome = Api.call { Api.service().search(request) }
            outcome
                .onSuccess {
                    results = it.results
                    total = it.total
                    searched = true
                    retryAction = null
                }
                .onFailure {
                    error = apiErrorMessage(context, it)
                    retryAction = { runSearch(query) }
                }
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
                    searched = true
                    retryAction = null
                }
                .onFailure {
                    error = apiErrorMessage(context, it)
                    retryAction = { runRank() }
                }
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
                    searched = true
                    retryAction = null
                }
                .onFailure {
                    error = apiErrorMessage(context, it)
                    retryAction = { runCategory(categoryId) }
                }
            loading = false
        }
    }

    fun addTask(result: SearchResult) {
        val key = result.watchKey()
        if (key in addingKeys) return
        addingKeys = addingKeys + key
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
            addingKeys = addingKeys - key
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

    if (showDatePicker) {
        SearchDatePickerDialog(
            initialDate = filters.updatedAfter,
            onConfirm = { millis ->
                filterModel.updateUpdatedAfter(formatFilterDate(millis))
                showDatePicker = false
            },
            onDismiss = { showDatePicker = false },
        )
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
            // The field is only a display: the overlay below owns the
            // semantics, so TalkBack gets one control carrying the current
            // value plus the open action instead of a read-only field and a
            // second clickable node on top of it.
            val sitesValue = if (selectedSites.isEmpty()) {
                stringResource(R.string.search_sites_all)
            } else {
                siteSummary(selectedSites, siteLabels)
            }
            OutlinedTextField(
                value = sitesValue,
                onValueChange = {},
                readOnly = true,
                label = { Text(stringResource(R.string.search_sites_hint)) },
                trailingIcon = {
                    Icon(
                        Icons.Outlined.ArrowDropDown,
                        contentDescription = null,
                    )
                },
                singleLine = true,
                modifier = Modifier.fillMaxWidth().clearAndSetSemantics {},
            )
            // The transparent overlay opens the multi-select dialog instead of
            // the keyboard, and reads as "label: value, open sites".
            val sitesFieldState = stringResource(
                R.string.search_sites_field_state,
                stringResource(R.string.search_sites_hint),
                sitesValue,
            )
            val openSitesAction = stringResource(R.string.search_sites_dialog_title)
            Box(
                Modifier
                    .matchParentSize()
                    .semantics { contentDescription = sitesFieldState }
                    .clickable(
                        role = Role.DropdownList,
                        onClickLabel = openSitesAction,
                    ) { showSiteDialog = true },
            )
        }
        Spacer(Modifier.height(8.dp))
        SearchFilterToggleRow(
            filters = filters,
            expanded = showFilters,
            onToggle = { showFilters = !showFilters },
            onClear = filterModel::clear,
        )
        if (showFilters) {
            Spacer(Modifier.height(8.dp))
            SearchFilterPanel(
                filters = filters,
                validation = filterValidation,
                onMinWordsChange = filterModel::updateMinWords,
                onMaxWordsChange = filterModel::updateMaxWords,
                onPickDate = { showDatePicker = true },
                onClearDate = { filterModel.updateUpdatedAfter("") },
            )
        }
        Spacer(Modifier.height(8.dp))
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Button(onClick = { runSearch(keyword.trim()) }, enabled = !loading) {
                Text(stringResource(R.string.search_button))
            }
            if (loading) {
                // The adjacent text says the same thing; hiding the spinner
                // keeps TalkBack from announcing the state twice.
                CircularProgressIndicator(
                    modifier = Modifier.height(20.dp).clearAndSetSemantics {},
                )
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
                    modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite },
                )
                // A failed category load has its own retry: re-running the
                // keyword search would do nothing (the field may be empty).
                TextButton(onClick = { loadCategories() }, enabled = !categoriesLoading) {
                    Text(stringResource(R.string.action_retry))
                }
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
                modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite },
            )
            retryAction?.let { retry ->
                TextButton(onClick = retry) {
                    Text(stringResource(R.string.action_retry))
                }
            }
        }
        Spacer(Modifier.height(8.dp))
        if (results.isNotEmpty()) {
            Text(
                stringResource(R.string.search_results, total),
                style = MaterialTheme.typography.bodySmall,
            )
        } else {
            searchPlaceholderRes(
                hasSearched = searched,
                loading = loading,
                hasError = error != null,
            )?.let { placeholder ->
                Text(
                    stringResource(placeholder),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
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
                        Text(
                            "${result.site} · ID ${result.book_id}",
                            style = MaterialTheme.typography.labelSmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                            maxLines = 1,
                            overflow = TextOverflow.Ellipsis,
                        )
                        // The two actions wrap under the identity line at a
                        // 2.0 font scale instead of squeezing it to nothing.
                        FlowRow(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.spacedBy(8.dp, Alignment.End),
                            verticalArrangement = Arrangement.spacedBy(8.dp),
                        ) {
                            FollowIconButton(
                                followed = result.watchKey() in watchedKeys,
                                watching = result.watchKey() in watchingKeys,
                                onFollow = { follow(result) },
                            )
                            // The visible label is the same on every row;
                            // the description names the book so a screen
                            // reader user knows which result it applies to.
                            val addTaskLabel = stringResource(
                                R.string.search_add_task_named,
                                result.title.ifBlank { result.book_id },
                            )
                            TextButton(
                                onClick = { addTask(result) },
                                enabled = result.watchKey() !in addingKeys,
                                modifier = Modifier.semantics {
                                    contentDescription = addTaskLabel
                                },
                            ) {
                                Text(stringResource(R.string.action_add_to_queue))
                            }
                        }
                    }
                }
            }
        }
    }

/**
 * The "advanced filters" disclosure row. The count includes entries that are
 * not valid yet, so a malformed value is never hidden behind a collapsed panel.
 */
@Composable
private fun SearchFilterToggleRow(
    filters: SearchFilterState,
    expanded: Boolean,
    onToggle: () -> Unit,
    onClear: () -> Unit,
) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        TextButton(onClick = onToggle) {
            Text(
                stringResource(
                    if (expanded) R.string.search_filters_hide else R.string.search_filters_show,
                ),
            )
        }
        if (filters.isActive) {
            Text(
                stringResource(R.string.search_filters_active_count, filters.activeFilterCount),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.primary,
                modifier = Modifier.weight(1f),
            )
            TextButton(onClick = onClear) {
                Text(stringResource(R.string.search_filters_clear))
            }
        }
    }
}

/**
 * The expanded filter panel: numeric word bounds plus an inclusive
 * "updated after" date chosen from the Material date picker. Every field is
 * full width so a 2.0 font scale does not squeeze the labels/values.
 */
@Composable
private fun SearchFilterPanel(
    filters: SearchFilterState,
    validation: SearchFilterValidation,
    onMinWordsChange: (String) -> Unit,
    onMaxWordsChange: (String) -> Unit,
    onPickDate: () -> Unit,
    onClearDate: () -> Unit,
) {
    val dateLabel = stringResource(R.string.search_filters_updated_after)
    val dateValue: String = filters.updatedAfter.ifBlank {
        stringResource(R.string.search_filters_any_date)
    }
    val pickDateLabel = stringResource(R.string.search_filters_pick_date)
    // One description for the whole date control: label, current value and the
    // action. The visible label/value column below is cleared from semantics so
    // TalkBack does not announce the same information twice.
    val dateState = stringResource(
        R.string.search_filters_date_state,
        dateLabel,
        dateValue,
        pickDateLabel,
    )
    Column(
        modifier = Modifier.fillMaxWidth(),
        verticalArrangement = Arrangement.spacedBy(8.dp),
    ) {
        WordFilterField(
            value = filters.minWords,
            onValueChange = onMinWordsChange,
            label = stringResource(R.string.search_filters_min_words),
            error = validation.minWordsError,
            rangeError = validation.rangeError,
        )
        WordFilterField(
            value = filters.maxWords,
            onValueChange = onMaxWordsChange,
            label = stringResource(R.string.search_filters_max_words),
            error = validation.maxWordsError,
            rangeError = validation.rangeError,
        )
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Column(
                modifier = Modifier
                    .weight(1f)
                    .padding(vertical = 4.dp)
                    .clearAndSetSemantics {},
            ) {
                Text(dateLabel, style = MaterialTheme.typography.labelLarge)
                Text(
                    dateValue,
                    style = MaterialTheme.typography.bodyMedium,
                    color = if (filters.updatedAfter.isBlank()) {
                        MaterialTheme.colorScheme.onSurfaceVariant
                    } else {
                        MaterialTheme.colorScheme.onSurface
                    },
                )
            }
            TextButton(
                onClick = onPickDate,
                modifier = Modifier.semantics { contentDescription = dateState },
            ) {
                Text(pickDateLabel)
            }
            if (filters.updatedAfter.isNotBlank()) {
                IconButton(onClick = onClearDate) {
                    Icon(
                        Icons.Outlined.Clear,
                        contentDescription = stringResource(R.string.search_filters_clear_date),
                    )
                }
            }
        }
        if (validation.rangeError) {
            Text(
                stringResource(R.string.search_filters_range_invalid),
                color = MaterialTheme.colorScheme.error,
                style = MaterialTheme.typography.bodySmall,
                modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite },
            )
        }
        if (validation.updatedAfterError != null) {
            Text(
                stringResource(R.string.search_filters_invalid_date),
                color = MaterialTheme.colorScheme.error,
                style = MaterialTheme.typography.bodySmall,
                modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite },
            )
        }
        Text(
            stringResource(R.string.search_filters_optional_hint),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

@Composable
private fun WordFilterField(
    value: String,
    onValueChange: (String) -> Unit,
    label: String,
    error: WordFilterError?,
    rangeError: Boolean,
) {
    OutlinedTextField(
        value = value,
        onValueChange = onValueChange,
        label = { Text(label) },
        singleLine = true,
        // The min > max message is shared by both fields, so both turn red;
        // each field keeps its own entry-specific message when it has one.
        isError = error != null || rangeError,
        keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Number),
        supportingText = if (error == null) {
            null
        } else {
            {
                Text(
                    wordFilterErrorText(error),
                    modifier = Modifier.semantics { liveRegion = LiveRegionMode.Polite },
                )
            }
        },
        modifier = Modifier.fillMaxWidth(),
    )
}

@Composable
private fun wordFilterErrorText(error: WordFilterError): String = stringResource(
    when (error) {
        WordFilterError.NOT_A_NUMBER -> R.string.search_filters_words_not_number
        WordFilterError.NEGATIVE -> R.string.search_filters_words_negative
        WordFilterError.TOO_LARGE -> R.string.search_filters_words_too_large
    },
)

/**
 * Material date picker for the inclusive lower bound. The picker works in UTC
 * millis; [formatFilterDate] converts them back to the API's ISO date with the
 * same offset, so the chosen calendar day never shifts.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun SearchDatePickerDialog(
    initialDate: String,
    onConfirm: (Long) -> Unit,
    onDismiss: () -> Unit,
) {
    val pickerState = rememberDatePickerState(
        initialSelectedDateMillis = filterDateEpochMillis(initialDate),
    )
    DatePickerDialog(
        onDismissRequest = onDismiss,
        confirmButton = {
            TextButton(
                onClick = { pickerState.selectedDateMillis?.let(onConfirm) },
                enabled = pickerState.selectedDateMillis != null,
            ) {
                Text(stringResource(R.string.action_ok))
            }
        },
        dismissButton = {
            TextButton(onClick = onDismiss) {
                Text(stringResource(R.string.action_cancel))
            }
        },
    ) {
        DatePicker(
            state = pickerState,
            title = {
                Text(
                    stringResource(R.string.search_filters_date_dialog_title),
                    modifier = Modifier.padding(start = 24.dp, top = 16.dp, end = 12.dp),
                )
            },
        )
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
                            // The text below already announces the state.
                            CircularProgressIndicator(
                                modifier = Modifier.height(20.dp).clearAndSetSemantics {},
                            )
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
            // The checkbox is display-only here (the row owns the toggle);
            // pin the row to 48 dp so the touch target does not depend on the
            // checkbox's own minimum-size handling.
            .heightIn(min = 48.dp)
            .toggleable(
                value = checked,
                role = Role.Checkbox,
                onValueChange = { onToggle(key) },
            )
            .padding(vertical = 4.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        // Display-only: the row owns the toggle, so the checkbox must not add
        // a second focusable node with its own state.
        Checkbox(
            checked = checked,
            onCheckedChange = null,
            modifier = Modifier.clearAndSetSemantics {},
        )
        Spacer(Modifier.width(8.dp))
        Text(label, style = MaterialTheme.typography.bodyMedium)
    }
}