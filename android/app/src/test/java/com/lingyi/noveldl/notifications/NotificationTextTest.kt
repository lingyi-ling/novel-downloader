package com.lingyi.noveldl.notifications

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Task titles are remote, untrusted text. These tests pin the three
 * protections the notifier relies on: a single line, no leaked credentials and
 * a bounded length that does not split surrogate pairs.
 */
class NotificationTextTest {

    private val max = NotificationText.MAX_TITLE_CHARS

    @Test
    fun collapsesWhitespaceIntoOneLine() {
        assertEquals(
            "斗破 苍穹 第 1 章",
            NotificationText.taskTitle("  斗破\t苍穹\n第 1 章  ", "fallback"),
        )
    }

    @Test
    fun dropsBidirectionalAndZeroWidthMarks() {
        // U+202E reverses rendering, U+200B hides characters; neither should
        // survive into a notification the user reads.
        assertEquals(
            "titleevil",
            NotificationText.taskTitle("title\u202Eevil\u200B", "fallback"),
        )
    }

    @Test
    fun redactsUrlUserInfoAndSecretQueryParameters() {
        val title = NotificationText.taskTitle(
            "下载 https://user:pass@example.com/book?id=1&token=abc123&x=2",
            "fallback",
        )
        assertFalse(title, title.contains("user:pass"))
        assertFalse(title, title.contains("abc123"))
        assertTrue(title, title.contains("https://example.com/book?id=1&token=***&x=2"))
    }

    @Test
    fun redactsKeyValueAssignments() {
        val title = NotificationText.taskTitle(
            "失败 token=SECRET123 password: hunter2 api_key=xyz",
            "fallback",
        )
        assertFalse(title, title.contains("SECRET123"))
        assertFalse(title, title.contains("hunter2"))
        assertFalse(title, title.contains("xyz"))
        assertTrue(title, title.contains("token=***"))
        assertTrue(title, title.contains("password=***"))
    }

    @Test
    fun redactsSchemeCredentialsAndKnownTokenShapes() {
        val bearer = NotificationText.taskTitle(
            "Authorization: Bearer abcdefgh12345678",
            "fallback",
        )
        assertFalse(bearer, bearer.contains("abcdefgh12345678"))

        val github = NotificationText.taskTitle(
            "token ghp_0123456789abcdefghijABCDEFGHIJ",
            "fallback",
        )
        assertFalse(github, github.contains("ghp_0123456789"))
        assertTrue(github, github.contains("***"))
    }

    @Test
    fun keepsOrdinaryTitlesIntact() {
        assertEquals("三体（全集） 😀", NotificationText.taskTitle("三体（全集） 😀", "fallback"))
    }

    @Test
    fun fallsBackForBlankOrControlOnlyTitles() {
        val fallback = "未命名任务"
        assertEquals(fallback, NotificationText.taskTitle("", fallback))
        assertEquals(fallback, NotificationText.taskTitle(" \n\t\u200B ", fallback))
        assertEquals(fallback, NotificationText.taskTitle("\u202A\u202C", fallback))
    }

    @Test
    fun capsLongTitlesWithAnEllipsis() {
        val title = NotificationText.taskTitle("x".repeat(max * 3), "fallback")
        assertEquals(max, title.length)
        assertTrue(title.endsWith("\u2026"))
        assertEquals(max - 1, title.dropLast(1).length)
    }

    @Test
    fun neverSplitsASurrogatePair() {
        // The cut falls inside the emoji unless truncate backs off one char.
        val emoji = "\uD83D\uDE00"
        val raw = "a".repeat(max - 2) + emoji + "b"
        val title = NotificationText.taskTitle(raw, "fallback")
        assertTrue(title.length <= max)
        assertTrue(title.endsWith("\u2026"))
        assertFalse("lone surrogate in $title", hasLoneSurrogate(title))
    }

    private fun hasLoneSurrogate(text: String): Boolean {
        var index = 0
        while (index < text.length) {
            val char = text[index]
            when {
                char.isHighSurrogate() -> {
                    if (index + 1 >= text.length || !text[index + 1].isLowSurrogate()) return true
                    index += 2
                }
                char.isLowSurrogate() -> return true
                else -> index += 1
            }
        }
        return false
    }
}
