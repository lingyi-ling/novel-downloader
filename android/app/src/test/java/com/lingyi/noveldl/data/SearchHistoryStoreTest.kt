package com.lingyi.noveldl.data

import java.io.File
import java.nio.file.Files
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.launch
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeout
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
 * - a corrupt / oversized / newer-version / unreadable file loads as empty
 *   instead of crashing the search screen, and the next write repairs it;
 * - missing fields and wrong-typed values are tolerated per-entry;
 * - a failed write leaves the previous file (or no file) intact and does not
 *   crash; [SearchHistoryStore.record] still returns the optimistic in-memory
 *   list, while [SearchHistoryStore.snapshot] keeps reporting what is on disk;
 * - the store is thread-safe: all writes go through one process-wide lock and
 *   replace the file via an atomic rename, so concurrent recording from
 *   several instances never duplicates entries or exposes a torn document.
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

    // ---------- missing fields / wrong types ----------

    @Test
    fun entryMissingQueryIsSkippedAndMissingTimestampBecomesZero() {
        file.writeText(
            """{"version":1,"entries":[""" +
                """{"searched_at":5},""" +
                """{"query":"kept"}""" +
                """]}""",
        )

        val entries = runBlocking { store().snapshot() }

        assertEquals(listOf("kept"), queries(entries))
        assertEquals(0L, entries.single().searchedAt)
    }

    @Test
    fun nonNumericTimestampFallsBackToZeroInsteadOfFailingTheEntry() {
        file.writeText(
            """{"version":1,"entries":[""" +
                """{"query":"text","searched_at":"not-a-number"},""" +
                """{"query":"negative","searched_at":-7}""" +
                """]}""",
        )

        val entries = runBlocking { store().snapshot() }

        // The text timestamp decodes to 0; the negative one is clamped to 0.
        assertEquals(listOf("text", "negative"), queries(entries))
        assertTrue(entries.all { it.searchedAt == 0L })
    }

    @Test
    fun numericQueryPrimitiveIsCoercedToItsTextForm() {
        // JsonPrimitive.contentOrNull is non-null for any primitive, so a
        // numerical query is read as text rather than discarding the entry.
        file.writeText("""{"version":1,"entries":[{"query":123,"searched_at":1}]}""")

        assertEquals(listOf("123"), queries(runBlocking { store().snapshot() }))
    }

    @Test
    fun missingOrWrongTypedContainersLoadAsEmpty() {
        val documents = listOf(
            """{}""",
            """{"version":1}""",
            """{"version":1,"entries":{}}""",
            """{"version":1,"entries":[]}""",
            """123""",
            """null""",
            """   """,
        )

        documents.forEach { document ->
            file.writeText(document)
            assertTrue(
                "document should decode to an empty list: $document",
                runBlocking { store().snapshot() }.isEmpty(),
            )
        }
    }

    @Test
    fun stringVersionIsParsedAndMissingVersionIsAccepted() {
        file.writeText("""{"version":"1","entries":[{"query":"a","searched_at":1}]}""")
        assertEquals(listOf("a"), queries(runBlocking { store().snapshot() }))

        file.writeText("""{"entries":[{"query":"b","searched_at":2}]}""")
        assertEquals(listOf("b"), queries(runBlocking { store().snapshot() }))
    }

    @Test
    fun jsonSpecialCharactersSurviveTheRoundTrip() {
        val query = "他说：\"你好\" \\ 路径 😀"

        record(store(), query, 1_000L)

        assertEquals(listOf(query), queries(runBlocking { store().snapshot() }))
    }

    @Test
    fun ioReadFailureFallsBackToAnEmptyList() {
        // A file that reports itself as a regular, readable-length file but
        // whose bytes cannot be opened (here: it does not exist) exercises the
        // readText failure path deterministically on every OS.
        val unreadable = object : File(dir, "unreadable.json") {
            override fun isFile(): Boolean = true

            override fun length(): Long = 64L
        }

        assertTrue(runBlocking { SearchHistoryStore(unreadable).snapshot() }.isEmpty())
    }

    @Test
    fun pathThatIsNotARegularFileLoadsAsEmptyWithoutCrashing() {
        val asDirectory = File(dir, "history-is-a-directory")
        assertTrue(asDirectory.mkdirs())

        assertTrue(runBlocking { SearchHistoryStore(asDirectory).snapshot() }.isEmpty())
    }

    // ---------- write failures ----------

    @Test
    fun failedAtomicMoveKeepsTheInMemoryResultButDoesNotPersist() {
        // The target is a non-empty directory, so the final rename can never
        // succeed on any platform. record() must still return the optimistic
        // list (its documented return value), while snapshot() keeps reading
        // the unchanged disk state.
        val target = File(dir, "history-as-directory")
        assertTrue(target.mkdirs())
        assertTrue(File(target, "occupied").createNewFile())

        val store = SearchHistoryStore(target)
        val updated = runBlocking { store.record("三体", 1_000L) }

        assertEquals(listOf("三体"), queries(updated))
        assertTrue(runBlocking { store.snapshot() }.isEmpty())
        assertTrue("target directory must be untouched", File(target, "occupied").isFile)
        assertEquals(
            "no temporary file may be left behind",
            listOf("history-as-directory"),
            dir.list()?.toList().orEmpty(),
        )
    }

    @Test
    fun unwritableParentKeepsTheInMemoryResultButDoesNotPersist() {
        // The parent path is a regular file, so mkdirs() fails and writeLocked
        // returns before creating anything.
        val parentBlocker = File(dir, "blocked-parent")
        parentBlocker.writeText("not a directory")
        val store = SearchHistoryStore(File(parentBlocker, SEARCH_HISTORY_FILE))

        val updated = runBlocking { store.record("三体", 1_000L) }

        assertEquals(listOf("三体"), queries(updated))
        assertTrue(runBlocking { store.snapshot() }.isEmpty())
        assertTrue(parentBlocker.isFile)
        assertEquals("not a directory", parentBlocker.readText())
    }

    @Test
    fun failedWriteDoesNotCorruptAnExistingHistory() {
        val target = File(dir, "history-as-directory")
        assertTrue(target.mkdirs())
        assertTrue(File(target, "occupied").createNewFile())

        // Seed a real, readable history next door, then point the store at the
        // unwritable target: the seeded file must be unaffected.
        val seeded = File(dir, "seed.json")
        seeded.writeText(SearchHistoryCodec.encode(listOf(SearchHistoryEntry("kept", 1L))))
        val broken = SearchHistoryStore(target)

        runBlocking { broken.record("lost", 2_000L) }

        assertEquals(
            listOf("kept"),
            queries(SearchHistoryCodec.decode(seeded.readText())),
        )
        assertTrue(runBlocking { broken.snapshot() }.isEmpty())
    }

    // ---------- concurrency ----------

    @Test
    fun concurrentRecordsFromOneStoreKeepEveryTermExactlyOnce() = runBlocking {
        val store = store(maxEntries = 100)
        val count = 24

        withTimeout(30_000L) {
            (0 until count).map { index ->
                async(Dispatchers.Default) {
                    store.record("term-$index", 1_000L + index)
                }
            }.awaitAll()
        }

        val snapshot = store.snapshot()
        assertEquals(count, snapshot.size)
        assertEquals(count, snapshot.map { it.query }.toSet().size)
    }

    @Test
    fun concurrentRecordsFromSeveralInstancesShareTheSameLock() = runBlocking {
        // Several store instances stand in for several screens/recompositions.
        val stores = List(4) { store(maxEntries = 100) }
        val count = 40

        withTimeout(30_000L) {
            (0 until count).map { index ->
                async(Dispatchers.Default) {
                    stores[index % stores.size].record("q-$index", 1_000L + index)
                }
            }.awaitAll()
        }

        val snapshot = stores.first().snapshot()
        assertEquals(count, snapshot.size)
        assertEquals(count, snapshot.map { it.query }.toSet().size)
    }

    @Test
    fun concurrentDuplicateRecordsCollapseToOneEntry() = runBlocking {
        val store = store()

        withTimeout(30_000L) {
            (0 until 32).map { index ->
                async(Dispatchers.Default) {
                    store.record("same", 1_000L + index)
                }
            }.awaitAll()
        }

        val entries = store.snapshot()
        assertEquals(1, entries.size)
        assertEquals("same", entries.single().query)
        assertEquals(1_031L, entries.single().searchedAt)
    }

    @Test
    fun concurrentWritesNeverExposeATornDocument() = runBlocking {
        val store = store(maxEntries = 100)
        store.record("seed", 0L)
        val torn = mutableListOf<String>()

        val writer = launch(Dispatchers.Default) {
            repeat(100) { index -> store.record("term-$index", 1_000L + index) }
        }
        val reader = launch(Dispatchers.Default) {
            repeat(400) {
                val raw = runCatching { file.readText(Charsets.UTF_8) }.getOrNull()
                // A missing file (between renames) is fine; a present file must
                // always be a complete document.
                if (raw != null && SearchHistoryCodec.decode(raw).isEmpty()) {
                    torn += raw
                }
            }
        }
        writer.join()
        reader.join()

        assertTrue("saw a torn document: ${torn.firstOrNull()}", torn.isEmpty())
        // On Windows a reader holding the old file open can make a rename
        // fail (the store then skips that one write), so the exact count is
        // not asserted here; the other concurrency tests cover all writes
        // landing. What matters is that every visible state is a complete,
        // duplicate-free document and no temp file leaks.
        val snapshot = store.snapshot()
        assertTrue(snapshot.isNotEmpty())
        assertTrue(snapshot.size <= 100)
        assertEquals(snapshot.size, snapshot.map { it.query }.toSet().size)
        assertTrue(snapshot.all { it.query.isNotBlank() })
        assertEquals(listOf(SEARCH_HISTORY_FILE), dir.list()?.toList().orEmpty())
    }
}