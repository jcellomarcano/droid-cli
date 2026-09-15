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
