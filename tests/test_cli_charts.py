"""CLI live/replay chart rendering: _inspect_view, _replay_chart, _net_view, cmd_inspect --chart."""
import json
from types import SimpleNamespace

import pytest
from rich.console import Console

from droid import cli, inspector as insp


def _dev(key="dev1", display="Pixel (dev1)"):
    return SimpleNamespace(key=key, display=display)


def _sample(t, cpu=10.0, rss_kb=100000, frame_ms=None, pid=123):
    return insp.Sample(
        t=t, pid=pid, alive=True, cpu=cpu, cpu_total_pct=cpu / 2, ncpu=8,
        threads=[insp.ThreadSample(tid=pid, name="main", state="R", ticks=0, cpu=5.0)],
        nthreads=20, rss_kb=rss_kb, rss_anon_kb=rss_kb - 1000, rss_file_kb=1000, swap_kb=0,
        pss_kb=rss_kb - 5000, oom_adj=0, frames=16, janky=1, fps=58.0, jank_pct=6.0,
        p50=10, p90=20, p99=30, missed_vsync=0, slow_ui=0, gfx_total_frames=100, gfx_total_janky=2,
        interval=1.0, frame_ms=frame_ms or [],
    )


def _meminfo(t=1002.0):
    return insp.MemInfo(
        t=t, pss_total_kb=95000, rss_total_kb=100000, java_heap_kb=20000, native_heap_kb=15000,
        code_kb=5000, stack_kb=1000, graphics_kb=8000, private_other_kb=2000, system_kb=3000,
        dalvik_heap_size_kb=30000, dalvik_heap_alloc_kb=20000, dalvik_heap_free_kb=10000,
        native_heap_size_kb=18000, native_heap_alloc_kb=15000, native_heap_free_kb=3000,
        views=50, view_roots=2, activities=1, app_contexts=3,
    )


def _exit(ts="2026-09-15 10:00:02.000", reason=4, reason_name="CRASH"):
    return insp.ExitRecord(timestamp=ts, pid=123, reason=reason, reason_name=reason_name,
                            subreason="", status=0, importance=0, description="", anr="", rss="")


def _fake_session(with_frames=True):
    samples = [_sample(1000.0 + i, cpu=10.0 + i, rss_kb=100000 + i * 1000,
                        frame_ms=[8.0, 9.0, 20.0] if with_frames else None) for i in range(3)]
    return SimpleNamespace(
        dev=_dev(), package="com.example.app", debuggable=True, path=None, interval=1.0,
        samples=samples, meminfos=[_meminfo()], exits=[_exit()],
    )


def _render(renderable, width=100):
    console = Console(record=True, width=width, force_terminal=False)
    console.print(renderable)
    return console.export_text()


def test_inspect_view_has_labeled_sparks_and_heap_and_frame_blocks():
    ses = _fake_session()
    out = _render(cli._inspect_view(ses, top=15, thread_pat=None))
    assert "mín" in out
    for label in ("Java heap", "Native heap", "Code", "Stack", "Graphics", "Private other", "System"):
        assert label in out
    assert "Frames (histograma)" in out
    assert "<16 ms" in out


def test_inspect_view_timeline_shows_crash_glyph_for_known_exit():
    ses = _fake_session()
    out = _render(cli._inspect_view(ses, top=15, thread_pat=None))
    assert "×" in out
    assert "crash" in out


def test_inspect_view_falls_back_to_percentile_bars_without_frame_ms():
    ses = _fake_session(with_frames=False)
    out = _render(cli._inspect_view(ses, top=15, thread_pat=None))
    assert "Frames (histograma)" in out
    assert "p50" in out and "p90" in out and "p99" in out


def test_inspect_view_thread_table_uses_hbar():
    ses = _fake_session()
    out = cli._inspect_view(ses, top=15, thread_pat=None)
    rendered = _render(out)
    assert "main" in rendered


