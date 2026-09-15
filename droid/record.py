"""Grabación en background de logcat a la caché (sobrevive al cierre de la terminal)."""
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from typing import List, Optional

from . import config
from .adb import Device


def pidfile(key: str):
    return config.RUN_DIR / f"{key}.json"


def status_of(key: str) -> Optional[dict]:
    p = pidfile(key)
    if not p.exists():
        return None
    try:
        info = json.loads(p.read_text())
        os.kill(int(info["pid"]), 0)
        return info
    except (OSError, ValueError, KeyError):
        try:
            p.unlink()
        except OSError:
            pass
        return None


def all_status() -> List[dict]:
    config.ensure_dirs()
    out = []
    for p in sorted(config.RUN_DIR.glob("*.json")):
        info = status_of(p.stem)
        if info:
            out.append(info)
    return out


def start(dev: Device, buffers: Optional[List[str]] = None) -> dict:
    config.ensure_dirs()
    if status_of(dev.key):
        raise RuntimeError(f"ya hay una grabación activa para {dev.display} ({dev.key})")
    errlog = config.RUN_DIR / f"{dev.key}.err"
    cmd = [sys.executable, "-m", "droid", "_worker", "--device-json", json.dumps(dev.to_dict())]
    for b in buffers or []:
        cmd += ["-b", b]
    with open(errlog, "a") as fh:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=fh, stdin=subprocess.DEVNULL,
                                start_new_session=True, close_fds=True)
    info = {"pid": proc.pid, "key": dev.key, "serial": dev.serial, "model": dev.model, "alias": dev.alias,
            "started": datetime.now().isoformat(timespec="seconds"), "session": None}
    pidfile(dev.key).write_text(json.dumps(info, indent=2))
    # espera a que el worker publique la ruta de su sesión
    for _ in range(20):
        time.sleep(0.15)
        cur = status_of(dev.key)
        if cur and cur.get("session"):
            return cur
    return info


def stop(key: str, timeout: float = 5) -> bool:
    info = status_of(key)
    if not info:
        return False
    pid = int(info["pid"])
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    end = time.time() + timeout
    while time.time() < end:
        try:
            os.kill(pid, 0)
            time.sleep(0.1)
        except OSError:
            break
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        pidfile(key).unlink()
    except OSError:
        pass
    return True


def worker_main(dev: Device, buffers: List[str]) -> int:
    """Proceso en background: graba a la caché con reconexión infinita."""
    from . import cache
    from .logs import LogcatStream

    session = cache.Session(dev, source="record", pid=os.getpid())
    info_path = pidfile(dev.key)
    try:
        info = json.loads(info_path.read_text())
    except Exception:
        info = {"pid": os.getpid(), "key": dev.key, "serial": dev.serial, "model": dev.model}
    info["session"] = str(session.log_path)
    info_path.write_text(json.dumps(info, indent=2))

    stream = LogcatStream(dev.serial, dev.key, buffers=buffers, reconnect=True)

    def _term(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    session.note(f"grabación iniciada ({dev.label()})")
    try:
        for kind, payload in stream.stream():
            if kind == "line":
                session.write(payload)
            elif kind in ("lost", "reconnected", "end"):
                session.note(payload)
    except KeyboardInterrupt:
        pass
    finally:
        stream.close()
        session.note("grabación detenida")
        session.close()
        try:
            info_path.unlink()
        except OSError:
            pass
    return 0
