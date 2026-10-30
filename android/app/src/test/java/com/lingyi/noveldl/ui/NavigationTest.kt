package com.lingyi.noveldl.ui

import com.lingyi.noveldl.R
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * JVM tests for the bottom navigation model: five entries, stable order and
 * the follow tab wired to its label resource.
 */
class NavigationTest {

    @Test
    fun bottomBarHasFiveItems() {
        assertEquals(5, navItems.size)
    }

    @Test
    fun routesKeepTheirOrderAndFollowIsPresent() {
        assertEquals(
            listOf("search", "tasks", "history", "music", "follow"),
            navItems.map { it.route },
        )
    }

    @Test
    fun followTabUsesTheFollowLabelAndAnIcon() {
        val follow = navItems.last { it.route == "follow" }
        assertEquals(R.string.nav_follow, follow.label)
        assertTrue(follow.icon.name.isNotEmpty())
    }

    @Test
    fun everyRouteIsUniqueAndLabelled() {
        assertEquals(navItems.size, navItems.map { it.route }.distinct().size)
        navItems.forEach { item ->
            assertTrue("missing icon for ${item.route}", item.icon.name.isNotEmpty())
        }
    }
}