def _write_session_jsonl(path, with_frames=True):
    meta = {"type": "meta", "device": {"key": "dev1", "model": "Pixel", "serial": "S1"},
            "package": "com.example.app", "interval": 1.0, "started": "2026-09-15T10:00:00"}
    lines = [meta]
    for i in range(3):
        lines.append({
            "type": "sample", "t": 1000.0 + i, "pid": 123, "alive": True,
            "cpu": 10.0 + i, "cpu_total_pct": 5.0 + i, "ncpu": 8, "nthreads": 20,
            "rss_kb": 100000 + i * 1000, "rss_anon_kb": 90000, "rss_file_kb": 10000, "swap_kb": 0,
            "pss_kb": 95000, "oom_adj": 0, "frames": 16, "janky": 1, "fps": 58.0, "jank_pct": 6.0,
            "p50": 10, "p90": 20, "p99": 30, "missed_vsync": 0, "slow_ui": 0,
            "gfx_total_frames": 100 + i, "gfx_total_janky": 2, "interval": 1.0,
            "frame_ms": [8.0, 9.0, 20.0] if with_frames else [],
            "threads": [{"tid": 123, "name": "main", "state": "R", "cpu": 5.0}],
        })
    lines.append({"type": "meminfo", "t": 1002.0, "pss_total_kb": 95000, "rss_total_kb": 100000,
                  "java_heap_kb": 20000, "native_heap_kb": 15000, "code_kb": 5000, "stack_kb": 1000,
                  "graphics_kb": 8000, "private_other_kb": 2000, "system_kb": 3000,
                  "dalvik_heap_size_kb": 30000, "dalvik_heap_alloc_kb": 20000, "dalvik_heap_free_kb": 10000,
                  "native_heap_size_kb": 18000, "native_heap_alloc_kb": 15000, "native_heap_free_kb": 3000,
                  "views": 50, "view_roots": 2, "activities": 1, "app_contexts": 3, "error": ""})
    lines.append({"type": "exit", "timestamp": "2026-09-15 10:00:02.000", "pid": 123, "reason": 4,
                  "reason_name": "CRASH", "subreason": "", "status": 0, "importance": 0,
                  "description": "", "anr": "", "rss": ""})
    lines.append({"type": "end", "ended": "2026-09-15T10:05:00", "samples": 3})
    with open(path, "w", encoding="utf-8") as fh:
        for obj in lines:
            fh.write(json.dumps(obj) + "\n")


def test_replay_chart_renders_summary_sparks_and_exit_glyph(tmp_path):
    path = tmp_path / "session.jsonl"
    _write_session_jsonl(path)
    data = insp.load_session(path)
    out = _render(cli._replay_chart(data))
    assert "mín" in out
    assert "×" in out
    assert "Java heap" in out
    assert "Frames (histograma)" in out


def _write_session_jsonl_with_gc_leak(path):
    _write_session_jsonl(path)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "gc", "t": 1000.5, "kind": "Explicit concurrent mark compact GC",
                             "freed_kb": 10130.0, "los_kb": 704.0, "free_pct": 95, "heap_used_kb": 9010.0,
                             "heap_total_kb": 196608.0, "pause_ms": 0.881, "total_ms": 19.749}) + "\n")
        fh.write(json.dumps({"type": "leak", "metric": "java_heap_kb", "slope_kb_per_min": 12.3, "samples": 7,
                             "since_t": 1000.0, "until_t": 1090.0, "first_kb": 20000.0, "last_kb": 38000.0,
                             "gc_backed": False, "epistemic": "Inferido"}) + "\n")


def test_replay_chart_shows_gc_and_leak_lines(tmp_path):
    path = tmp_path / "session_gc.jsonl"
    _write_session_jsonl_with_gc_leak(path)
    data = insp.load_session(path)
    out = _render(cli._replay_chart(data))
    assert "GC: 1 colecciones · pausa total 1 ms" in out
    assert "Posible fuga: java_heap_kb" in out


def test_cmd_inspect_replay_plain_shows_gc_leak_count(tmp_path, capsys):
    path = tmp_path / "session_gc.jsonl"
    _write_session_jsonl_with_gc_leak(path)
    args = SimpleNamespace(list=False, replay=str(path))
    rc = cli.cmd_inspect(args)
    assert rc == 0
    captured = capsys.readouterr()
    assert "GC: 1 colecciones · pausa total 1 ms · fugas posibles: 1" in captured.out


def test_replay_chart_empty_session_does_not_raise(tmp_path):
    path = tmp_path / "empty.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "meta", "device": {}, "package": "com.example.app"}) + "\n")
        fh.write(json.dumps({"type": "end", "ended": "2026-09-15T10:05:00", "samples": 0}) + "\n")
    data = insp.load_session(path)
    out = _render(cli._replay_chart(data))
    assert "sin muestras" in out


