"""droid.watch.LogWatcher: sigue logcat en un hilo daemon y filtra por pid."""
import stat
import threading
import time
from pathlib import Path

import pytest

import droid.adb as adbmod
from droid.watch import LogWatcher

FAKE_ADB_GC_LINES = """#!/bin/bash
if [[ "$1" == "wait-for-device" ]]; then
  echo device
  exit 0
fi
if [[ "$*" == *"get-state"* ]]; then
  echo device
  exit 0
fi
echo "2026-09-15 15:09:12.773 10362 14001 I igital.app: Explicit concurrent mark compact GC freed 10130KB AllocSpace bytes, 22(704KB) LOS objects, 95% free, 9010KB/192MB, paused 274us,607us total 19.749ms"
echo "2026-09-15 15:09:30.406 10362 14001 I igital.app: Explicit concurrent mark compact GC freed 160KB AllocSpace bytes, 0(0B) LOS objects, 95% free, 8982KB/192MB, paused 43us,782us total 18.337ms"
echo "2026-09-15 15:09:32.475 1 1 I system_server: Explicit concurrent mark compact GC freed 64KB AllocSpace bytes, 0(0B) LOS objects, 95% free, 8982KB/192MB, paused 77us,635us total 18.373ms"
echo "2026-09-15 15:09:33.001 10362 14001 I igital.app: Explicit concurrent mark compact GC freed 64KB AllocSpace bytes, 0(0B) LOS objects, 95% free, 8982KB/192MB, paused 77us,635us total 18.373ms"
exit 0
"""

FAKE_ADB_SILENT = """#!/bin/bash
exit 0
"""


def _write_fake_adb(tmp_path: Path, script: str) -> Path:
    p = tmp_path / "adb"
    p.write_text(script)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


def _fast_logs_time(monkeypatch):
    import time as real_time

    import droid.logs as logsmod

    class _FastTime:
        def sleep(self, _seconds):
            return None

        def time(self):
            return real_time.time()

    monkeypatch.setattr(logsmod, "time", _FastTime())


@pytest.mark.allow_adb
def test_log_watcher_filters_by_pid(tmp_path, monkeypatch):
    fake_adb = _write_fake_adb(tmp_path, FAKE_ADB_GC_LINES)
    monkeypatch.setattr(adbmod, "adb_path", lambda: str(fake_adb))
    monkeypatch.setattr(adbmod, "list_devices", lambda details=False: [])
    _fast_logs_time(monkeypatch)

    seen = []
    lock = threading.Lock()

    def on_line(ll, raw):
        with lock:
            seen.append((ll, raw))

    watcher = LogWatcher("EMU123", "emu123key", on_line, pid=10362, buffers=("main",), tail=1)
    watcher.start()
    try:
        for _ in range(100):
            with lock:
                if len(seen) >= 3:
                    break
            time.sleep(0.05)
    finally:
        watcher.stop()
        watcher.join(timeout=3)

    with lock:
        n = len(seen)
        pids = {ll.pid for ll, _ in seen}
    assert n == 3
    assert pids == {10362}


@pytest.mark.allow_adb
def test_log_watcher_swallows_callback_errors_and_counts_them(tmp_path, monkeypatch):
    fake_adb = _write_fake_adb(tmp_path, FAKE_ADB_GC_LINES)
    monkeypatch.setattr(adbmod, "adb_path", lambda: str(fake_adb))
    monkeypatch.setattr(adbmod, "list_devices", lambda details=False: [])
    _fast_logs_time(monkeypatch)

    def on_line(ll, raw):
        raise RuntimeError("boom")

    watcher = LogWatcher("EMU123", "emu123key", on_line, pid=10362)
    watcher.start()
    try:
        for _ in range(100):
            if watcher.errors >= 3:
                break
            time.sleep(0.05)
    finally:
        watcher.stop()
        watcher.join(timeout=3)

    assert watcher.errors == 3
    assert watcher.is_alive() is False


@pytest.mark.allow_adb
def test_log_watcher_no_lines_from_silent_adb(tmp_path, monkeypatch):
    fake_adb = _write_fake_adb(tmp_path, FAKE_ADB_SILENT)
    monkeypatch.setattr(adbmod, "adb_path", lambda: str(fake_adb))
    monkeypatch.setattr(adbmod, "list_devices", lambda details=False: [])
    _fast_logs_time(monkeypatch)

    seen = []
    watcher = LogWatcher("EMU123", "emu123key", lambda ll, raw: seen.append(ll), pid=999)
    watcher.start()
    time.sleep(0.2)
    watcher.stop()
    watcher.join(timeout=3)
    assert seen == []


