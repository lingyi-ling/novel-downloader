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
 * Persistence contract for the `provider + id -> path` cache that lets a
 * playlist be exported after the app restarts:
 *
 * - a write is readable by a fresh instance (i.e. survives process death);
 * - re-downloading a track replaces its entry instead of duplicating it;
 * - entries for deleted device-local files are skipped/pruned;
 * - a corrupt file loads as empty instead of crashing the music screen;
 * - the map is bounded and the oldest write is evicted first.
 */
class MusicPathStoreTest {

    private lateinit var dir: File
    private lateinit var file: File

    private val trackA = "netease\u00001"
    private val trackB = "qq\u00002"

    @Before
    fun setUp() {
        dir = Files.createTempDirectory("music-path-store").toFile()
        file = File(dir, MUSIC_TRACK_PATHS_FILE)
    }

    @After
    fun tearDown() {
        dir.deleteRecursively()
    }

    private fun store(
        roots: List<String> = emptyList(),
        exists: (String) -> Boolean = { File(it).isFile },
        maxEntries: Int = MUSIC_TRACK_PATHS_MAX_ENTRIES,
    ) = MusicPathStore(file, roots, exists, maxEntries)

    /** Creates a real file and returns its absolute path. */
    private fun media(name: String): String =
        File(dir, name).apply { writeText("audio") }.absolutePath

    private fun missing(name: String): String = File(dir, name).absolutePath

    // ---------- save / read ----------

    @Test
    fun savedPathsAreReadBackByAFreshInstance() {
        val path = media("a.mp3")

        runBlocking { store().putAll(mapOf(trackA to path)) }

        // A new instance (new process) sees the same mapping.
        assertEquals(mapOf(trackA to path), runBlocking { store().snapshot() })
    }

    @Test
    fun writingTheSameTrackReplacesItsPath() {
        val first = media("first.mp3")
        val second = media("second.mp3")

        runBlocking {
            store().putAll(mapOf(trackA to first))
            store().putAll(mapOf(trackA to second))
        }

        val snapshot = runBlocking { store().snapshot() }
        assertEquals(mapOf(trackA to second), snapshot)
        assertEquals(1, snapshot.size)
    }

    @Test
    fun removeDropsOnlyTheGivenKeys() {
        val a = media("a.mp3")
        val b = media("b.mp3")
        runBlocking { store().putAll(mapOf(trackA to a, trackB to b)) }

        runBlocking { store().remove(listOf(trackA)) }

        assertEquals(mapOf(trackB to b), runBlocking { store().snapshot() })
    }

    // ---------- missing files ----------

    @Test
    fun localPathsWhoseFileIsAlreadyGoneAreNotStored() {
        val present = media("present.mp3")
        val gone = missing("gone.mp3")
        val store = store(roots = listOf(dir.absolutePath))

        runBlocking {
            store.putAll(mapOf(trackA to present, trackB to gone))
            assertEquals(mapOf(trackA to present), store.snapshot())
        }
        // The discarded entry is not written to disk either.
        assertEquals(
            mapOf(trackA to present),
            runBlocking { store(roots = listOf(dir.absolutePath)).snapshot() },
        )
    }

    @Test
    fun deletingASavedFileDropsItsEntryOnTheNextRead() {
        val keep = media("keep.mp3")
        val drop = File(dir, "drop.mp3").apply { writeText("audio") }
        val roots = listOf(dir.absolutePath)
        runBlocking { store(roots).putAll(mapOf(trackA to keep, trackB to drop.absolutePath)) }

        assertTrue(drop.delete())

        val snapshot = runBlocking { store(roots).snapshot() }
        assertEquals(mapOf(trackA to keep), snapshot)
        // The cleanup is persisted, not just hidden for this read.
        assertEquals(
            mapOf(trackA to keep),
            runBlocking { store(roots).snapshot() },
        )
    }

    @Test
    fun serverSidePathsAreKeptWithoutALocalExistenceCheck() {
        // Remote mode: the engine wrote the file on the server machine; the
        // phone cannot stat it, so it must not be pruned.
        val store = store(
            roots = listOf(dir.absolutePath),
            exists = { false },
        )

        runBlocking { store.putAll(mapOf(trackA to "/srv/music/a.mp3")) }

        assertEquals(
            mapOf(trackA to "/srv/music/a.mp3"),
            runBlocking { store().snapshot() },
        )
    }

    // ---------- corruption / validation ----------

