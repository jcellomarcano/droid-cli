"""Caché de logs por dispositivo (~/.droid/logs/<modelo>-<serial>/<fecha>.log + .json)."""
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from . import config
from .adb import Device, sanitize

FLUSH_EVERY = 1.0      # segundos entre flush a disco
META_EVERY = 5.0       # segundos entre actualizaciones del .json


def device_dir(dev: Device) -> Path:
    """Directorio de caché del dispositivo. Reutiliza uno existente con la misma key aunque cambie el nombre."""
    config.ensure_dirs()
    for d in config.LOGS_DIR.iterdir():
        meta = d / "device.json"
        if d.is_dir() and meta.exists():
            try:
                if json.loads(meta.read_text()).get("key") == dev.key:
                    return d
            except Exception:
                pass
    name = f"{sanitize(dev.name)}-{dev.key}" if dev.name and sanitize(dev.name) != dev.key else dev.key
    d = config.LOGS_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "device.json").write_text(json.dumps({
        "key": dev.key, "model": dev.model, "manufacturer": dev.manufacturer, "serial": dev.hw_serial or dev.serial,
        "android": dev.android, "sdk": dev.sdk, "created": datetime.now().isoformat(timespec="seconds"),
    }, indent=2, ensure_ascii=False))
    return d


class Session:
    """Escribe el stream crudo de logcat a disco con flush periódico y metadatos."""

    def __init__(self, dev: Device, source: str, filters: Optional[dict] = None, pid: Optional[int] = None):
        self.dev = dev
        self.dir = device_dir(dev)
        self.id = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.log_path = self.dir / f"{self.id}.log"
        self.meta_path = self.dir / f"{self.id}.json"
        self.started = datetime.now()
        self.lines = 0
        self.reconnects = 0
        self.events: List[dict] = []
        self.last_ts: Optional[str] = None
        self._fh = open(self.log_path, "a", encoding="utf-8", errors="replace", buffering=1 << 16)
        self._last_flush = time.time()
        self._last_meta = 0.0
        self.meta = {
            "id": self.id, "source": source, "pid": pid or os.getpid(),
            "device": {"key": dev.key, "serial": dev.serial, "hw_serial": dev.hw_serial, "model": dev.model,
                       "manufacturer": dev.manufacturer, "android": dev.android, "sdk": dev.sdk,
                       "transport": dev.transport, "ip": dev.ip},
            "filters": filters or {}, "started": self.started.isoformat(timespec="seconds"), "ended": None,
            "lines": 0, "bytes": 0, "reconnects": 0, "last_ts": None, "events": [],
        }
        self._write_meta()

    def write(self, raw: str) -> None:
        self._fh.write(raw + "\n")
        self.lines += 1
        if raw[:4].isdigit():
            self.last_ts = raw[:23]
        now = time.time()
        if now - self._last_flush > FLUSH_EVERY:
            self._fh.flush()
            self._last_flush = now
        if now - self._last_meta > META_EVERY:
            self._write_meta()

    def set_pids(self, mapping: dict) -> None:
        """Guarda los PIDs vistos de los paquetes filtrados para poder re-filtrar la sesión después."""
        seen = self.meta.setdefault("filters", {}).setdefault("pids_seen", {})
        changed = False
        for pid, name in mapping.items():
            if str(pid) not in seen:
                seen[str(pid)] = name
                changed = True
        if changed:
            self._write_meta()

    def set_threads(self, mapping: dict) -> None:
        """Guarda nombres de hilos vistos (tid -> comm) para re-filtrar por hilo en post-mortem."""
        seen = self.meta.setdefault("filters", {}).setdefault("threads_seen", {})
        changed = False
        for tid, name in mapping.items():
            if name and seen.get(str(tid)) != name:
                seen[str(tid)] = name
                changed = True
        if changed:
            self._write_meta()

    def set_procs(self, mapping: dict) -> None:
        """Guarda nombres de proceso vistos (pid -> nombre) para identificar procesos en post-mortem."""
        seen = self.meta.setdefault("filters", {}).setdefault("procs_seen", {})
        changed = False
        for pid, name in mapping.items():
            if name and seen.get(str(pid)) != name:
                seen[str(pid)] = name
                changed = True
        if changed:
            self._write_meta()

    def note(self, text: str) -> None:
        """Marca un evento (dispositivo perdido/reconectado) dentro del log y en los metadatos."""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        self._fh.write(f"#droid {ts} {text}\n")
        self._fh.flush()
        self.events.append({"at": ts, "text": text})
        if "reconectado" in text:
            self.reconnects += 1
        self._write_meta()

    def _write_meta(self) -> None:
        self.meta.update({
            "lines": self.lines, "bytes": self._fh.tell() if not self._fh.closed else self.meta.get("bytes", 0),
            "reconnects": self.reconnects, "last_ts": self.last_ts, "events": self.events[-50:],
        })
        try:
            self.meta_path.write_text(json.dumps(self.meta, indent=2, ensure_ascii=False))
        except Exception:
            pass
        self._last_meta = time.time()

    def close(self) -> None:
        if self._fh.closed:
            return
        self._fh.flush()
        self.meta["ended"] = datetime.now().isoformat(timespec="seconds")
        self._write_meta()
        self._fh.close()


