"""droid.inspector: parseo de /proc/stat, dumpsys meminfo/gfxinfo, exit-info y el sampler rapido."""
import json
from types import SimpleNamespace

import pytest
from tests.helpers import fixture_text

from droid import inspector as insp

_FRAMESTATS_HEADER = ("Flags,FrameTimelineVsyncId,IntendedVsync,Vsync,InputEventId,HandleInputStart,AnimationStart,"
                      "PerformTraversalsStart,DrawStart,FrameDeadline,FrameStartTime,FrameInterval,WorkloadTarget,"
                      "AnimationTime,SyncQueued,SyncStart,IssueDrawCommandsStart,SwapBuffers,FrameCompleted,")


def _fast_sample_text(total_frames, janky, rows=None):
    lines = [
        "cpu  100 0 100 100 0 0 0 0 0 0",
        "##NCPU 4",
        "999 (app) S 1 1 0 0 -1 4194624 0 0 0 0 0 0 0 0 -2 0 26 0 0 0 0 0 1 1 0 0 0 0 0 1 0 0 0 0 17 2 1 1 0 0 0 0 0 0 0 0 0 0 0",
        "##TASKS", "##STATUS", "VmRSS:      1000 kB", "Threads:        1", "##OOM 0", "##GFX",
        f"Total frames rendered: {total_frames}", f"Janky frames: {janky} (0.00%)",
    ]
    if rows is not None:
        lines.append(_FRAMESTATS_HEADER)
        lines.extend(rows)
    return "\n".join(lines) + "\n"


def _fake_session():
    dev = SimpleNamespace(serial="emulator-5554", name="Pixel", key="pixel1", to_dict=lambda: {})
    ses = insp.InspectSession(dev, "com.example.app", interval=1.0, record=False)
    ses.pid = 999
    ses.debuggable = False
    return ses


def test_parse_stat_line_from_proc_pid_stat():
    line = "10362 (igital.app) S 24285 24285 0 0 -1 4194624 48727 0 148 0 148 74 0 0 -2 0 26 0 31262609 7377362944 62111 18446744073709551615 1 1 0 0 0 0 4612 1 1073775864 0 0 0 17 2 1 1 0 0 0 0 0 0 0 0 0 0 0"
    tid, comm, state, ticks = insp.parse_stat_line(line)
    assert (tid, comm, state, ticks) == (10362, "igital.app", "S", 222)


def test_parse_stat_line_task_path_form():
    line = "/proc/10362/task/10363/stat:10363 (Signal Catcher) S 24285 24285 0 0 -1 4194368 54 0 0 0 0 0 0 0 20 0 26 0 31262613 7377362944 62111 18446744073709551615 1 1 0 0 0 0 20480 1 1073775864 0 0 0 -1 1 0 0 0 0 0 0 0 0 0 0 0 0 0"
    tid, comm, state, ticks = insp.parse_stat_line(line)
    assert (tid, comm, state) == (10363, "Signal Catcher", "S")


def test_parse_stat_line_garbage_returns_none():
    assert insp.parse_stat_line("not a stat line") is None
    assert insp.parse_stat_line("") is None


def test_parse_meminfo_real_fixture():
    mi = insp.parse_meminfo(fixture_text("meminfo_app"))
    assert mi.pss_total_kb == 128272
    assert mi.rss_total_kb == 255416
    assert mi.java_heap_kb == 23048
    assert mi.native_heap_kb == 4516
    assert mi.views == 18
    assert mi.view_roots == 1
    assert mi.error == ""


def test_parse_meminfo_timeout_sets_error():
    mi = insp.parse_meminfo("IOException while dumping meminfo\nTimeout waiting for meminfo")
    assert mi.error == "timeout de dumpsys meminfo"
    assert mi.pss_total_kb == 0


def test_parse_meminfo_empty_text_returns_zeroed():
    mi = insp.parse_meminfo("")
    assert mi.pss_total_kb == 0
    assert mi.error == ""


