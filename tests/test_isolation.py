"""B18: constantes derivadas de config.DROID_HOME calculadas en import-time
(INSPECT_DIR, CLIPS_DIR, HISTORY_FILE, ...) deben seguir el aislamiento de
_isolate_droid_state (tests/conftest.py), no solo config.DROID_HOME en si."""
from pathlib import Path

from droid import clip as clipmod
from droid import config as droidconfig
from droid import inspector as inspectormod
from droid import suggest as suggestmod


def test_derived_home_constants_live_under_tmp_path(tmp_path):
    # El fixture autouse _isolate_droid_state (tests/conftest.py) ya parcheo
    # droidconfig.DROID_HOME a tmp_path / ".droid" para este test.
    home = droidconfig.DROID_HOME
    assert home == tmp_path / ".droid"
    assert home.name == ".droid"
    assert inspectormod.INSPECT_DIR == home / "inspect"
    assert clipmod.CLIPS_DIR == home / "clips"
    assert suggestmod.HISTORY_FILE == home / "history.json"
    for path in (inspectormod.INSPECT_DIR, clipmod.CLIPS_DIR, suggestmod.HISTORY_FILE, droidconfig.LOGS_DIR,
                 droidconfig.RUN_DIR, droidconfig.CONFIG_FILE, droidconfig.REGISTRY_FILE):
        assert _is_under(path, home), f"{path} no vive bajo el DROID_HOME aislado ({home})"


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
