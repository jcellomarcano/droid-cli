"""Meta-tests del guardia de adb real (conftest._block_real_adb).

Verifica que el guardia actua a nivel de subprocess.Popen/run (no solo por nombre
de funcion), asi que atrapa tambien invocaciones que no pasan por droid.adb.run/shell,
como el Popen directo de LogcatStream._launch.
"""
import stat

import pytest

import droid.adb as adbmod
from droid.logs import LogcatStream


def _write_fake_adb(tmp_path):
    adb_path = tmp_path / "adb"
    adb_path.write_text("#!/bin/bash\necho device\nexit 0\n")
    adb_path.chmod(adb_path.stat().st_mode | stat.S_IEXEC)
    return adb_path


def test_logcat_stream_without_marker_raises_on_real_adb_popen(tmp_path, monkeypatch):
    fake_adb = _write_fake_adb(tmp_path)
    monkeypatch.setattr(adbmod, "adb_path", lambda: str(fake_adb))

    stream = LogcatStream("EMU123", "key1", reconnect=False)
    with pytest.raises(RuntimeError, match="adb real bloqueado"):
        next(iter(stream.stream()))


@pytest.mark.allow_adb
def test_logcat_stream_with_marker_allows_real_adb_popen(tmp_path, monkeypatch):
    fake_adb = _write_fake_adb(tmp_path)
    monkeypatch.setattr(adbmod, "adb_path", lambda: str(fake_adb))

    stream = LogcatStream("EMU123", "key1", reconnect=False)
    kinds = [kind for kind, _payload in stream.stream()]
    assert kinds and kinds[-1] == "end"