GOLDEN_REPLAY_TEXT = (
    "com.example.app · Pixel · 2026-09-15T10:00:00 → 2026-09-15T10:05:00 · 3 muestras\n"
    "CPU media 11.0% máx 12.0%  ▇██\n"
    "RSS media 99 MB máx 100 MB  ▁▄█\n"
    "Frames 48 · jank 3 (6.2%) · fps medio 58.0  ███\n"
    "Hilos con más CPU    \n"
    "(media)              \n"
    "                     \n"
    "  Hilo   %CPU medio  \n"
    " ─────────────────── \n"
    "  main          5.0  \n"
    "                     \n"
    "último meminfo: PSS 93 MB · Java 20 MB · Native 15 MB · Views 50 · Activities 1\n"
)


def test_cmd_inspect_replay_without_chart_is_byte_identical_to_before(tmp_path, capsys):
    path = tmp_path / "session.jsonl"
    _write_session_jsonl(path)
    args = SimpleNamespace(list=False, replay=str(path))
    rc = cli.cmd_inspect(args)
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out == GOLDEN_REPLAY_TEXT


def test_cmd_inspect_replay_with_chart_flag_prints_chart_renderable(tmp_path, capsys):
    path = tmp_path / "session.jsonl"
    _write_session_jsonl(path)
    args = SimpleNamespace(list=False, replay=str(path), chart=True)
    rc = cli.cmd_inspect(args)
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out != GOLDEN_REPLAY_TEXT
    assert "mín" in captured.out
    assert "Memoria por heap" in captured.out


def test_net_view_uses_dual_spark_and_por_tipo_line():
    sample = SimpleNamespace(rates={"wlan0": (1000, 2000)})
    mon = SimpleNamespace(
        dev=_dev(), package=None, uid=None, samples=[sample],
        totals={"rx": 5000, "tx": 6000, "by_type": {"wifi": (3000, 4000), "mobile": (2000, 2000)}},
        totals_start=None, sockets=[],
    )
    out = _render(cli._net_view(mon))
    assert "wlan0" in out
    assert "Por tipo" in out
    assert "wifi" in out and "mobile" in out


def test_exit_events_uses_device_tz_offset_when_present():
    e = insp.ExitRecord(timestamp="2026-09-15 15:03:13.455", pid=1, reason=4, reason_name="CRASH",
                         subreason="", status=0, importance=0, description="", anr="", rss="")
    events_utc = cli._exit_events([e], "+0000")
    events_plus2 = cli._exit_events([e], "+0200")
    assert len(events_utc) == 1 and len(events_plus2) == 1
    assert events_utc[0][0] - events_plus2[0][0] == pytest.approx(2 * 3600)


def test_exit_events_falls_back_to_host_tz_when_offset_missing_or_invalid():
    e = insp.ExitRecord(timestamp="2026-09-15 15:03:13.455", pid=1, reason=4, reason_name="CRASH",
                         subreason="", status=0, importance=0, description="", anr="", rss="")
    from datetime import datetime as _dt
    host_ts = _dt.strptime("2026-09-15 15:03:13.455", "%Y-%m-%d %H:%M:%S.%f").timestamp()
    assert cli._exit_events([e], None)[0][0] == host_ts
    assert cli._exit_events([e], "not-an-offset")[0][0] == host_ts
    assert cli._exit_events([e])[0][0] == host_ts


def test_net_view_shows_hosts_and_events_tables():
    sample = SimpleNamespace(rates={"wlan0": (1000, 2000)})
    mon = SimpleNamespace(
        dev=_dev(), package=None, uid=None, samples=[sample],
        totals={"rx": 5000, "tx": 6000, "by_type": {"wifi": (3000, 4000)}},
        totals_start=None, sockets=[],
        hosts=[{"host": "example.com", "ip": "1.2.3.4", "connections": 3,
                "states": {"ESTAB": 2}, "first_seen": 1.0, "last_seen": 2.0}],
        events=[{"t": 2.0, "kind": "new", "proto": "tcp", "local": "0.0.0.0:1234",
                 "remote": "1.2.3.4:443", "state": "ESTAB"}],
        cadence_label="por app acumulado cada 10 s (dumpsys netstats)",
    )
    out = _render(cli._net_view(mon))
    assert "Hosts" in out
    assert "example.com" in out
    assert "Eventos" in out
    assert "por app acumulado cada 10 s" in out


def test_net_view_tolerates_missing_hosts_and_events():
    sample = SimpleNamespace(rates={"wlan0": (1000, 2000)})
    mon = SimpleNamespace(
        dev=_dev(), package=None, uid=None, samples=[sample],
        totals={"rx": 5000, "tx": 6000, "by_type": {"wifi": (3000, 4000)}},
        totals_start=None, sockets=[],
    )
    out = _render(cli._net_view(mon))
    assert "wlan0" in out
    assert "Hosts" not in out
    assert "Eventos" not in out
