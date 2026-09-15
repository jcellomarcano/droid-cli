"""TUI: pestaña Memoria del inspector (MemoriaView) y carga de sesiones grabadas (load_replay)."""
import json
from types import SimpleNamespace

import droid.adb as adbmod
from droid.inspector import ExitRecord, MemInfo
from droid.memoria import GcEvent, LeakFlag
from droid.tui import DroidApp
from droid.tui_panes import InspectPane, MemoriaView
from textual.widgets import DataTable, TabbedContent


async def _wait_until(pilot, predicate, attempts=100, step=0.02):
    for _ in range(attempts):
        if predicate():
            return True
        await pilot.pause(step)
    return False


def _meminfo(t=1000.0, java_kb=20000):
    return MemInfo(
        t=t, pss_total_kb=95000, rss_total_kb=100000, java_heap_kb=java_kb, native_heap_kb=15000,
        code_kb=5000, stack_kb=1000, graphics_kb=8000, private_other_kb=2000, system_kb=3000,
        dalvik_heap_size_kb=30000, dalvik_heap_alloc_kb=20000, dalvik_heap_free_kb=10000,
        native_heap_size_kb=18000, native_heap_alloc_kb=15000, native_heap_free_kb=3000,
        views=50, view_roots=2, activities=1, app_contexts=3,
    )


def _gc_event(t=1000.0):
    return GcEvent(t=t, kind="Explicit concurrent mark compact GC", freed_kb=10130.0, los_kb=704.0, free_pct=95,
                    heap_used_kb=9010.0, heap_total_kb=196608.0, pause_ms=0.881, total_ms=19.749)


def _leak_flag(metric="java_heap_kb"):
    return LeakFlag(metric=metric, slope_kb_per_min=12.3, samples=7, since_t=1000.0, until_t=1090.0,
                     first_kb=20000.0, last_kb=38000.0, gc_backed=False)


def _fake_session(with_leak=False):
    return SimpleNamespace(
        dev=SimpleNamespace(key="dev1", display="Pixel (dev1)"),
        package="com.example.app", debuggable=True, path=None, interval=1.0,
        samples=[], meminfos=[_meminfo()], exits=[], gc_events=[_gc_event()],
        leak_flags=[_leak_flag()] if with_leak else [], running=False, pid=1,
    )


async def _boot(monkeypatch):
    monkeypatch.setattr(adbmod, "list_devices", lambda details=True: [])
    monkeypatch.setattr(adbmod, "adb_path", lambda: "/bin/echo")
    monkeypatch.setattr(DroidApp, "refresh_devices", lambda self: None)
    monkeypatch.setattr(DroidApp, "_tick_periodic", lambda self: None)
    return DroidApp()


async def test_memoria_tab_shows_breakdown_and_leak_banner(monkeypatch):
    app = await _boot(monkeypatch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("7")
        assert await _wait_until(pilot, lambda: app.active_tab == "inspect")

        pane = app.query_one("#inspect_pane", InspectPane)
        fake = _fake_session(with_leak=True)
        pane.session = fake
        pane.query_one(MemoriaView).set_session(fake)

        await pilot.press("u")
        assert await _wait_until(pilot, lambda: pane.query_one("#i_switch", TabbedContent).active == "i_view_memoria")
        assert pane.query_one("#i_switch", TabbedContent).active == "i_view_memoria"

        assert await _wait_until(pilot, lambda: pane.query_one("#me_breakdown", DataTable).row_count == 8)
        bt = pane.query_one("#me_breakdown", DataTable)
        assert bt.row_count == 8

        head_text = pane.query_one("#me_head").content
        assert "fuga" in str(head_text)

        await pilot.press("o")
        assert await _wait_until(pilot, lambda: pane.query_one("#i_switch", TabbedContent).active == "i_view_overview")
        assert pane.query_one("#i_switch", TabbedContent).active == "i_view_overview"


async def test_memoria_tab_has_no_leak_banner_without_leak_flags(monkeypatch):
    app = await _boot(monkeypatch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("7")
        assert await _wait_until(pilot, lambda: app.active_tab == "inspect")

        pane = app.query_one("#inspect_pane", InspectPane)
        fake = _fake_session(with_leak=False)
        pane.session = fake
        pane.query_one(MemoriaView).set_session(fake)

        await pilot.press("u")
        assert await _wait_until(pilot, lambda: pane.query_one("#i_switch", TabbedContent).active == "i_view_memoria")

        assert await _wait_until(pilot, lambda: pane.query_one("#me_breakdown", DataTable).row_count == 8)
        head_text = str(pane.query_one("#me_head").content)
        assert "fuga" not in head_text


def _write_session_jsonl(path):
    lines = [
        {"type": "meta", "device": {"key": "dev1", "model": "Pixel"}, "package": "com.example.app", "interval": 1.0,
         "started": "2026-09-15T00:00:00"},
        {"type": "sample", "t": 1000.0, "pid": 42, "alive": True, "cpu": 5.0, "cpu_total_pct": 2.5, "ncpu": 8,
         "nthreads": 10, "rss_kb": 100000, "rss_anon_kb": 90000, "rss_file_kb": 10000, "swap_kb": 0, "pss_kb": 95000,
         "oom_adj": 0, "frames": 16, "janky": 1, "fps": 58.0, "jank_pct": 6.0, "p50": 10, "p90": 20, "p99": 30,
         "missed_vsync": 0, "slow_ui": 0, "gfx_total_frames": 100, "gfx_total_janky": 2, "interval": 1.0,
         "frame_ms": [], "frames_truncated": 0, "threads": []},
        {"type": "meminfo", **_meminfo(t=1000.0).to_dict()},
        {"type": "gc", **_gc_event(t=1000.0).to_dict()},
        {"type": "leak", **_leak_flag().to_dict()},
        {"type": "exit", "timestamp": "2026-09-15 00:00:01", "pid": 42, "reason": 4, "reason_name": "CRASH",
         "subreason": "UNKNOWN", "status": 0, "importance": 100, "description": "crash", "anr": "", "rss": "0.00"},
        {"type": "end", "ended": "2026-09-15T00:00:02", "samples": 1},
    ]
    with open(path, "w", encoding="utf-8") as fh:
        for obj in lines:
            fh.write(json.dumps(obj) + "\n")


async def test_load_replay_populates_session_for_overview_and_memoria(monkeypatch, tmp_path):
    app = await _boot(monkeypatch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("7")
        assert await _wait_until(pilot, lambda: app.active_tab == "inspect")

        path = tmp_path / "session.jsonl"
        _write_session_jsonl(path)
        pane = app.query_one("#inspect_pane", InspectPane)
        pane.load_replay(path)

        assert pane.session is not None
        assert pane.session.package == "com.example.app"
        assert pane.session.running is False
        assert len(pane.session.samples) == 1
        assert pane.session.samples[0].pid == 42
        assert len(pane.session.meminfos) == 1
        assert len(pane.session.gc_events) == 1
        assert len(pane.session.leak_flags) == 1
        assert len(pane.session.exits) == 1

        await pilot.press("u")
        assert await _wait_until(pilot, lambda: pane.query_one("#i_switch", TabbedContent).active == "i_view_memoria")
        assert await _wait_until(pilot, lambda: pane.query_one("#me_breakdown", DataTable).row_count == 8)
        assert pane.query_one("#me_breakdown", DataTable).row_count == 8