def test_parse_gfxinfo_real_fixture():
    out = insp.parse_gfxinfo(fixture_text("gfxinfo_app"))
    assert out["total_frames"] == 29
    assert out["janky"] == 2
    assert out["p50"] == 17
    assert out["p99"] == 150
    assert out["missed_vsync"] == 0
    assert out["slow_ui"] == 1


def test_parse_gfxinfo_no_process_found_returns_empty_dict():
    assert insp.parse_gfxinfo(fixture_text("gfxinfo_systemui")) == {}


def test_parse_exit_info_real_fixture_reports_crash():
    records = insp.parse_exit_info(fixture_text("exit_info_app_after_crash"))
    assert len(records) == 1
    rec = records[0]
    assert rec.pid == 10362
    assert rec.description == "crash"
    assert rec.timestamp == "2026-09-15 15:03:13.455"
    assert rec.importance == 100
    assert rec.rss == "0.00"


def test_parse_exit_info_multiword_reason_is_parsed():
    records = insp.parse_exit_info(fixture_text("exit_info_app_after_crash"))
    rec = records[0]
    assert rec.reason == 4
    assert rec.reason_name == "CRASH"
    assert rec.subreason == "UNKNOWN"
    assert rec.status == 0


def test_parse_exit_info_user_requested_multiword_subreason():
    records = insp.parse_exit_info(fixture_text("exit_info_all"))
    user_requested = [r for r in records if r.reason == 10]
    assert user_requested
    rec = user_requested[0]
    assert rec.reason == 10
    assert isinstance(rec.status, int)
    assert rec.status == 0


def test_parse_exit_info_garbage_returns_empty_list():
    assert insp.parse_exit_info("nothing relevant here\njust noise") == []
    assert insp.parse_exit_info("") == []


def test_fast_sample_script_includes_pss_when_debuggable():
    script = insp.fast_sample_script(1234, "com.example.app", debuggable=True)
    assert "/proc/1234/stat" in script
    assert "run-as com.example.app cat /proc/1234/smaps_rollup" in script
    assert "##PSS" in script


def test_fast_sample_script_omits_pss_when_not_debuggable():
    script = insp.fast_sample_script(1234, "com.example.app", debuggable=False)
    assert "##PSS" not in script
    assert "smaps_rollup" not in script


def test_parse_fast_sample_real_fixture():
    total_ticks, ncpu, proc, threads, status, oom, pss, gfx = insp.parse_fast_sample(fixture_text("fast_sample_app"))
    assert ncpu == 4
    assert proc[0] == 10362
    assert proc[2] == "S"
    assert len(threads) == 26
    assert status["VmRSS"] == 248948
    assert status["Threads"] == 26
    assert oom == 0
    assert pss["Pss"] == 121747
    assert gfx["total_frames"] == 29


def test_parse_fast_sample_empty_text_returns_defaults():
    total_ticks, ncpu, proc, threads, status, oom, pss, gfx = insp.parse_fast_sample("")
    assert total_ticks == 0
    assert ncpu == 1
    assert proc is None
    assert threads == []
    assert status == {}
    assert oom is None
    assert pss == {}
    assert gfx == {}


def test_oom_label_known_and_none():
    assert insp.oom_label(None) == "?"
    assert insp.oom_label(0) == "primer plano"
    assert insp.oom_label(1000) == "en caché"
    assert insp.oom_label(99999) == "en caché"


def test_fast_sample_script_runs_gfxinfo_framestats():
    script = insp.fast_sample_script(1234, "com.example.app", debuggable=False)
    assert "dumpsys gfxinfo com.example.app framestats" in script
    assert "##GFX" in script


def test_parse_framestats_real_fixture():
    text = fixture_text("gfxinfo_framestats_app")
    frames = insp.parse_framestats(text)
    profiledata_start = text.splitlines().index("---PROFILEDATA---") + 1
    header_line = text.splitlines()[profiledata_start]
    assert header_line.startswith("Flags,FrameTimelineVsyncId")
    expected_count = sum(
        1 for line in text.splitlines()[profiledata_start + 1:]
        if line[:1].isdigit() and line.split(",", 1)[0] == "0"
    )
    assert len(frames) == expected_count
    assert expected_count > 0
    for vsync, duration_ms in frames:
        assert isinstance(vsync, int)
        assert duration_ms > 0


