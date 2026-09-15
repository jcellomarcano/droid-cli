"""INV-01: reconexion de logcat no duplica la linea frontera del -T."""
import stat
import time
from pathlib import Path

import pytest

import droid.adb as adbmod
from droid.adb import Device
from droid.logs import LogcatStream, is_replayed

TS = "2026-09-15 15:56:31.118"


def test_is_replayed_true_for_identical_raw_at_boundary():
    raw = f"{TS}  1000  1000 I Tag1: dup"
    seen = {raw}
    assert is_replayed(raw, TS, TS, seen) is True


def test_is_replayed_two_different_raws_same_ts_both_pass():
    raw_a = f"{TS}  1000  1000 I Tag1: a"
    raw_b = f"{TS}  1000  1000 I Tag1: b"
    seen = {raw_a}
    assert is_replayed(raw_b, TS, TS, seen) is False


def test_is_replayed_later_ts_resets():
    raw = f"{TS}  1000  1000 I Tag1: a"
    later_ts = "2026-09-15 15:56:31.119"
    seen = {raw}
    assert is_replayed(raw, later_ts, TS, seen) is False


def test_is_replayed_unparsable_raw_never_filtered():
    raw = "not a logcat line at all"
    seen = {raw}
    assert is_replayed(raw, None, TS, seen) is False


FAKE_ADB = """#!/bin/bash
STATE_FILE="{state_file}"
ARGV_FILE="{argv_file}"
if [[ "$1" == "wait-for-device" ]]; then
  echo device
  exit 0
fi
if [[ "$*" == *"get-state"* ]]; then
  echo device
  exit 0
fi
echo "$*" >> "$ARGV_FILE"
if [[ "$*" == *"-T"* ]]; then
  echo "2026-09-15 15:56:31.300  1000  1000 I Tag1: line 300a"
  echo "2026-09-15 15:56:31.300  1000  1000 I Tag1: line 300b"
  echo "2026-09-15 15:56:31.400  1000  1000 I Tag1: line 400"
  echo "2026-09-15 15:56:31.500  1000  1000 I Tag1: line 500"
  exit 0
fi
echo "2026-09-15 15:56:31.100  1000  1000 I Tag1: line 100"
echo "2026-09-15 15:56:31.200  1000  1000 I Tag1: line 200"
echo "2026-09-15 15:56:31.300  1000  1000 I Tag1: line 300a"
echo "2026-09-15 15:56:31.300  1000  1000 I Tag1: line 300b"
exit 0
"""


def _write_fake_adb(tmp_path: Path, state_file: Path, argv_file: Path) -> Path:
    adb_path = tmp_path / "adb"
    adb_path.write_text(FAKE_ADB.format(state_file=state_file, argv_file=argv_file))
    adb_path.chmod(adb_path.stat().st_mode | stat.S_IEXEC)
    return adb_path


@pytest.mark.allow_adb
def test_reconnect_does_not_duplicate_boundary_line(tmp_path, monkeypatch):
    state_file = tmp_path / "state"
    argv_file = tmp_path / "argv.log"
    fake_adb = _write_fake_adb(tmp_path, state_file, argv_file)

    monkeypatch.setattr(adbmod, "adb_path", lambda: str(fake_adb))

    dev = Device(serial="EMU123", state="device", transport="emu", model="Pixel_8_Pro")
    monkeypatch.setattr(adbmod, "list_devices", lambda details=False: [dev])

    statuses = []
    stream = LogcatStream(dev.serial, dev.key, reconnect=True, status=lambda kind, text: statuses.append((kind, text)))
    stream._reconnect_poll_interval = 0.01

    collected = []
    deadline = time.time() + 5
    for kind, payload in stream.stream():
        if kind == "line":
            collected.append(payload)
        if len(collected) >= 6 or time.time() > deadline:
            stream.stopped = True
            break

    assert collected == [
        "2026-09-15 15:56:31.100  1000  1000 I Tag1: line 100",
        "2026-09-15 15:56:31.200  1000  1000 I Tag1: line 200",
        "2026-09-15 15:56:31.300  1000  1000 I Tag1: line 300a",
        "2026-09-15 15:56:31.300  1000  1000 I Tag1: line 300b",
        "2026-09-15 15:56:31.400  1000  1000 I Tag1: line 400",
        "2026-09-15 15:56:31.500  1000  1000 I Tag1: line 500",
    ]
    assert len(collected) == len(set(collected))


FAKE_ADB_SAME_MS = """#!/bin/bash
if [[ "$1" == "wait-for-device" ]]; then echo device; exit 0; fi
if [[ "$*" == *"get-state"* ]]; then echo device; exit 0; fi
echo "2026-09-15 15:56:31.300  1000  1000 I Tag1: same"
echo "2026-09-15 15:56:31.300  1000  1000 I Tag1: same"
echo "2026-09-15 15:56:31.300  1000  1000 I Tag1: same"
echo "2026-09-15 15:56:31.400  1000  1000 I Tag1: next"
exit 0
"""


@pytest.mark.allow_adb
def test_identical_lines_in_the_same_millisecond_are_all_delivered_without_reconnect(tmp_path, monkeypatch):
    adb_path = tmp_path / "adb"
    adb_path.write_text(FAKE_ADB_SAME_MS)
    adb_path.chmod(adb_path.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(adbmod, "adb_path", lambda: str(adb_path))
    stream = LogcatStream("EMU123", "k", reconnect=False)
    lines = [payload for kind, payload in stream.stream() if kind == "line"]
    assert lines.count("2026-09-15 15:56:31.300  1000  1000 I Tag1: same") == 3
    assert lines[-1].endswith("next")


def test_ts_re_accepts_lines_without_year():
    from droid.logs import TS_RE
    assert TS_RE.match("09-15 10:00:00.300  1 1 I T: x").group(1) == "09-15 10:00:00.300"
    assert TS_RE.match("2026-09-15 10:00:00.300  1 1 I T: x").group(1) == "2026-09-15 10:00:00.300"
    assert TS_RE.match("garbage") is None


def test_boundary_is_inherited_by_a_new_stream_only_when_it_matches_since():
    s = LogcatStream("S", "k", reconnect=False, since="2026-09-15 10:00:00.300",
                     boundary=("2026-09-15 10:00:00.300", {"a"}))
    assert s._boundary_armed is True
    assert s.boundary() == ("2026-09-15 10:00:00.300", {"a"})
    s2 = LogcatStream("S", "k", reconnect=False, since="2026-09-15 10:00:00.400",
                      boundary=("2026-09-15 10:00:00.300", {"a"}))
    assert s2._boundary_armed is False
    assert LogcatStream("S", "k", reconnect=False).boundary() is None
