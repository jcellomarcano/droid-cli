"""Tests puros para droid.salud: parseo de crashes, ANRs y eventos de proceso."""
from droid import salud
from droid.inspector import ExitRecord
from tests.helpers import fixture_text


# ----------------------------------------------------------------------------- Java crash (fixture real)


def test_java_crash_real_fixture_parses():
    raw = fixture_text("logcat_crash")
    blocks = salud.split_crash_blocks(raw)
    assert len(blocks) == 1
    rec = salud.parse_crash_block(blocks[0])
    assert rec is not None
    assert rec.kind == "java"
    assert rec.exc_type == "android.app.RemoteServiceException$CrashedByAdbException"
    assert rec.message == "shell-induced crash"
    assert rec.pid == 10362
    assert len(rec.frames) >= 8
    assert rec.frames[0].startswith("android.app.ActivityThread.throwRemoteServiceException")


def test_java_crash_sig_stable_across_two_parses():
    raw = fixture_text("logcat_crash")
    blocks = salud.split_crash_blocks(raw)
    rec1 = salud.parse_crash_block(blocks[0])
    blocks2 = salud.split_crash_blocks(raw)
    rec2 = salud.parse_crash_block(blocks2[0])
    assert rec1.sig == rec2.sig
    assert rec1.sig != ""


def test_java_crash_line_number_invariance():
    block_a = [
        "2026-09-15 10:00:00.000 100 100 E AndroidRuntime: FATAL EXCEPTION: main",
        "2026-09-15 10:00:00.000 100 100 E AndroidRuntime: Process: com.example.app, PID: 100",
        "2026-09-15 10:00:00.000 100 100 E AndroidRuntime: com.example.app.MyException: boom",
        "2026-09-15 10:00:00.000 100 100 E AndroidRuntime: \tat com.example.app.Foo.bar(Foo.java:10)",
        "2026-09-15 10:00:00.000 100 100 E AndroidRuntime: \tat com.example.app.Baz.qux(Baz.java:20)",
    ]
    block_b = [
        "2026-09-15 11:00:00.000 200 200 E AndroidRuntime: FATAL EXCEPTION: main",
        "2026-09-15 11:00:00.000 200 200 E AndroidRuntime: Process: com.example.app, PID: 200",
        "2026-09-15 11:00:00.000 200 200 E AndroidRuntime: com.example.app.MyException: boom, different line",
        "2026-09-15 11:00:00.000 200 200 E AndroidRuntime: \tat com.example.app.Foo.bar(Foo.java:999)",
        "2026-09-15 11:00:00.000 200 200 E AndroidRuntime: \tat com.example.app.Baz.qux(Baz.java:1)",
    ]
    rec_a = salud.parse_crash_block(block_a)
    rec_b = salud.parse_crash_block(block_b)
    assert rec_a is not None and rec_b is not None
    assert rec_a.sig == rec_b.sig


def test_java_crash_different_top_frame_different_sig():
    block_a = [
        "2026-09-15 10:00:00.000 100 100 E AndroidRuntime: FATAL EXCEPTION: main",
        "2026-09-15 10:00:00.000 100 100 E AndroidRuntime: Process: com.example.app, PID: 100",
        "2026-09-15 10:00:00.000 100 100 E AndroidRuntime: com.example.app.MyException: boom",
        "2026-09-15 10:00:00.000 100 100 E AndroidRuntime: \tat com.example.app.Foo.bar(Foo.java:10)",
    ]
    block_b = [
        "2026-09-15 10:00:00.000 100 100 E AndroidRuntime: FATAL EXCEPTION: main",
        "2026-09-15 10:00:00.000 100 100 E AndroidRuntime: Process: com.example.app, PID: 100",
        "2026-09-15 10:00:00.000 100 100 E AndroidRuntime: com.example.app.MyException: boom",
        "2026-09-15 10:00:00.000 100 100 E AndroidRuntime: \tat com.example.app.Other.method(Other.java:5)",
    ]
    rec_a = salud.parse_crash_block(block_a)
    rec_b = salud.parse_crash_block(block_b)
    assert rec_a.sig != rec_b.sig


