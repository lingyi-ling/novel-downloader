package com.lingyi.noveldl.ui

import androidx.annotation.StringRes
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.Download
import androidx.compose.material.icons.outlined.History
import androidx.compose.material.icons.outlined.LibraryMusic
import androidx.compose.material.icons.outlined.Search
import androidx.compose.material.icons.outlined.Settings
import androidx.compose.material.icons.outlined.Visibility
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.NavigationBar
import androidx.compose.material3.NavigationBarItem
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.LifecycleEventObserver
import androidx.lifecycle.compose.LocalLifecycleOwner
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.navigation.NavGraph.Companion.findStartDestination
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import com.lingyi.noveldl.R
import com.lingyi.noveldl.data.SettingsStore
import com.lingyi.noveldl.notifications.TaskOpenViewModel
import com.lingyi.noveldl.ui.follow.FollowScreen
import com.lingyi.noveldl.ui.history.HistoryScreen
import com.lingyi.noveldl.ui.music.MusicExportViewModel
import com.lingyi.noveldl.ui.music.MusicJobViewModel
import com.lingyi.noveldl.ui.music.MusicScreen
import com.lingyi.noveldl.ui.music.MusicSearchViewModel
import com.lingyi.noveldl.ui.search.SearchScreen
import com.lingyi.noveldl.ui.settings.SettingsScreenView
import com.lingyi.noveldl.ui.tasks.TasksScreen
import kotlinx.coroutines.launch

// internal so the JVM navigation test can inspect the bottom bar entries.
internal data class NavItem(
    val route: String,
    @StringRes val label: Int,
    val icon: ImageVector,
)

/** Route shared by the bottom bar and notification-driven navigation. */
internal const val TASKS_ROUTE = "tasks"

internal val navItems = listOf(
    NavItem("search", R.string.nav_search, Icons.Outlined.Search),
    NavItem(TASKS_ROUTE, R.string.nav_tasks, Icons.Outlined.Download),
    NavItem("history", R.string.nav_history, Icons.Outlined.History),
    NavItem("music", R.string.nav_music, Icons.Outlined.LibraryMusic),
    NavItem("follow", R.string.nav_follow, Icons.Outlined.Visibility),
)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun NovelApp(store: SettingsStore, taskOpen: TaskOpenViewModel) {
    val navController = rememberNavController()
    val snackbarHostState = remember { SnackbarHostState() }
    val scope = rememberCoroutineScope()
    val backStackEntry by navController.currentBackStackEntryAsState()
    val route = backStackEntry?.destination?.route ?: "search"
    // Latest notification tap, if any. MainActivity raises it on cold start,
    // onNewIntent and on a restored intent, so the same path covers the app
    // being killed, in the foreground, or restored from recents.
    val openRequest by taskOpen.request.collectAsState()

    LaunchedEffect(openRequest?.token) {
        val request = openRequest ?: return@LaunchedEffect
        navController.navigate(TASKS_ROUTE) {
            // Same options as the bottom bar: keep one copy of each tab and
            // restore its saved state instead of stacking destinations.
            popUpTo(navController.graph.findStartDestination().id) {
                saveState = true
            }
            launchSingleTop = true
            restoreState = true
        }
        // Without a task id there is nothing for the tasks screen to locate;
        // acknowledge here so the one-shot request does not linger.
        if (request.taskId == null) taskOpen.consume(request.token)
    }

    // The music download job is owned here, at Activity scope, instead of by
    // the music composable: switching tabs (or recomposing the screen) used to
    // cancel the watch and lose the progress line, and a stale screen could
    // resubmit a job that was still running. The host lifecycle gates the
    // poll loop so it parks while the app is in the background.
    val musicJobs: MusicJobViewModel = viewModel(
        factory = MusicJobViewModel.factory(LocalContext.current),
    )
    // Same lifetime for the search itself: the keyword, platform, loaded pages,
    // search_id map and checkbox selection must survive a tab switch, otherwise
    // coming back forces a fresh search and loses what was selected.
    val musicSearch: MusicSearchViewModel = viewModel(
        factory = MusicSearchViewModel.factory(LocalContext.current),
    )
    // The playlist export is also owned here: leaving the music tab used to
    // cancel it (its scope was the composition) and drop the path it wrote.
    // Its factory binds the cache that keeps downloaded paths across restarts.
    val musicExport: MusicExportViewModel = viewModel(
        factory = MusicExportViewModel.factory(LocalContext.current),
    )
    val hostLifecycle = LocalLifecycleOwner.current
    DisposableEffect(hostLifecycle, musicJobs) {
        val observer = LifecycleEventObserver { _, event ->
            when (event) {
                Lifecycle.Event.ON_RESUME -> musicJobs.setForeground(true)
                Lifecycle.Event.ON_PAUSE -> musicJobs.setForeground(false)
                else -> Unit
            }
        }
        hostLifecycle.lifecycle.addObserver(observer)
        onDispose { hostLifecycle.lifecycle.removeObserver(observer) }
    }

    val notify: (String) -> Unit = { message ->
        scope.launch { snackbarHostState.showSnackbar(message) }
    }

    val titleRes = when (route) {
        TASKS_ROUTE -> R.string.nav_tasks
        "history" -> R.string.nav_history
        "music" -> R.string.nav_music
        "follow" -> R.string.nav_follow
        "settings" -> R.string.nav_settings
        else -> R.string.nav_search
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(stringResource(titleRes)) },
                actions = {
                    IconButton(onClick = { navController.navigate("settings") }) {
                        Icon(
                            Icons.Outlined.Settings,
                            contentDescription = stringResource(R.string.nav_settings),
                        )
                    }
                },
            )
        },
        snackbarHost = { SnackbarHost(snackbarHostState) },
        bottomBar = {
            NavigationBar {
                navItems.forEach { item ->
                    NavigationBarItem(
                        selected = route == item.route,
                        onClick = {
                            navController.navigate(item.route) {
                                popUpTo(navController.graph.findStartDestination().id) {
                                    saveState = true
                                }
                                launchSingleTop = true
                                restoreState = true
                            }
                        },
                        icon = { Icon(item.icon, contentDescription = null) },
                        label = { Text(stringResource(item.label)) },
                    )
                }
            }
        },
    ) { innerPadding ->
        NavHost(
            navController = navController,
            startDestination = "search",
            modifier = Modifier.padding(innerPadding),
        ) {
            composable("search") { SearchScreen(onMessage = notify) }
            composable(TASKS_ROUTE) {
                TasksScreen(
                    onMessage = notify,
                    focusTaskId = openRequest?.taskId,
                    focusToken = openRequest?.token,
                    onFocusHandled = taskOpen::consume,
                )
            }
            composable("history") { HistoryScreen(store = store, onMessage = notify) }
            composable("music") {
                MusicScreen(
                    jobModel = musicJobs,
                    searchModel = musicSearch,
                    exportModel = musicExport,
                    onMessage = notify,
                )
            }
            composable("follow") { FollowScreen(onMessage = notify) }
            composable("settings") { SettingsScreenView(store = store, onMessage = notify) }
        }
    }
}