def test_parse_framestats_without_profiledata_returns_empty():
    assert insp.parse_framestats(fixture_text("gfxinfo_app")) == []
    assert insp.parse_framestats("") == []


def test_new_frames_dedupes_across_ticks():
    frames = [(100, 5.0), (200, 6.0), (300, 7.0)]
    ms, last = insp.new_frames(frames, 150)
    assert ms == [6.0, 7.0]
    assert last == 300
    ms2, last2 = insp.new_frames(frames, last)
    assert ms2 == []
    assert last2 == 300


def test_new_frames_empty_input_keeps_last_vsync():
    ms, last = insp.new_frames([], 42)
    assert ms == []
    assert last == 42


def test_load_session_round_trip_with_exits_and_unknown_type(tmp_path):
    path = tmp_path / "session.jsonl"
    lines = [
        {"type": "meta", "device": {}, "package": "com.example.app", "interval": 1.0, "started": "2026-09-15T00:00:00"},
        {"type": "sample", "t": 1.0, "pid": 123, "alive": True},
        {"type": "exit", "timestamp": "2026-09-15 00:00:01", "pid": 123, "reason": 4, "reason_name": "CRASH",
         "subreason": "UNKNOWN", "status": 0, "importance": 100, "description": "crash", "anr": "", "rss": "0.00"},
        {"type": "future_unknown_type", "whatever": True},
        {"type": "end", "ended": "2026-09-15T00:00:02", "samples": 1},
    ]
    with open(path, "w", encoding="utf-8") as fh:
        for obj in lines:
            fh.write(json.dumps(obj) + "\n")
    out = insp.load_session(path)
    assert out["meta"]["package"] == "com.example.app"
    assert len(out["samples"]) == 1
    assert len(out["exits"]) == 1
    assert out["exits"][0]["reason"] == 4
    assert out["end"]["samples"] == 1


def test_tick_reconciles_frames_from_framestats_when_counters_static(monkeypatch):
    ses = _fake_session()
    tick1 = _fast_sample_text(46, 9, ["0,1,100,100,0,0,0,0,0,0,0,0,0,0,0,0,0,0,5000100,"])
    tick2 = _fast_sample_text(46, 9, [
        "0,2,200,200,0,0,0,0,0,0,0,0,0,0,0,0,0,0,20000200,",
        "0,3,300,300,0,0,0,0,0,0,0,0,0,0,0,0,0,0,10000300,",
    ])
    outputs = iter([tick1, tick2])
    monkeypatch.setattr(insp.adbmod, "shell", lambda *a, **k: next(outputs))
    ses._tick()
    ses._tick()
    last = ses.samples[-1]
    assert last.gfx_total_frames == 46
    assert last.frames == 2
    assert last.janky == 1
    assert last.fps > 0
    assert last.jank_pct == pytest.approx(50.0)


def test_tick_falls_back_to_counter_delta_without_framestats(monkeypatch):
    ses = _fake_session()
    tick1 = _fast_sample_text(46, 9)
    tick2 = _fast_sample_text(50, 10)
    outputs = iter([tick1, tick2])
    monkeypatch.setattr(insp.adbmod, "shell", lambda *a, **k: next(outputs))
    ses._tick()
    ses._tick()
    last = ses.samples[-1]
    assert last.frames == 4
    assert last.janky == 1
    assert last.fps > 0