def test_normalize_frames_app_prefix_kept():
    frames = [
        "android.os.Looper.loop(Looper.java:397)",
        "com.example.app.Foo.bar(Foo.java:10)",
        "com.example.app.Baz.qux(Baz.java:20)",
    ]
    norm = salud.normalize_frames(frames, "java", "com.example.app")
    assert norm == ["com.example.app.Foo.bar", "com.example.app.Baz.qux"]


def test_normalize_frames_drops_framework_when_non_framework_present():
    frames = [
        "android.os.Looper.loop(Looper.java:397)",
        "com.other.Lib.call(Lib.java:1)",
    ]
    norm = salud.normalize_frames(frames, "java", None)
    assert norm == ["com.other.Lib.call"]


def test_normalize_frames_all_framework_falls_back_to_top_frame():
    frames = [
        "android.os.Looper.loop(Looper.java:397)",
        "java.lang.reflect.Method.invoke(Method.java:0)",
    ]
    norm = salud.normalize_frames(frames, "java", None)
    assert norm == ["android.os.Looper.loop"]


def test_obfuscated_detection():
    norm = ["a.b.c"]
    assert salud._is_obfuscated(norm)
    norm2 = ["com.example.app.Foo.bar"]
    assert not salud._is_obfuscated(norm2)


# ----------------------------------------------------------------------------- native crash (synthetic fixture)


def test_native_crash_symbolised_block():
    raw = fixture_text("logcat_crash_native")
    blocks = salud.split_crash_blocks(raw)
    assert len(blocks) == 2
    rec = salud.parse_crash_block(blocks[0])
    assert rec is not None
    assert rec.kind == "native"
    assert rec.exc_type == "SIGSEGV"
    assert rec.pid == 1230
    assert rec.tid == 1234
    assert rec.frames[0] == "libfoo.so!foo_symbol"
    assert rec.frames[1] == "libc.so!__libc_init"
    norm = salud.normalize_frames(rec.frames, "native", None)
    assert norm == rec.frames[:3]


def test_native_crash_lib_only_block():
    raw = fixture_text("logcat_crash_native")
    blocks = salud.split_crash_blocks(raw)
    rec = salud.parse_crash_block(blocks[1])
    assert rec is not None
    assert rec.exc_type == "SIGABRT"
    assert rec.pid == 1236
    assert rec.frames == ["libc.so"]


def test_native_crash_sig_differs_from_java():
    raw = fixture_text("logcat_crash_native")
    blocks = salud.split_crash_blocks(raw)
    rec1 = salud.parse_crash_block(blocks[0])
    rec2 = salud.parse_crash_block(blocks[1])
    assert rec1.sig != rec2.sig


# ----------------------------------------------------------------------------- ANR


def test_parse_anr_trace_main_frames():
    raw = fixture_text("anr_trace")
    rec = salud.parse_anr_trace(raw, "com.example.app")
    assert rec is not None
    assert rec.pid == 1234
    assert len(rec.main_frames) == 4
    assert rec.main_frames[0] == "com.example.app.MainActivity.onCreate(MainActivity.kt:42)"
    assert "Signal Catcher" not in "".join(rec.main_frames)


def test_parse_anr_logcat_pair():
    lines = [
        "2026-09-15 12:00:01.000  1000  1000 E ActivityManager: ANR in com.example.app (com.example.app/.MainActivity)",
        "2026-09-15 12:00:01.000  1000  1000 E ActivityManager: Reason: Input dispatching timed out (b0f45d com.example.app/.MainActivity, Waiting to send key event)",
    ]
    result = salud.parse_anr_logcat(lines)
    assert result is not None
    component, reason = result
    assert component == "com.example.app/.MainActivity"
    assert reason.startswith("Input dispatching timed out")


