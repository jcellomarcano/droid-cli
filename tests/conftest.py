"""Fixtures compartidos: aislamiento de estado de droid, bloqueo de adb real y helpers reexportados."""
import os
import subprocess
from typing import Callable

import pytest

from tests.helpers import FIXTURES_DIR, fixture_text

__all__ = ["FIXTURES_DIR", "fixture_text", "fake_shell"]

# El marcador "allow_adb" se declara en pyproject.toml ([tool.pytest.ini_options]
# markers=...) junto con --strict-markers; no se duplica aquí.


@pytest.fixture(autouse=True)
def _isolate_droid_state(tmp_path, monkeypatch):
    from droid import clip as clipmod
    from droid import config as droidconfig
    from droid import inspector as inspectormod
    from droid import suggest as suggestmod

    home = tmp_path / ".droid"
    monkeypatch.setattr(droidconfig, "DROID_HOME", home)
    monkeypatch.setattr(droidconfig, "LOGS_DIR", home / "logs")
    monkeypatch.setattr(droidconfig, "RUN_DIR", home / "run")
    monkeypatch.setattr(droidconfig, "CONFIG_FILE", home / "config.json")
    monkeypatch.setattr(droidconfig, "REGISTRY_FILE", home / "devices.json")
    monkeypatch.setattr(droidconfig, "_cache", None)
    # Constantes derivadas de config.DROID_HOME calculadas en import-time: no
    # siguen el DROID_HOME parcheado arriba salvo que se parcheen también aqui.
    monkeypatch.setattr(inspectormod, "INSPECT_DIR", home / "inspect")
    monkeypatch.setattr(clipmod, "CLIPS_DIR", home / "clips")
    monkeypatch.setattr(suggestmod, "HISTORY_FILE", home / "history.json")
    yield


def _argv0_is_adb(argv0) -> bool:
    try:
        name = os.path.basename(str(argv0))
    except Exception:
        return False
    return name == "adb" or str(argv0).endswith("/adb")


def _blocked(*args, **kwargs):
    raise RuntimeError("adb real bloqueado en tests: usa fake_shell o @pytest.mark.allow_adb")


@pytest.fixture(autouse=True)
def _block_real_adb(request, monkeypatch):
    """Bloquea cualquier invocacion de un binario llamado 'adb' via subprocess.Popen/run,
    sin importar por que capa de droid pase (droid.adb.run/shell, LogcatStream.Popen directo,
    etc.), salvo que el test tenga @pytest.mark.allow_adb."""
    if request.node.get_closest_marker("allow_adb"):
        yield
        return

    real_popen = subprocess.Popen
    real_run = subprocess.run

    def _argv_of(args):
        if isinstance(args, (list, tuple)) and args:
            return args[0]
        return args

    def fake_popen(args, *a, **kw):
        if _argv0_is_adb(_argv_of(args)):
            raise RuntimeError("adb real bloqueado en tests: usa fake_shell o @pytest.mark.allow_adb")
        return real_popen(args, *a, **kw)

    def fake_run(args, *a, **kw):
        if _argv0_is_adb(_argv_of(args)):
            raise RuntimeError("adb real bloqueado en tests: usa fake_shell o @pytest.mark.allow_adb")
        return real_run(args, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(subprocess, "run", fake_run)

    # Ademas de la capa subprocess, bloquea las funciones de conveniencia de
    # droid.adb por si algun caller las invoca sin pasar por subprocess directamente.
    import droid.adb as adbmod

    monkeypatch.setattr(adbmod, "run", _blocked)
    monkeypatch.setattr(adbmod, "shell", _blocked)
    yield


@pytest.fixture
def fake_shell(monkeypatch):
    import droid.adb as adbmod

    def install(response) -> Callable:
        def shell(serial, cmd, timeout=15):
            return response(cmd) if callable(response) else response

        monkeypatch.setattr(adbmod, "shell", shell)
        return shell

    return install
