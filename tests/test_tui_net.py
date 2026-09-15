"""Prueba de humo de la pestaña Red: hosts, eventos y cadencia a partir de un monitor falso."""
from types import SimpleNamespace

import droid.adb as adbmod
from droid.net import ReverseDns, Socket
from droid.tui import DroidApp
from droid.tui_panes import NetPane
from textual.widgets import DataTable, Static


async def _wait_until(pilot, predicate, attempts=100, step=0.02):
    for _ in range(attempts):
        if predicate():
            return True
        await pilot.pause(step)
    return False


def _fake_mon():
    dns = ReverseDns()
    dns._cache["1.2.3.4"] = "example.com"
    sample = SimpleNamespace(t=1.0, rates={"wlan0": (100.0, 50.0)}, rx_rate=100.0, tx_rate=50.0,
                              app_rx_rate=200.0, app_tx_rate=80.0)
    sock = Socket(proto="tcp", state="ESTAB", local="0.0.0.0:1234", remote="1.2.3.4:443", uid=10099)
    return SimpleNamespace(
        dev=SimpleNamespace(key="dev1", display="Pixel (dev1)"),
        package="com.example.app", uid=10099, running=True,
        samples=[sample],
        totals={"rx": 1000, "tx": 500, "by_type": {"WIFI": (700, 300)}, "source": "detail"},
        totals_start=None,
        sockets=[sock],
        hosts=[{"host": "example.com", "ip": "1.2.3.4", "connections": 3,
                "states": {"ESTAB": 2, "TIME_WAIT": 1}, "first_seen": 1.0, "last_seen": 2.0}],
        events=[{"t": 2.0, "kind": "new", "proto": "tcp", "local": "0.0.0.0:1234",
                 "remote": "1.2.3.4:443", "state": "ESTAB"}],
        cadence_label="por app acumulado cada 10 s (dumpsys netstats)",
        qtaguid_available=False,
        dns=dns,
        _tot_last=0.0,
    )


async def test_net_pane_shows_hosts_events_and_cadence(monkeypatch):
    monkeypatch.setattr(adbmod, "list_devices", lambda details=True: [])
    monkeypatch.setattr(adbmod, "adb_path", lambda: "/bin/echo")
    monkeypatch.setattr(DroidApp, "refresh_devices", lambda self: None)
    monkeypatch.setattr(DroidApp, "_tick_periodic", lambda self: None)
    app = DroidApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("9")
        assert await _wait_until(pilot, lambda: app.active_tab == "net")

        pane = app.query_one("#net_pane", NetPane)
        pane.mon = _fake_mon()
        pane._refresh()
        await pilot.pause()

        hosts_table = pane.query_one("#n_hosts", DataTable)
        assert hosts_table.row_count == 1
        row = [str(c) for c in hosts_table.get_row_at(0)]
        assert any("example.com" in c for c in row)

        events_table = pane.query_one("#n_events", DataTable)
        assert events_table.row_count == 1
        erow = [str(c) for c in events_table.get_row_at(0)]
        assert any("nuevo" in c for c in erow)

        head_static = pane.query_one("#n_head", Static)
        head_content = str(getattr(head_static, "_Static__content", ""))
        assert "por app acumulado cada 10 s" in head_content


def test_net_pane_sockets_table_has_same_min_height_as_hosts_and_events():
    """F346-C9: #n_sockets (la tabla original, previa a F6) no debe quedar sin piso frente a las
    dos tablas nuevas en pantallas pequeñas."""
    css = NetPane.DEFAULT_CSS
    for sel in ("#n_hosts", "#n_events", "#n_sockets"):
        idx = css.index(sel)
        rule = css[idx:css.index("}", idx)]
        assert "min-height: 4" in rule, f"{sel} no tiene min-height: 4 en {rule!r}"