def test_parse_anr_logcat_missing_pair_returns_none():
    assert salud.parse_anr_logcat(["some unrelated line"]) is None


def test_anr_full_record_with_component_and_reason():
    raw = fixture_text("anr_trace")
    lines = [
        "2026-09-15 12:00:01.000  1000  1000 E ActivityManager: ANR in com.example.app (com.example.app/.MainActivity)",
        "2026-09-15 12:00:01.000  1000  1000 E ActivityManager: Reason: Input dispatching timed out (b0f45d com.example.app/.MainActivity, Waiting to send key event)",
    ]
    component, reason = salud.parse_anr_logcat(lines)
    rec = salud.parse_anr_trace(raw, "com.example.app", component=component, reason=reason)
    assert rec.component == "com.example.app/.MainActivity"
    assert rec.reason.startswith("Input dispatching timed out")
    assert rec.sig != ""


# ----------------------------------------------------------------------------- ActivityManager / exits


def test_parse_am_line_died_from_real_fixture():
    raw = fixture_text("logcat_am_art")
    events = [salud.parse_am_line(l) for l in raw.splitlines()]
    events = [e for e in events if e is not None]
    assert len(events) >= 1
    died = [e for e in events if e.kind == "died"]
    assert len(died) == 1
    assert died[0].pid == 10362
    assert died[0].process == "com.example.app"


def test_parse_am_line_start_and_kill_constructed():
    start = salud.parse_am_line(
        "2026-09-15 10:00:00.000 500 500 I ActivityManager: Start proc 1234:com.example.app/u0a123 for activity"
    )
    assert start is not None
    assert start.kind == "start"
    assert start.pid == 1234
    assert start.process == "com.example.app"

    kill = salud.parse_am_line(
        "2026-09-15 10:00:00.000 500 500 I ActivityManager: Killing 1234:com.example.app/u0a123 (adj 906): remove task"
    )
    assert kill is not None
    assert kill.kind == "killed"
    assert kill.pid == 1234
    assert kill.process == "com.example.app"


def test_parse_am_line_non_am_message_returns_none():
    assert salud.parse_am_line(
        "2026-09-15 10:00:00.000 500 500 I SomeOtherTag: nothing to see here"
    ) is None


def test_exit_to_event():
    rec = ExitRecord(
        timestamp="2026-09-15 12:00:00.123", pid=1230, reason=4, reason_name="CRASH",
        subreason="", status=0, importance=100, description="app crashed", anr="", rss="",
    )
    ev = salud.exit_to_event(rec)
    assert ev.kind == "exit"
    assert ev.pid == 1230
    assert ev.reason_name == "CRASH"
    assert isinstance(ev.t, float)
    assert ev.t > 0


# ----------------------------------------------------------------------------- negative controls


def test_parse_crash_block_empty_is_none():
    assert salud.parse_crash_block([]) is None


def test_split_crash_blocks_empty_text():
    assert salud.split_crash_blocks("") == []


def test_parse_crash_block_warn_line_is_none():
    rec = salud.parse_crash_block(
        ["2026-09-15 10:00:00.000 100 100 W SomeTag: just a warning, nothing crashed"]
    )
    assert rec is None


def test_parse_anr_trace_no_header_returns_none():
    assert salud.parse_anr_trace("nothing here", "com.example.app") is None


# ----------------------------------------------------------------------------- group_events


