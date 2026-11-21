#!/usr/bin/env python3
"""Static audit of the Android notification deep-link / Intent-extra surface.

``MainActivity`` is exported because it is the launcher entry point. Any
installed app can therefore start it explicitly (``component =
com.lingyi.noveldl/.MainActivity``) with an arbitrary action, extras and URI.
The only thing the activity may do with that intent is local navigation to the
task list; everything else in this module checks that the intent surface cannot
be widened into a file read, a download, a cancel or a share.

The checks read the real Kotlin/XML files a release build packages, with the
standard library only: no Gradle, no Android SDK, no device. The Kotlin
counterpart ``TaskOpenValidationTest`` asserts the same invariants from the
Gradle unit-test gate.

Audited invariants:

* the accepted ``OPEN_TASKS`` action is an allow-list (canonical decimal
  notification id), not a bare prefix match;
* the task id extra is length- and character-bounded before it is used, on both
  the posting side (``TaskNotifier``) and the consuming side (``TaskOpen``);
* the delivered intent's data/clipData is never read;
* every PendingIntent is immutable + updatable, with per-task request codes;
* the ``ACTION_DOWNLOAD_COMPLETE`` receiver is registered
  ``RECEIVER_NOT_EXPORTED`` and only acts on ids this screen enqueued;
* the notification body is built from redacted, length-limited text.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "android" / "app" / "src" / "main"
MANIFEST = MAIN / "AndroidManifest.xml"
JAVA = MAIN / "java" / "com" / "lingyi" / "noveldl"
NOTIFICATIONS = JAVA / "notifications"
ACTIVITY = JAVA / "MainActivity.kt"
HISTORY = JAVA / "ui" / "history" / "HistoryScreen.kt"
TASK_OPEN = NOTIFICATIONS / "TaskOpen.kt"
TASK_NOTIFIER = NOTIFICATIONS / "TaskNotifier.kt"

ANDROID_NS = "{http://schemas.android.com/apk/res/android}"


def read(path: Path) -> str:
    """Read a UTF-8 source file."""
    return path.read_text(encoding="utf-8")


def body(source: str, signature: str) -> str:
    """Text from [signature] to the next top-level closing brace."""
    start = source.index(signature)
    end = source.index("\n    }", start)
    return source[start:end]


def code_only(source: str) -> str:
    """Strip Kotlin line and block comments.

    Assertions about what the code *does* must not be satisfied or broken by
    prose in a comment; the comment that documents a rule usually names it.
    """
    without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"//[^\n]*", "", without_block)


# ----------------------------------------------------------------------
# Action allow-list
# ----------------------------------------------------------------------


def test_open_tasks_action_is_an_allow_list_not_a_prefix_match() -> None:
    """A forged suffix must not be accepted just because the prefix matches."""
    source = read(TASK_OPEN)
    check = body(source, "fun isOpenTasksAction(")

    # The suffix is validated as digits in the canonical range; a bare
    # startsWith() would accept "OPEN_TASKS:anything".
    assert "suffix.all { it in '0'..'9' }" in check
    assert "MAX_ACTION_ID_DIGITS" in check
    # Canonical decimal: no zero padding, no sign.
    assert "suffix[0] == '0'" in check
    assert "in 1..Int.MAX_VALUE.toLong()" in check


def test_the_activity_forwards_only_action_and_task_id() -> None:
    """No other extra, no URI and no component is read from the intent."""
    source = read(ACTIVITY)
    assert "getStringExtra(TaskOpen.EXTRA_TASK_ID)" in source
    assert "TaskOpen.shouldHandleDelivery(" in source

    code = code_only(source)
    for forbidden in (
        "getData()",
        "intent.data",
        "intent?.data",
        "clipData",
        "getParcelableExtra",
        "getSerializableExtra",
        "getBundleExtra",
        "openInputStream",
        "contentResolver",
        "DocumentFile",
    ):
        assert forbidden not in code, (
            f"MainActivity reads {forbidden!r} from its intent"
        )


def test_a_forged_intent_cannot_reach_cancel_download_or_share() -> None:
    """The intent path has no side effect beyond navigation."""
    code = code_only(read(ACTIVITY))
    for forbidden in (
        "cancelTask",
        "ACTION_SEND",
        "ACTION_DELETE",
        "ACTION_INSTALL_PACKAGE",
        "startService",
        "sendBroadcast",
        "DownloadManager",
    ):
        assert forbidden not in code, f"intent handling reached {forbidden!r}"


# ----------------------------------------------------------------------
# Task id validation
# ----------------------------------------------------------------------


def test_task_id_is_bounded_and_character_checked_on_both_sides() -> None:
    """The remote id is validated where it is posted and where it is consumed."""
    task_open = read(TASK_OPEN)
    normalize = body(task_open, "fun taskId(")

    assert "MAX_TASK_ID_CHARS" in normalize
    assert "trimmed.length > MAX_TASK_ID_CHARS" in normalize
    assert "isISOControl" in normalize
    assert "Character.FORMAT" in normalize
    assert "isWhitespace" in normalize

    # A bounded constant, not an inline magic number that could drift.
    limit = re.search(r"const val MAX_TASK_ID_CHARS: Int = (\d+)", task_open)
    assert limit is not None
    assert 32 <= int(limit.group(1)) <= 1024, (
        "the limit must fit a uuid4().hex id and stay small enough for "
        "saved instance state"
    )

    # The notifier posts the validated form, never the raw server string.
    notifier = read(TASK_NOTIFIER)
    assert "val taskIdExtra = TaskOpen.taskId(event.task.task_id).orEmpty()" in notifier
    assert "putExtra(TaskOpen.EXTRA_TASK_ID, taskIdExtra)" in notifier
    assert "putExtra(TaskOpen.EXTRA_TASK_ID, event.task.task_id" not in notifier


def test_an_unknown_id_is_reported_missing_not_resolved() -> None:
    """The id is only compared with the task list, never used as a path."""
    actions = read(JAVA / "ui" / "tasks" / "TaskActions.kt")
    decision = body(actions, "fun taskFocusDecision(")

    assert "TaskFocusDecision.NoTaskId" in decision
    assert "TaskFocusDecision.Missing(id)" in decision
    assert "TaskFocusDecision.Reveal(id, row)" in decision
    # Reveal is keyed by a row index computed from the listed ids.
    assert "taskRowIndex(" in decision


# ----------------------------------------------------------------------
# PendingIntent flags and request codes
# ----------------------------------------------------------------------


def test_every_pending_intent_is_immutable_and_updatable() -> None:
    source = read(TASK_NOTIFIER)
    calls = source.count("PendingIntent.getActivity(")
    assert calls == 2, f"expected the per-task and summary intents, found {calls}"
    assert source.count("PendingIntent.FLAG_IMMUTABLE") == calls
    assert source.count("PendingIntent.FLAG_UPDATE_CURRENT") == calls
    assert "FLAG_MUTABLE" not in source


def test_request_codes_are_unique_per_task_and_per_summary() -> None:
    source = read(TASK_NOTIFIER)
    assert re.search(
        r"PendingIntent\.getActivity\(\s*appContext,\s*notificationId,", source
    )
    assert re.search(r"PendingIntent\.getActivity\(\s*context,\s*SUMMARY_ID,", source)

    ids = read(NOTIFICATIONS / "NotificationIds.kt")
    floor = re.search(r"const val TASK_ID_FLOOR: Int = ([\d_]+)", ids)
    summary = re.search(r"const val SUMMARY_ID: Int = ([\d_]+)", ids)
    assert floor is not None and summary is not None
    assert int(summary.group(1).replace("_", "")) < int(floor.group(1).replace("_", ""))
    # The allocator never hands out a duplicate within a process.
    assert "while (!used.add(candidate))" in ids


# ----------------------------------------------------------------------
# Broadcast receiver
# ----------------------------------------------------------------------


def test_download_receiver_is_not_exported() -> None:
    source = read(HISTORY)
    assert "ContextCompat.RECEIVER_NOT_EXPORTED" in source
    assert "RECEIVER_EXPORTED" not in source
    # The only registration goes through the ContextCompat overload that takes
    # an explicit export flag; the two-argument form defaults to exported on
    # some API levels and must not appear.
    code = code_only(source)
    registrations = re.findall(r"(\w+)\.registerReceiver\(", code)
    assert registrations == ["ContextCompat"], registrations
    assert "DownloadManager.EXTRA_DOWNLOAD_ID" in source
    assert "pendingDownloads.remove(id) ?: return" in source


def test_manifest_still_declares_no_receiver() -> None:
    """The only receiver is registered in-process, so none is exported in XML."""
    application = ET.parse(MANIFEST).getroot().find("application")
    assert application is not None
    assert application.findall("receiver") == []


# ----------------------------------------------------------------------
# Notification content
# ----------------------------------------------------------------------


def test_notification_text_is_redacted_and_length_limited() -> None:
    source = read(NOTIFICATIONS / "NotificationText.kt")
    assert "fun taskTitle(" in source
    assert "redact(" in source
    assert "truncate(" in source
    assert "MAX_TITLE_CHARS" in source
    # The notifier must route the remote title through it.
    notifier = read(TASK_NOTIFIER)
    assert "NotificationText.taskTitle(event.task.title, fallbackTitle)" in notifier
    # No raw title is placed in the notification body.
    assert "setContentText(event.task.title)" not in notifier
    assert "setContentTitle(event.task.title)" not in notifier


def test_the_task_id_extra_is_not_rendered_in_the_notification() -> None:
    """The id is a navigation key only; the body carries the title."""
    notifier = read(TASK_NOTIFIER)
    body_start = notifier.index("val notification = NotificationCompat.Builder")
    builder = notifier[body_start:]
    assert "task_id" not in builder