def test_set_pid_retargets(monkeypatch):
    watcher = LogWatcher("EMU123", "emu123key", lambda ll, raw: None, pid=None)
    assert watcher.pid is None
    watcher.set_pid(4242)
    assert watcher.pid == 4242


def test_log_watcher_stop_bounds_wait_when_process_ignores_terminate():
    """F346-C3: LogWatcher.stop(timeout=0.5) debe acotar la espera del subprocess.wait aunque el
    proceso ignore terminate() (nunca sale por su cuenta): stop() debe volver en bien menos de 2s."""
    import subprocess as subprocessmod
    import time as timemod

    watcher = LogWatcher("EMU123", "emu123key", lambda ll, raw: None, pid=999)

    class FakeProc:
        def poll(self):
            return None  # nunca termina por su cuenta

        def terminate(self):
            pass  # el proceso lo ignora

        def wait(self, timeout=None):
            raise subprocessmod.TimeoutExpired(cmd="adb logcat", timeout=timeout)

        def kill(self):
            pass

    watcher.stream.proc = FakeProc()
    t0 = timemod.time()
    watcher.stop(timeout=0.3)
    elapsed = timemod.time() - t0
    assert elapsed < 1.0


def test_log_watcher_extra_tags_defaults_to_empty_set():
    watcher = LogWatcher("EMU123", "emu123key", lambda ll, raw: None, pid=999)
    assert watcher.extra_tags == set()
    watcher2 = LogWatcher("EMU123", "emu123key", lambda ll, raw: None, pid=999, extra_tags=("ActivityManager",))
    assert watcher2.extra_tags == {"ActivityManager"}


# ------------------------------------------------------------------ Salud: sesión + crash/ANR/proc events

def _harness_session(pid=10362, package="com.example.app"):
    from types import SimpleNamespace

    from droid.inspector import InspectSession

    dev = SimpleNamespace(serial="EMU123", key="emu123key", name="Emu", to_dict=lambda: {})
    ses = InspectSession(dev, package, interval=1.0, record=False)
    ses.pid = pid
    ses.debuggable = False
    return ses


def _feed_fixture(ses, fixture_name, extra_raw_lines=()):
    import droid.logs as logsmod
    from tests.helpers import fixture_text

    for raw in fixture_text(fixture_name).splitlines():
        ll = logsmod.parse(raw)
        if ll is not None:
            ses._on_log_line(ll, raw)
    for raw in extra_raw_lines:
        ll = logsmod.parse(raw)
        if ll is not None:
            ses._on_log_line(ll, raw)


def test_session_handler_builds_one_crash_record_and_group_from_fixture():
    ses = _harness_session()
    other_tag_line = "2026-09-15 15:03:14.000 10362 10362 I OtherTag: something else"
    _feed_fixture(ses, "logcat_crash", extra_raw_lines=[other_tag_line])
    assert len(ses.crashes) == 1
    assert ses.crashes[0].kind == "java"
    assert "FATAL EXCEPTION" in ses.crashes[0].raw
    assert len(ses.groups) == 1
    assert ses.groups[0].count == 1


def test_session_handler_am_died_line_yields_proc_event():
    import droid.logs as logsmod

    ses = _harness_session()
    raw = "2026-09-15 15:03:14.000 900 900 I ActivityManager: Process com.example.app (pid 10362) has died"
    ll = logsmod.parse(raw)
    ses._on_log_line(ll, raw)
    assert len(ses.proc_events) == 1
    assert ses.proc_events[0].kind == "died"
    assert ses.proc_events[0].pid == 10362


def test_session_handler_does_not_raise_on_am_art_fixture_lines():
    import droid.logs as logsmod
    from tests.helpers import fixture_text

    ses = _harness_session(pid=24357)
    for raw in fixture_text("logcat_am_art").splitlines():
        ll = logsmod.parse(raw)
        if ll is not None:
            ses._on_log_line(ll, raw)
    # No debe lanzar y no debe generar registros espurios de crash/ANR.
    assert ses.crashes == []
    assert ses.anrs == []