def test_group_events_counts_same_sig_crashes():
    block = [
        "2026-09-15 10:00:00.000 100 100 E AndroidRuntime: FATAL EXCEPTION: main",
        "2026-09-15 10:00:00.000 100 100 E AndroidRuntime: Process: com.example.app, PID: 100",
        "2026-09-15 10:00:00.000 100 100 E AndroidRuntime: com.example.app.MyException: boom",
        "2026-09-15 10:00:00.000 100 100 E AndroidRuntime: \tat com.example.app.Foo.bar(Foo.java:10)",
    ]
    block2 = [
        "2026-09-15 11:00:00.000 200 200 E AndroidRuntime: FATAL EXCEPTION: main",
        "2026-09-15 11:00:00.000 200 200 E AndroidRuntime: Process: com.example.app, PID: 200",
        "2026-09-15 11:00:00.000 200 200 E AndroidRuntime: com.example.app.MyException: boom again",
        "2026-09-15 11:00:00.000 200 200 E AndroidRuntime: \tat com.example.app.Foo.bar(Foo.java:99)",
    ]
    rec1 = salud.parse_crash_block(block)
    rec2 = salud.parse_crash_block(block2)
    assert rec1.sig == rec2.sig
    groups = salud.group_events([rec1, rec2], [], [])
    assert len(groups) == 1
    g = groups[0]
    assert g.count == 2
    assert set(g.pids) == {100, 200}
    assert g.kind == "java"
    assert g.title.startswith("com.example.app.MyException: boom")


def test_group_events_sorted_by_count_desc():
    solo_block = [
        "2026-09-15 09:00:00.000 300 300 E AndroidRuntime: FATAL EXCEPTION: main",
        "2026-09-15 09:00:00.000 300 300 E AndroidRuntime: Process: com.example.app, PID: 300",
        "2026-09-15 09:00:00.000 300 300 E AndroidRuntime: com.example.app.OtherException: single",
        "2026-09-15 09:00:00.000 300 300 E AndroidRuntime: \tat com.example.app.Solo.once(Solo.java:1)",
    ]
    dup_a = [
        "2026-09-15 10:00:00.000 100 100 E AndroidRuntime: FATAL EXCEPTION: main",
        "2026-09-15 10:00:00.000 100 100 E AndroidRuntime: Process: com.example.app, PID: 100",
        "2026-09-15 10:00:00.000 100 100 E AndroidRuntime: com.example.app.MyException: boom",
        "2026-09-15 10:00:00.000 100 100 E AndroidRuntime: \tat com.example.app.Foo.bar(Foo.java:10)",
    ]
    dup_b = [
        "2026-09-15 11:00:00.000 200 200 E AndroidRuntime: FATAL EXCEPTION: main",
        "2026-09-15 11:00:00.000 200 200 E AndroidRuntime: Process: com.example.app, PID: 200",
        "2026-09-15 11:00:00.000 200 200 E AndroidRuntime: com.example.app.MyException: boom again",
        "2026-09-15 11:00:00.000 200 200 E AndroidRuntime: \tat com.example.app.Foo.bar(Foo.java:99)",
    ]
    solo = salud.parse_crash_block(solo_block)
    a = salud.parse_crash_block(dup_a)
    b = salud.parse_crash_block(dup_b)
    groups = salud.group_events([solo, a, b], [], [])
    assert groups[0].count == 2
    assert groups[1].count == 1


def test_group_events_exits_grouped_by_reason():
    rec1 = ExitRecord(timestamp="2026-09-15 12:00:00.000", pid=1, reason=4, reason_name="CRASH",
                       subreason="", status=0, importance=0, description="", anr="", rss="")
    rec2 = ExitRecord(timestamp="2026-09-15 12:05:00.000", pid=2, reason=4, reason_name="CRASH",
                       subreason="", status=0, importance=0, description="", anr="", rss="")
    e1 = salud.exit_to_event(rec1)
    e2 = salud.exit_to_event(rec2)
    groups = salud.group_events([], [], [e1, e2])
    assert len(groups) == 1
    assert groups[0].count == 2
    assert groups[0].sig == "exit:CRASH"
    assert groups[0].title == "CRASH"


def test_group_events_empty_inputs():
    assert salud.group_events([], [], []) == []
