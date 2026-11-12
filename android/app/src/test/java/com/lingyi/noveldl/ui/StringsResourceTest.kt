package com.lingyi.noveldl.ui

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.w3c.dom.Element
import java.io.File
import javax.xml.parsers.DocumentBuilderFactory

/**
 * Guards the two Android resource files against drifting apart.
 *
 * A string added to `values/` but not `values-en/` renders Chinese inside an
 * English UI (or vice versa), and a translation that renames the format
 * arguments crashes at runtime or prints a literal `%1$s`. Neither is caught
 * by the Kotlin compiler, so this plain JVM test compares the XML directly.
 */
class StringsResourceTest {

    private val chinese = readStrings("values")
    private val english = readStrings("values-en")

    @Test
    fun bothLocalesDefineTheSameKeys() {
        assertEquals("keys only in values/", emptySet<String>(), chinese.keys - english.keys)
        assertEquals("keys only in values-en/", emptySet<String>(), english.keys - chinese.keys)
    }

    @Test
    fun everyStringIsNonBlankInBothLocales() {
        val blank = (chinese + english).filterValues { it.isBlank() }.keys
        assertTrue("blank strings: $blank", blank.isEmpty())
    }

    @Test
    fun formatArgumentsMatchBetweenLocales() {
        chinese.keys.intersect(english.keys).forEach { key ->
            assertEquals(
                "format arguments differ for $key",
                placeholders(chinese.getValue(key)),
                placeholders(english.getValue(key)),
            )
        }
    }

    @Test
    fun itemActionLabelsInterpolateTheItemName() {
        // These carry the book/file name into content descriptions; a
        // translation that drops the placeholder would make every visible
        // label identical ("Remove", "Download") again.
        listOf(
            "search_add_task_named",
            "history_download_named",
            "follow_remove_named",
            "tasks_retry_named",
            "tasks_cancel_named",
            "tasks_detail_expand_named",
            "tasks_detail_collapse_named",
        ).forEach { key ->
            assertFalse("$key is missing", placeholders(chinese.getValue(key)).isEmpty())
            assertFalse("$key is missing its argument", placeholders(english.getValue(key)).isEmpty())
        }
    }

    @Test
    fun englishStringsContainNoChineseCharacters() {
        // A key copied from values/ with the translation left behind renders
        // Chinese inside an English UI; no English value may contain CJK.
        val leaked = english.filterValues { CJK.containsMatchIn(it) }.keys
        assertEquals("English strings contain Chinese: $leaked", emptySet<String>(), leaked)
    }

    /** Resolves `src/main/res` from the test worker's working directory. */
    private fun resDir(): File {
        val start = File(System.getProperty("user.dir")).absoluteFile
        var dir: File? = start
        while (dir != null) {
            listOf(
                File(dir, "src/main/res"),
                File(dir, "app/src/main/res"),
                File(dir, "android/app/src/main/res"),
            ).firstOrNull { File(it, "values/strings.xml").isFile }?.let { return it }
            dir = dir.parentFile
        }
        error("cannot locate app/src/main/res from $start")
    }

    private fun readStrings(directory: String): Map<String, String> {
        val file = File(resDir(), "$directory/strings.xml")
        assertTrue("missing ${file.path}", file.isFile)
        val factory = DocumentBuilderFactory.newInstance().apply {
            setFeature("http://apache.org/xml/features/disallow-doctype-decl", true)
        }
        val nodes = factory.newDocumentBuilder().parse(file).getElementsByTagName("string")
        return buildMap {
            for (index in 0 until nodes.length) {
                val element = nodes.item(index) as Element
                put(element.getAttribute("name"), element.textContent.orEmpty())
            }
        }
    }

    /** Format specifiers such as `%1$s` / `%2$d`, sorted for comparison. */
    private fun placeholders(text: String): List<String> =
        PLACEHOLDER.findAll(text).map { it.value }.sorted().toList()

    private companion object {
        val PLACEHOLDER = Regex("""%(\d+\$)?[@a-zA-Z]""")
        val CJK = Regex("""[\u4e00-\u9fff]""")
    }
}
