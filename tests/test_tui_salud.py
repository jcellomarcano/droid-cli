"""TUI: pestaña Salud del inspector (SaludView) — grupos de crash/ANR/salidas."""
from types import SimpleNamespace

import droid.adb as adbmod
from droid import salud
from droid.tui import DroidApp
from droid.tui_panes import InspectPane, SaludView
from textual.widgets import DataTable, TabbedContent, TextArea


async def _wait_until(pilot, predicate, attempts=100, step=0.02):
    for _ in range(attempts):
        if predicate():
            return True
        await pilot.pause(step)
    return False


_CRASH_LINES = [
    "2026-09-15 15:03:13.450 10362 10362 E AndroidRuntime: FATAL EXCEPTION: main",
    "2026-09-15 15:03:13.450 10362 10362 E AndroidRuntime: Process: com.example.app, PID: 10362",
    "2026-09-15 15:03:13.450 10362 10362 E AndroidRuntime: java.lang.RuntimeException: boom",
    "2026-09-15 15:03:13.450 10362 10362 E AndroidRuntime: \tat com.example.app.Main.onCreate(Main.java:1)",
]


def _crash():
    return salud.parse_crash_block(_CRASH_LINES)


def _exit_event():
    return salud.ProcEvent(t=1000.0, kind="exit", pid=10362, process="", reason_name="CRASH", detail="crash")


def _fake_session_with_crashes():
    crashes = [_crash(), _crash()]
    exits = [_exit_event()]
    groups = salud.group_events(crashes, [], exits)
    return SimpleNamespace(
        dev=SimpleNamespace(key="dev1", display="Pixel (dev1)"),
        package="com.example.app", debuggable=True, path=None, interval=1.0,
        samples=[], meminfos=[], exits=[], gc_events=[], leak_flags=[],
        crashes=crashes, anrs=[], proc_events=exits, groups=groups,
        running=False, pid=10362,
    )


async def _boot(monkeypatch):
    monkeypatch.setattr(adbmod, "list_devices", lambda details=True: [])
    monkeypatch.setattr(adbmod, "adb_path", lambda: "/bin/echo")
    monkeypatch.setattr(DroidApp, "refresh_devices", lambda self: None)
    monkeypatch.setattr(DroidApp, "_tick_periodic", lambda self: None)
    return DroidApp()


async def test_salud_tab_shows_grouped_crashes_and_detail(monkeypatch):
    app = await _boot(monkeypatch)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.press("7")
        assert await _wait_until(pilot, lambda: app.active_tab == "inspect")

        pane = app.query_one("#inspect_pane", InspectPane)
        fake = _fake_session_with_crashes()
        pane.session = fake
        pane.query_one(SaludView).set_session(fake)

        await pilot.press("h")
        assert await _wait_until(pilot, lambda: pane.query_one("#i_switch", TabbedContent).active == "i_view_salud")
        assert pane.query_one("#i_switch", TabbedContent).active == "i_view_salud"

        assert await _wait_until(pilot, lambda: pane.query_one("#sa_groups", DataTable).row_count == 2)
        table = pane.query_one("#sa_groups", DataTable)
        assert table.row_count == 2
        first_row = table.get_row_at(0)
        assert str(first_row[3]) == "2"

        await pilot.press("enter")
        assert await _wait_until(pilot, lambda: "FATAL EXCEPTION" in pane.query_one("#sa_detail", TextArea).text)
        assert "FATAL EXCEPTION" in pane.query_one("#sa_detail", TextArea).text

        await pilot.press("o")
        assert await _wait_until(pilot, lambda: pane.query_one("#i_switch", TabbedContent).active == "i_view_overview")
        assert pane.query_one("#i_switch", TabbedContent).active == "i_view_overview"