def test_tick_caps_frame_ms_to_240_entries(monkeypatch):
    ses = _fake_session()
    rows = [f"0,{i},{i * 1000},{i * 1000},0,0,0,0,0,0,0,0,0,0,0,0,0,0,{i * 1000 + 5000000}," for i in range(1, 301)]
    monkeypatch.setattr(insp.adbmod, "shell", lambda *a, **k: _fast_sample_text(0, 0, []))
    ses._tick()
    assert ses.samples[-1].frame_ms == []
    monkeypatch.setattr(insp.adbmod, "shell", lambda *a, **k: _fast_sample_text(0, 0, rows))
    ses._tick()
    assert len(ses.samples[-1].frame_ms) == 240
    assert ses.samples[-1].frames_truncated == 60
    assert ses.samples[-1].frames == 300


def test_first_tick_does_not_count_frames_rendered_before_the_session(monkeypatch):
    ses = _fake_session()
    rows = [f"0,{i},{i * 1000},{i * 1000},0,0,0,0,0,0,0,0,0,0,0,0,0,0,{i * 1000 + 5000000}," for i in range(1, 20)]
    monkeypatch.setattr(insp.adbmod, "shell", lambda *a, **k: _fast_sample_text(0, 0, rows))
    ses._tick()
    first = ses.samples[-1]
    assert first.frame_ms == [] and first.frames == 0
    ses._tick()
    second = ses.samples[-1]
    assert second.frame_ms == [] and second.frames == 0
    newer = rows + [f"0,{i},{i * 1000},{i * 1000},0,0,0,0,0,0,0,0,0,0,0,0,0,0,{i * 1000 + 20000000}," for i in range(20, 25)]
    monkeypatch.setattr(insp.adbmod, "shell", lambda *a, **k: _fast_sample_text(0, 0, newer))
    ses._tick()
    third = ses.samples[-1]
    assert len(third.frame_ms) == 5 and third.frames == 5 and third.fps > 0


def test_parse_framestats_merges_multiple_blocks_deduped_by_vsync():
    text = fixture_text("gfxinfo_framestats_app")
    lines = text.splitlines()
    header_idx = next(i for i, l in enumerate(lines) if l.startswith("Flags,FrameTimelineVsyncId"))
    end_idx = next(i for i in range(header_idx + 1, len(lines)) if lines[i].strip() == "---PROFILEDATA---")
    block = lines[header_idx:end_idx]
    header_fields = block[0].rstrip(",").split(",")
    vsync_i = header_fields.index("IntendedVsync")
    shifted_rows = []
    for row in block[1:]:
        fields = row.rstrip(",").split(",")
        fields[vsync_i] = str(int(fields[vsync_i]) + 10_000_000_000)
        shifted_rows.append(",".join(fields) + ",")
    doubled = "\n".join(block) + "\n" + "\n".join(shifted_rows) + "\n"
    single = insp.parse_framestats("\n".join(block) + "\n")
    merged = insp.parse_framestats(doubled)
    assert len(merged) == 2 * len(single)
    vsyncs = [v for v, _ in merged]
    assert len(vsyncs) == len(set(vsyncs))


def test_parse_framestats_dedupes_same_vsync_across_blocks():
    row = "0,1,100,100,0,0,0,0,0,0,0,0,0,0,0,0,0,0,5000100,"
    text = _FRAMESTATS_HEADER + "\n" + row + "\n" + _FRAMESTATS_HEADER + "\n" + row + "\n"
    frames = insp.parse_framestats(text)
    assert len(frames) == 1


def test_fast_sample_script_filters_framestats_dump_through_grep():
    script = insp.fast_sample_script(1234, "com.example.app", debuggable=False)
    assert "framestats 2>/dev/null | grep -E" in script
    assert "^Flags," in script
    assert "^[0-9]+," in script


