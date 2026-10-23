package com.lingyi.noveldl.ui

import androidx.annotation.StringRes
import androidx.compose.foundation.layout.padding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.outlined.Download
import androidx.compose.material.icons.outlined.History
import androidx.compose.material.icons.outlined.LibraryMusic
import androidx.compose.material.icons.outlined.Search
import androidx.compose.material.icons.outlined.Settings
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
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.vector.ImageVector
import androidx.compose.ui.res.stringResource
import androidx.navigation.NavGraph.Companion.findStartDestination
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.currentBackStackEntryAsState
import androidx.navigation.compose.rememberNavController
import com.lingyi.noveldl.R
import com.lingyi.noveldl.data.SettingsStore
import com.lingyi.noveldl.ui.history.HistoryScreen
import com.lingyi.noveldl.ui.music.MusicScreen
import com.lingyi.noveldl.ui.search.SearchScreen
import com.lingyi.noveldl.ui.settings.SettingsScreenView
import com.lingyi.noveldl.ui.tasks.TasksScreen
import kotlinx.coroutines.launch

private data class NavItem(
    val route: String,
    @StringRes val label: Int,
    val icon: ImageVector,
)

private val navItems = listOf(
    NavItem("search", R.string.nav_search, Icons.Outlined.Search),
    NavItem("tasks", R.string.nav_tasks, Icons.Outlined.Download),
    NavItem("history", R.string.nav_history, Icons.Outlined.History),
    NavItem("music", R.string.nav_music, Icons.Outlined.LibraryMusic),
)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun NovelApp(store: SettingsStore) {
    val navController = rememberNavController()
    val snackbarHostState = remember { SnackbarHostState() }
    val scope = rememberCoroutineScope()
    val backStackEntry by navController.currentBackStackEntryAsState()
    val route = backStackEntry?.destination?.route ?: "search"

    val notify: (String) -> Unit = { message ->
        scope.launch { snackbarHostState.showSnackbar(message) }
    }

    val titleRes = when (route) {
        "tasks" -> R.string.nav_tasks
        "history" -> R.string.nav_history
        "music" -> R.string.nav_music
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
            composable("tasks") { TasksScreen(onMessage = notify) }
            composable("history") { HistoryScreen(onMessage = notify) }
            composable("music") { MusicScreen(onMessage = notify) }
            composable("settings") { SettingsScreenView(store = store, onMessage = notify) }
        }
    }
}
