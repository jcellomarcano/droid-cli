"""INV-01: LogcatStream no pierde ni reordena lineas al reconectar con -T <last_ts>.

El adb real, medido hoy contra un emulador (`adb logcat -T "<ts>"`), RE-EMITE la
linea que tiene exactamente ese timestamp al reconectar. Los fakes de aqui
reproducen ese comportamiento; droid/logs.py todavia NO deduplica esa linea
repetida (eso se corrige en un cambio aparte de este mismo release), asi que
`test_logcat_stream_reconnect_boundary_line_is_not_deduped_yet` esta escrita
para fallar contra el arbol actual (ver docstring de ese test).
"""
import json
import stat
from pathlib import Path

import pytest

import droid.adb as adbmod
from droid import cache
from droid.adb import Device
from droid.logs import LogcatStream

# El fake escribe cada invocacion de argv como una linea JSON (un elemento de
# lista por argumento), no como "$*" con split por espacios: asi un timestamp
# con espacio interno ("2026-09-15 10:00:00.300") sigue siendo UN solo
# argumento y se puede comprobar con precision cual valor siguio a "-T".
_ARGV_JSON_HELPER = r"""
args_json="["
first=1
for a in "$@"; do
  esc=$(printf '%s' "$a" | sed 's/\\/\\\\/g; s/"/\\"/g')
  if [[ $first -eq 1 ]]; then args_json+="\"$esc\""; first=0; else args_json+=",\"$esc\""; fi
done
args_json+="]"
echo "$args_json" >> "$ARGV_FILE"
"""

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
COUNT=0
if [[ -f "$STATE_FILE" ]]; then
  COUNT=$(cat "$STATE_FILE")
fi
""" + _ARGV_JSON_HELPER + """
NEXT=$((COUNT + 1))
echo "$NEXT" > "$STATE_FILE"
if [[ "$COUNT" == "0" ]]; then
  echo "2026-09-15 10:00:00.100  1000  1000 I Tag1: line one"
  echo "2026-09-15 10:00:00.200  1000  1000 I Tag1: line two"
  echo "2026-09-15 10:00:00.300  1000  1000 I Tag1: line three"
  exit 0
fi
echo "2026-09-15 10:00:00.400  1000  1000 I Tag1: line four"
echo "2026-09-15 10:00:00.500  1000  1000 I Tag1: line five"
exit 0
"""

# Variante fiel al adb real: re-emite la linea .300 en la SEGUNDA invocacion
# (antes de .400/.500) y el primer batch trae DOS lineas con timestamp .300
# pero texto distinto (ambas deben conservarse: no es correcto deduplicar por
# timestamp desnudo).
FAKE_ADB_DUP_TS = """#!/bin/bash
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
COUNT=0
if [[ -f "$STATE_FILE" ]]; then
  COUNT=$(cat "$STATE_FILE")
fi
""" + _ARGV_JSON_HELPER + """
NEXT=$((COUNT + 1))
echo "$NEXT" > "$STATE_FILE"
if [[ "$COUNT" == "0" ]]; then
  echo "2026-09-15 10:00:00.100  1000  1000 I Tag1: line one"
  echo "2026-09-15 10:00:00.200  1000  1000 I Tag1: line two"
  echo "2026-09-15 10:00:00.300  1000  1000 I Tag1: line three"
  echo "2026-09-15 10:00:00.300  1000  1000 I Tag1: line three-bis"
  exit 0