# --- Lectura -----------------------------------------------------------------

@dataclass
class SessionInfo:
    device_dir: Path
    id: str
    log_path: Path
    meta: dict

    @property
    def size(self) -> int:
        try:
            return self.log_path.stat().st_size
        except OSError:
            return 0

    @property
    def started(self) -> Optional[datetime]:
        s = self.meta.get("started")
        try:
            return datetime.fromisoformat(s) if s else datetime.fromtimestamp(self.log_path.stat().st_mtime)
        except Exception:
            return None

    @property
    def ended(self) -> Optional[datetime]:
        s = self.meta.get("ended")
        try:
            return datetime.fromisoformat(s) if s else None
        except Exception:
            return None

    @property
    def live(self) -> bool:
        if self.ended:
            return False
        pid = self.meta.get("pid")
        if not pid:
            return False
        try:
            os.kill(int(pid), 0)
            return True
        except OSError:
            return False

    def duration(self) -> str:
        a, b = self.started, self.ended or (datetime.now() if self.live else None)
        if not a:
            return ""
        if b is None:
            try:
                b = datetime.fromtimestamp(self.log_path.stat().st_mtime)
            except OSError:
                return ""
        secs = int((b - a).total_seconds())
        if secs < 60:
            return f"{secs}s"
        if secs < 3600:
            return f"{secs // 60}m{secs % 60:02d}s"
        return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"


@dataclass
class CachedDevice:
    dir: Path
    info: dict
    sessions: List[SessionInfo]

    @property
    def key(self) -> str:
        return self.info.get("key", self.dir.name)

    @property
    def name(self) -> str:
        return self.info.get("model") or self.dir.name

    @property
    def total_size(self) -> int:
        return sum(s.size for s in self.sessions)


def list_cached() -> List[CachedDevice]:
    config.ensure_dirs()
    out = []
    for d in sorted(config.LOGS_DIR.iterdir()):
        if not d.is_dir():
            continue
        info = {}
        if (d / "device.json").exists():
            try:
                info = json.loads((d / "device.json").read_text())
            except Exception:
                info = {}
        sessions = []
        for log in sorted(d.glob("*.log"), reverse=True):
            meta = {}
            mp = log.with_suffix(".json")
            if mp.exists():
                try:
                    meta = json.loads(mp.read_text())
                except Exception:
                    meta = {}
            sessions.append(SessionInfo(d, log.stem, log, meta))
        if sessions or info:
            out.append(CachedDevice(d, info, sessions))
    out.sort(key=lambda c: (c.sessions[0].started if c.sessions and c.sessions[0].started else datetime.min), reverse=True)
    return out


def find_device(target: Optional[str]) -> Optional[CachedDevice]:
    cached = list_cached()
    if not cached:
        return None
    if not target or target == "latest":
        return cached[0]
    t = target.lower()
    from . import registry
    alias_keys = {e.get("alias", "").lower(): k for k, e in registry.load().items() if e.get("alias")}
    if t in alias_keys:
        for c in cached:
            if c.key == alias_keys[t]:
                return c
    if t.isdigit():
        idx = int(t)
        if 1 <= idx <= len(cached):
            return cached[idx - 1]
    for c in cached:
        vals = [c.key.lower(), c.dir.name.lower(), str(c.info.get("serial", "")).lower(), c.name.lower()]
        if t in vals:
            return c
    for c in cached:
        if any(t in v for v in (c.key.lower(), c.dir.name.lower(), c.name.lower())):
            return c
    return None


def find_session(dev: CachedDevice, session: Optional[str]) -> Optional[SessionInfo]:
    if not dev.sessions:
        return None
    if not session or session == "latest":
        return dev.sessions[0]
    if session.isdigit() and 1 <= int(session) <= len(dev.sessions):
        return dev.sessions[int(session) - 1]
    for s in dev.sessions:
        if s.id == session or s.id.startswith(session):
            return s
    return None


def parse_age(spec: str) -> timedelta:
    m = re.fullmatch(r"(\d+)\s*([smhdw])", spec.strip().lower())
    if not m:
        raise ValueError("Formato de edad no válido; usa p.ej. 30d, 12h, 2w")
    n, unit = int(m.group(1)), m.group(2)
    names = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}
    return timedelta(**{names[unit]: n})
