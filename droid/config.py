"""Rutas y configuración global de droid (~/.droid)."""
import json
import os
from pathlib import Path

HOME = Path.home()
DROID_HOME = Path(os.environ.get("DROID_HOME", str(HOME / ".droid")))
LOGS_DIR = DROID_HOME / "logs"
RUN_DIR = DROID_HOME / "run"
CONFIG_FILE = DROID_HOME / "config.json"
REGISTRY_FILE = DROID_HOME / "devices.json"

DEFAULTS = {
    "adb": "",                                   # ruta a adb (vacío = autodetectar)
    "wifi_port": 5555,                           # puerto para `adb tcpip`
    "projects_dir": str(HOME / "AndroidStudioProjects"),
    "tag_width": 22,                             # ancho de la columna TAG en logs
    "reconnect": True,                           # reintentar cuando el dispositivo se pierde
    "ui_on_empty": True,                         # `droid` sin argumentos abre la app interactiva
}

_cache = None


def ensure_dirs() -> None:
    for d in (DROID_HOME, LOGS_DIR, RUN_DIR):
        d.mkdir(parents=True, exist_ok=True)


def load() -> dict:
    global _cache
    if _cache is None:
        data = {}
        if CONFIG_FILE.exists():
            try:
                data = json.loads(CONFIG_FILE.read_text())
            except Exception:
                data = {}
        _cache = {**DEFAULTS, **data}
    return _cache


def save(cfg: dict) -> None:
    global _cache
    ensure_dirs()
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
    _cache = None


def get(key: str):
    return load().get(key, DEFAULTS.get(key))
