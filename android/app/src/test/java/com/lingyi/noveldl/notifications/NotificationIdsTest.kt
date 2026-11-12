package com.lingyi.noveldl.notifications

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Both the notification id and its PendingIntent request code come from
 * [NotificationIds]. Stability keeps an update from stacking, uniqueness keeps
 * two tasks from overwriting each other.
 */
class NotificationIdsTest {

    @Test
    fun stableIdIsDeterministicAcrossInstances() {
        repeat(2_000) { index ->
            val key = "task-$index"
            assertEquals(
                NotificationIds.stableTaskId(key),
                NotificationIds.stableTaskId(key),
            )
        }
    }

    @Test
    fun stableIdStaysInReservedRange() {
        repeat(5_000) { index ->
            val id = NotificationIds.stableTaskId("site/book/$index")
            assertTrue("id $id below floor", id >= NotificationIds.TASK_ID_FLOOR)
            assertTrue("id $id not positive", id > 0)
        }
    }

    @Test
    fun doesNotRelyOnStringHashCode() {
        // "Aa" and "BB" are the classic Java String.hashCode collision; using
        // that hash would give two unrelated tasks the same notification id.
        assertEquals("Aa".hashCode(), "BB".hashCode())
        assertNotEquals(NotificationIds.stableTaskId("Aa"), NotificationIds.stableTaskId("BB"))
    }

    @Test
    fun allocatorReturnsTheSameIdForTheSameTask() {
        val allocator = TaskNotificationIds()
        val first = allocator.idFor("site:1")
        assertEquals(first, allocator.idFor("site:1"))
        assertEquals(NotificationIds.stableTaskId("site:1"), first)
    }

    @Test
    fun allocatorNeverHandsOutADuplicate() {
        val allocator = TaskNotificationIds()
        val keys = (0 until 10_000).map { "site-${it % 7}/book-$it" }
        val ids = keys.map { allocator.idFor(it) }
        assertEquals("duplicate ids handed out", keys.size, ids.toSet().size)
        assertTrue(ids.all { it >= NotificationIds.TASK_ID_FLOOR })
    }

    @Test
    fun nextCandidateWrapsInsideTheReservedRange() {
        val top = Int.MAX_VALUE
        val next = NotificationIds.nextCandidate(top)
        assertTrue(next >= NotificationIds.TASK_ID_FLOOR)
        assertTrue(next < top)

        val floor = NotificationIds.TASK_ID_FLOOR
        assertEquals(floor + 1, NotificationIds.nextCandidate(floor))
    }
}