    @Test
    fun corruptJsonLoadsAsEmptyAndIsReplacedOnTheNextWrite() {
        file.writeText("""{"version":1,"entries":[ this is not json""")

        assertTrue(runBlocking { store().snapshot() }.isEmpty())

        val path = media("a.mp3")
        runBlocking { store().putAll(mapOf(trackA to path)) }

        assertEquals(mapOf(trackA to path), runBlocking { store().snapshot() })
    }

    @Test
    fun malformedEntriesAreSkippedIndividually() {
        // A hand-edited file: a bare id from an older build, a relative path
        // and one valid record. Only the valid record survives.
        file.writeText(
            """{"version":1,"entries":[""" +
                """{"key":"bare-id","path":"/a.mp3"},""" +
                """{"key":"netease\u00002","path":"relative.mp3"},""" +
                """{"key":"netease\u00003","path":"/ok.mp3"}""" +
                """]}""",
        )

        assertEquals(
            mapOf("netease\u00003" to "/ok.mp3"),
            runBlocking { store().snapshot() },
        )
    }

    @Test
    fun invalidIncomingEntriesAreIgnored() {
        val path = media("a.mp3")

        runBlocking {
            store().putAll(
                mapOf(
                    "bare-id" to path,
                    trackA to "relative.mp3",
                    trackB to path,
                ),
            )
        }

        assertEquals(mapOf(trackB to path), runBlocking { store().snapshot() })
    }

    @Test
    fun aNewerSchemaVersionLoadsAsEmptyInsteadOfGuessing() {
        file.writeText("""{"version":99,"entries":[{"key":"netease\u00001","path":"/a.mp3"}]}""")

        assertTrue(runBlocking { store().snapshot() }.isEmpty())
    }

    // ---------- bounds ----------

    @Test
    fun entryCountIsCappedAndTheOldestWriteIsEvicted() {
        val paths = (1..5).associate { index ->
            "netease\u0000$index" to media("$index.mp3")
        }

        runBlocking { store(maxEntries = 3).putAll(paths) }

        val snapshot = runBlocking { store().snapshot() }
        assertEquals(3, snapshot.size)
        assertFalse(snapshot.containsKey("netease\u00001"))
        assertFalse(snapshot.containsKey("netease\u00002"))
        assertTrue(snapshot.containsKey("netease\u00005"))
    }

    @Test
    fun noOpPersistenceWritesNothing() {
        runBlocking {
            MusicPathPersistence.None.putAll(mapOf(trackA to media("a.mp3")))
            assertTrue(MusicPathPersistence.None.snapshot().isEmpty())
        }
        assertFalse(file.exists())
    }
}

/**
 * Lexical containment rules used to decide whether a path is a device-local
 * file (so it can be existence-checked) or an engine/server-side path.
 */
class MusicLocalPathsTest {

    @Test
    fun insideAcceptsDescendantsOnly() {
        assertTrue(MusicLocalPaths.isInside("/data/app/files", "/data/app/files/a.mp3"))
        assertTrue(MusicLocalPaths.isInside("/data/app/files/", "/data/app/files/sub/a.mp3"))
        assertFalse(MusicLocalPaths.isInside("/data/app/files", "/data/app/files2/a.mp3"))
        assertFalse(MusicLocalPaths.isInside("/data/app/files", "/data/app/other/a.mp3"))
    }

    @Test
    fun parentTraversalCannotEscapeTheRoot() {
        assertFalse(
            MusicLocalPaths.isInside(
                "/data/app/files",
                "/data/app/files/../../secret.mp3",
            ),
        )
        assertTrue(MusicLocalPaths.isInside("/data/app/files", "/data/app/files/./a.mp3"))
    }

    @Test
    fun windowsSeparatorsAndDriveLettersAreNormalised() {
        assertTrue(
            MusicLocalPaths.isInside(
                "C:\\Users\\me\\Music",
                "c:\\Users\\me\\Music\\a.mp3",
            ),
        )
        assertFalse(
            MusicLocalPaths.isInside(
                "C:\\Users\\me\\Music",
                "C:\\Users\\me\\Music\\..\\secret.mp3",
            ),
        )
    }

    @Test
    fun onlyPathsUnderAKnownRootAreLocal() {
        val roots = listOf("/data/app/files")
        assertTrue(MusicLocalPaths.isLocal("/data/app/files/music/a.mp3", roots))
        assertFalse(MusicLocalPaths.isLocal("/srv/music/a.mp3", roots))
        assertFalse(MusicLocalPaths.isLocal("/data/app/files", emptyList()))
    }
}
