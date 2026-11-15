package com.lingyi.noveldl.data

import java.io.File
import java.nio.file.Files
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * Persistence contract for the recent-search list:
 *
 * - a recorded term survives a fresh instance (process death);
 * - a repeated term is de-duplicated and moves to the front;
 * - the list is ordered newest first and capped;
 * - a corrupt / oversized / newer-version file loads as empty instead of
 *   crashing the search screen, and the next write repairs it;
 * - single-term deletion and clear-all work on disk.
 */
class SearchHistoryStoreTest {

    private lateinit var dir: File
    private lateinit var file: File

    @Before
    fun setUp() {
        dir = Files.createTempDirectory("search-history-store").toFile()
        file = File(dir, SEARCH_HISTORY_FILE)
    }

    @After
    fun tearDown() {
        dir.deleteRecursively()
    }

    private fun store(maxEntries: Int = MAX_SEARCH_HISTORY_ENTRIES) =
        SearchHistoryStore(file, maxEntries)

    private fun record(store: SearchHistoryStore, query: String, at: Long): List<SearchHistoryEntry> =
        runBlocking { store.record(query, at) }

    private fun queries(entries: List<SearchHistoryEntry>): List<String> =
        entries.map { it.query }

    // ---------- save / read ----------

    @Test
    fun recordedTermIsReadBackByAFreshInstance() {
        record(store(), "三体", 1_000L)

        // A new instance (new process) sees the same term.
        assertEquals(listOf("三体"), queries(runBlocking { store().snapshot() }))
        assertEquals(1_000L, runBlocking { store().snapshot() }.single().searchedAt)
    }

    @Test
    fun newestTermComesFirst() {
        val store = store()
        record(store, "first", 1_000L)
        record(store, "second", 2_000L)
        record(store, "third", 3_000L)

        assertEquals(
            listOf("third", "second", "first"),
            queries(runBlocking { store.snapshot() }),
        )
    }

    // ---------- de-duplication / normalisation ----------

    @Test
    fun repeatedTermIsDeduplicatedAndMovesToTheFront() {
        val store = store()
        record(store, "三体", 1_000L)
        record(store, "凡人修仙传", 2_000L)
        val updated = record(store, "三体", 3_000L)

        assertEquals(listOf("三体", "凡人修仙传"), queries(updated))
        assertEquals(2, runBlocking { store.snapshot() }.size)
    }

    @Test
    fun deduplicationIgnoresCaseAndSurroundingWhitespace() {
        val store = store()
        record(store, "  Harry   Potter ", 1_000L)
        val updated = record(store, "harry potter", 2_000L)

        assertEquals(1, updated.size)
        // The most recent spelling wins.
        assertEquals("harry potter", updated.single().query)
        assertEquals(2_000L, updated.single().searchedAt)
    }

    @Test
    fun blankTermIsIgnoredAndNotPersisted() {
        record(store(), "   \t  ", 1_000L)

        assertTrue(runBlocking { store().snapshot() }.isEmpty())
        assertFalse("a blank term must not create a file", file.exists())
    }

    @Test
    fun overlongTermIsTruncatedToTheLimit() {
        val query = "x".repeat(MAX_SEARCH_HISTORY_QUERY_CHARS + 100)
        val updated = record(store(), query, 1_000L)

        assertEquals(MAX_SEARCH_HISTORY_QUERY_CHARS, updated.single().query.length)
    }

    // ---------- bounds ----------

    @Test
    fun historyIsCappedAndTheOldestTermIsEvicted() {
        val store = store(maxEntries = 3)
        record(store, "one", 1_000L)
        record(store, "two", 2_000L)
        record(store, "three", 3_000L)
        val updated = record(store, "four", 4_000L)

        assertEquals(listOf("four", "three", "two"), queries(updated))
        assertEquals(listOf("four", "three", "two"), queries(runBlocking { store().snapshot() }))
    }

    @Test
    fun defaultCapKeepsOnlyTheTenMostRecentTerms() {
        val store = store()
        repeat(12) { index ->
            record(store, "term-$index", 1_000L + index)
        }

        val snapshot = runBlocking { store().snapshot() }
        assertEquals(MAX_SEARCH_HISTORY_ENTRIES, snapshot.size)
        assertEquals(1_011L, snapshot.first().searchedAt)
        assertEquals("term-11", snapshot.first().query)
        assertEquals("term-2", snapshot.last().query)
    }

    // ---------- delete / clear ----------

    @Test
    fun removeDropsOnlyTheGivenTerm() {
        val store = store()
        record(store, "三体", 1_000L)
        record(store, "球状闪电", 2_000L)

        val updated = runBlocking { store.remove("三体") }

        assertEquals(listOf("球状闪电"), queries(updated))
        assertEquals(listOf("球状闪电"), queries(runBlocking { store().snapshot() }))
    }

    @Test
    fun clearEmptiesTheStoredHistory() {
        val store = store()
        record(store, "三体", 1_000L)
        record(store, "球状闪电", 2_000L)

        assertTrue(runBlocking { store.clear() }.isEmpty())
        assertTrue(runBlocking { store().snapshot() }.isEmpty())
    }

    // ---------- corruption / validation ----------

    @Test
    fun corruptFileLoadsAsEmptyAndIsReplacedOnTheNextWrite() {
        file.writeText("""{"version":1,"entries":[ this is not json""")

        assertTrue(runBlocking { store().snapshot() }.isEmpty())

        record(store(), "三体", 1_000L)

        assertEquals(listOf("三体"), queries(runBlocking { store().snapshot() }))
    }

    @Test
    fun newerSchemaVersionLoadsAsEmptyInsteadOfGuessing() {
        file.writeText("""{"version":99,"entries":[{"query":"三体","searched_at":1}]}""")

        assertTrue(runBlocking { store().snapshot() }.isEmpty())
    }

    @Test
    fun malformedEntriesAreSkippedIndividually() {
        file.writeText(
            """{"version":1,"entries":[""" +
                """{"query":"","searched_at":1},""" +
                """{"query":{"nested":true},"searched_at":2},""" +
                """{"query":"valid","searched_at":3}""" +
                """]}""",
        )

        assertEquals(listOf("valid"), queries(runBlocking { store().snapshot() }))
    }

    @Test
    fun bareArrayFromAHandWrittenFileIsTolerated() {
        file.writeText("""[{"query":"三体","searched_at":1}]""")

        assertEquals(listOf("三体"), queries(runBlocking { store().snapshot() }))
    }

    @Test
    fun oversizedFileLoadsAsEmptyAndIsReplacedOnTheNextWrite() {
        file.writeText("x".repeat(MAX_SEARCH_HISTORY_FILE_BYTES.toInt() + 1))

        assertTrue(runBlocking { store().snapshot() }.isEmpty())

        record(store(), "三体", 1_000L)

        assertEquals(listOf("三体"), queries(runBlocking { store().snapshot() }))
    }

    @Test
    fun writeLeavesNoTemporaryFilesBehind() {
        record(store(), "三体", 1_000L)

        val names = dir.list()?.toList().orEmpty()
        assertEquals(listOf(SEARCH_HISTORY_FILE), names)
    }
}