fi
echo "2026-09-15 10:00:00.300  1000  1000 I Tag1: line three"
echo "2026-09-15 10:00:00.400  1000  1000 I Tag1: line four"
echo "2026-09-15 10:00:00.500  1000  1000 I Tag1: line five"
exit 0
"""

FAKE_ADB_SILENT = """#!/bin/bash
exit 0
"""


def _write_fake_adb(tmp_path: Path, script: str, state_file: Path, argv_file: Path) -> Path:
    adb_path = tmp_path / "adb"
    adb_path.write_text(script.format(state_file=state_file, argv_file=argv_file))
    adb_path.chmod(adb_path.stat().st_mode | stat.S_IEXEC)
    return adb_path


def _fast_logs_time(monkeypatch):
    """N11: no se parchea time.sleep del stdlib global (afectaria a todo el
    proceso de test). En vez de eso se reemplaza el NOMBRE de modulo `time`
    dentro del namespace de droid.logs (import module-level en droid/logs.py)
    por un stand-in cuyo .sleep() es instantaneo y cuyo .time() delega al
    time.time() real -- se documenta aqui cual nombre se parchea: droid.logs.time."""
    import time as real_time

    import droid.logs as logsmod

    class _FastTime:
        def sleep(self, _seconds):
            return None

        def time(self):
            return real_time.time()

    monkeypatch.setattr(logsmod, "time", _FastTime())


@pytest.mark.allow_adb
def test_logcat_stream_resumes_with_last_ts_without_loss_or_reorder(tmp_path, monkeypatch):
    state_file = tmp_path / "state"
    argv_file = tmp_path / "argv.log"
    fake_adb = _write_fake_adb(tmp_path, FAKE_ADB, state_file, argv_file)

    monkeypatch.setattr(adbmod, "adb_path", lambda: str(fake_adb))

    dev = Device(serial="EMU123", state="device", transport="emu", model="Pixel_8_Pro")
    monkeypatch.setattr(adbmod, "list_devices", lambda details=False: [dev])

    _fast_logs_time(monkeypatch)

    session = cache.Session(dev, source="record")
    stream = LogcatStream(dev.serial, dev.key, reconnect=True)

    collected = []
    for kind, payload in stream.stream():
        if kind == "line":
            collected.append(payload)
            session.write(payload)
        elif kind in ("lost", "reconnected", "end"):
            session.note(payload)
        if len(collected) >= 5:
            stream.stopped = True
            break

    assert collected == [
        "2026-09-15 10:00:00.100  1000  1000 I Tag1: line one",
        "2026-09-15 10:00:00.200  1000  1000 I Tag1: line two",
        "2026-09-15 10:00:00.300  1000  1000 I Tag1: line three",
        "2026-09-15 10:00:00.400  1000  1000 I Tag1: line four",
        "2026-09-15 10:00:00.500  1000  1000 I Tag1: line five",
    ]

    argv_calls = [json.loads(ln) for ln in argv_file.read_text().splitlines()]
    assert len(argv_calls) == 2
    second_call = argv_calls[1]
    assert "-T" in second_call
    assert second_call[second_call.index("-T") + 1] == "2026-09-15 10:00:00.300"

    session.close()
    log_text = session.log_path.read_text(encoding="utf-8")
    droid_lines = [ln for ln in log_text.splitlines() if ln.startswith("#droid")]
    assert droid_lines, "esperaba al menos una linea #droid marcando la reconexion"

    meta = json.loads(session.meta_path.read_text(encoding="utf-8"))
    assert meta["reconnects"] >= 1


@pytest.mark.allow_adb
def test_logcat_stream_negative_control_silent_adb_yields_no_lines(tmp_path, monkeypatch):
    fake_adb = tmp_path / "adb"
    fake_adb.write_text(FAKE_ADB_SILENT)
    fake_adb.chmod(fake_adb.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setattr(adbmod, "adb_path", lambda: str(fake_adb))

    dev = Device(serial="EMU123", state="device", transport="emu", model="Pixel_8_Pro")

    stream = LogcatStream(dev.serial, dev.key, reconnect=False)
    collected = [payload for kind, payload in stream.stream() if kind == "line"]
    assert collected == []


@pytest.mark.allow_adb
def test_logcat_stream_reconnect_skips_the_boundary_line_logcat_reemits(tmp_path, monkeypatch):
    """N4: adb real re-emite la linea con el timestamp exacto pasado a `-T` al
    reconectar (medido en un emulador). LogcatStream debe entregar las 6 lineas
    distintas en orden, sin la reemision, y conservar las dos lineas distintas
    que comparten el milisegundo de la frontera."""
    state_file = tmp_path / "state"
    argv_file = tmp_path / "argv.log"
    fake_adb = _write_fake_adb(tmp_path, FAKE_ADB_DUP_TS, state_file, argv_file)

    monkeypatch.setattr(adbmod, "adb_path", lambda: str(fake_adb))

    dev = Device(serial="EMU123", state="device", transport="emu", model="Pixel_8_Pro")
    monkeypatch.setattr(adbmod, "list_devices", lambda details=False: [dev])

    _fast_logs_time(monkeypatch)

    stream = LogcatStream(dev.serial, dev.key, reconnect=True)
    collected = []
    for kind, payload in stream.stream():
        if kind == "line":
            collected.append(payload)
        if payload.endswith("line five") or len(collected) >= 7:
            stream.stopped = True
            break

    argv_calls = [json.loads(ln) for ln in argv_file.read_text().splitlines()]
    second_call = argv_calls[1]
    assert second_call[second_call.index("-T") + 1] == "2026-09-15 10:00:00.300"

    distinct_in_order = list(dict.fromkeys(collected))
    assert distinct_in_order == [
        "2026-09-15 10:00:00.100  1000  1000 I Tag1: line one",
        "2026-09-15 10:00:00.200  1000  1000 I Tag1: line two",
        "2026-09-15 10:00:00.300  1000  1000 I Tag1: line three",
        "2026-09-15 10:00:00.300  1000  1000 I Tag1: line three-bis",
        "2026-09-15 10:00:00.400  1000  1000 I Tag1: line four",
        "2026-09-15 10:00:00.500  1000  1000 I Tag1: line five",
    ]
    assert collected == distinct_in_order, (
        "LogcatStream.stream() todavia repite la linea del limite de reconexion "
        f"(recibidas {len(collected)}, distintas {len(distinct_in_order)}); "
        "esto se corrige en un cambio aparte de droid/logs.py."
    )