def test_device_tz_offset_parses_valid_and_rejects_garbage(monkeypatch):
    monkeypatch.setattr(insp.adbmod, "shell", lambda *a, **k: "+0200\n")
    assert insp.device_tz_offset("emulator-5554") == "+0200"
    monkeypatch.setattr(insp.adbmod, "shell", lambda *a, **k: "")
    assert insp.device_tz_offset("emulator-5554") is None
    monkeypatch.setattr(insp.adbmod, "shell", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no device")))
    assert insp.device_tz_offset("emulator-5554") is None


def test_counter_fallback_is_not_used_once_framestats_was_seen(monkeypatch):
    ses = _fake_session()
    rows = [f"0,{i},{i * 1000},{i * 1000},0,0,0,0,0,0,0,0,0,0,0,0,0,0,{i * 1000 + 5000000}," for i in range(1, 6)]
    monkeypatch.setattr(insp.adbmod, "shell", lambda *a, **k: _fast_sample_text(10, 0, rows))
    ses._tick()
    ses._tick()
    monkeypatch.setattr(insp.adbmod, "shell", lambda *a, **k: _fast_sample_text(40, 2))
    ses._tick()
    assert ses.samples[-1].frames == 0
    assert ses.samples[-1].janky == 0


def test_load_session_round_trip_with_gc_and_leak_lines(tmp_path):
    path = tmp_path / "session.jsonl"
    lines = [
        {"type": "meta", "device": {}, "package": "com.example.app", "interval": 1.0, "started": "2026-09-15T00:00:00"},
        {"type": "gc", "t": 1000.0, "kind": "Explicit concurrent mark compact GC", "freed_kb": 10130.0, "los_kb": 704.0,
         "free_pct": 95, "heap_used_kb": 9010.0, "heap_total_kb": 196608.0, "pause_ms": 0.881, "total_ms": 19.749},
        {"type": "leak", "metric": "java_heap_kb", "slope_kb_per_min": 12.3, "samples": 7, "since_t": 1000.0, "until_t": 1090.0,
         "first_kb": 20000.0, "last_kb": 38000.0, "gc_backed": False, "epistemic": "Inferido"},
        {"type": "end", "ended": "2026-09-15T00:00:02", "samples": 1},
    ]
    with open(path, "w", encoding="utf-8") as fh:
        for obj in lines:
            fh.write(json.dumps(obj) + "\n")
    out = insp.load_session(path)
    assert len(out["gc"]) == 1
    assert out["gc"][0]["kind"] == "Explicit concurrent mark compact GC"
    assert len(out["leak"]) == 1
    assert out["leak"][0]["metric"] == "java_heap_kb"


def _meminfo_text(java_kb: int) -> str:
    return (
        " App Summary\n"
        "                        Pss(KB)                        Rss(KB)\n"
        "                         ------                         ------\n"
        f"            Java Heap:    {java_kb}                          49520\n"
        "          Native Heap:     4516                           7408\n"
        "                 Code:     2000                            3000\n"
        "                Stack:      500                             600\n"
        "             Graphics:        0                               0\n"
        "        Private Other:     4896\n"
        "               System:    15072\n"
        "              Unknown:                                   14468\n"
        "\n"
        "            TOTAL PSS:   60000            TOTAL RSS:   255416       TOTAL SWAP PSS:       38\n"
        "\n"
        " Objects\n"
        "               Views:       18         ViewRootImpl:        1\n"
    )


def test_meminfo_java_heap_growth_writes_one_leak_record(monkeypatch, tmp_path):
    ses = _fake_session()
    ses.record = True
    ses.path = tmp_path / "session.jsonl"
    ses._fh = open(ses.path, "a", encoding="utf-8")

    t = [1000.0]

    def fake_time():
        t[0] += 15.0
        return t[0]

    monkeypatch.setattr(insp.time, "time", fake_time)

    calls = {"n": 0}

    def fake_shell(serial, cmd, timeout=15):
        calls["n"] += 1
        java = 20000 + (calls["n"] - 1) * 3000
        return _meminfo_text(java)

    monkeypatch.setattr(insp.adbmod, "shell", fake_shell)

    for _ in range(7):
        ses._meminfo(ses.pid)

    assert len(ses.leak_flags) == 1
    assert ses.leak_flags[0].metric == "java_heap_kb"

    ses._fh.close()
    written = [json.loads(l) for l in ses.path.read_text().splitlines()]
    leak_lines = [o for o in written if o.get("type") == "leak"]
    assert len(leak_lines) == 1
    assert leak_lines[0]["metric"] == "java_heap_kb"
