"""Registro de dispositivos conocidos (~/.droid/devices.json): IP WiFi, alias, último visto."""
import json
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from . import config
from .adb import Device


def load() -> Dict[str, dict]:
    if config.REGISTRY_FILE.exists():
        try:
            return json.loads(config.REGISTRY_FILE.read_text())
        except Exception:
            return {}
    return {}


def save(data: Dict[str, dict]) -> None:
    config.ensure_dirs()
    config.REGISTRY_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def upsert(dev: Device, ip: Optional[str] = None, port: Optional[int] = None, **extra) -> dict:
    data = load()
    entry = data.get(dev.key, {})
    entry.update({
        "key": dev.key,
        "serial": dev.hw_serial or dev.serial,
        "model": dev.model or entry.get("model", ""),
        "manufacturer": dev.manufacturer or entry.get("manufacturer", ""),
        "android": dev.android or entry.get("android", ""),
        "sdk": dev.sdk or entry.get("sdk", ""),
        "last_seen": datetime.now().isoformat(timespec="seconds"),
    })
    if ip:
        entry["ip"] = ip
    elif dev.ip and not entry.get("ip"):
        entry["ip"] = dev.ip
    if port:
        entry["port"] = port
    entry.setdefault("alias", "")
    entry.update(extra)
    data[dev.key] = entry
    save(data)
    return entry


def touch_all(devices: List[Device]) -> None:
    """Actualiza last_seen/IP de los dispositivos online sin sobreescribir puertos ya guardados."""
    data = load()
    changed = False
    for d in devices:
        if not d.online or d.transport == "emu":
            continue
        entry = data.get(d.key)
        if entry is None:
            continue
        entry["last_seen"] = datetime.now().isoformat(timespec="seconds")
        if d.ip:
            entry["ip"] = d.ip
        if d.model:
            entry["model"] = d.model
        changed = True
    if changed:
        save(data)


def set_alias(key: str, alias: str) -> None:
    data = load()
    entry = data.setdefault(key, {"key": key})
    entry["alias"] = alias
    save(data)


def remove(key: str) -> bool:
    data = load()
    if key in data:
        del data[key]
        save(data)
        return True
    return False


def apply_aliases(devices: List[Device]) -> None:
    data = load()
    for d in devices:
        e = data.get(d.key)
        if e:
            d.alias = e.get("alias", "") or ""
            if not d.model and e.get("model"):
                d.model = e["model"]


def hostport(entry: dict) -> str:
    return f"{entry.get('ip')}:{entry.get('port', config.get('wifi_port'))}"


def find(target: str) -> List[Tuple[str, dict]]:
    """Busca por alias, key, serial, modelo o IP (case-insensitive, substring en modelo)."""
    t = target.lower().strip()
    hits = []
    for key, e in load().items():
        vals = {str(e.get(k, "")).lower() for k in ("alias", "key", "serial", "ip")}
        if t in vals or hostport(e).lower() == t:
            hits.append((key, e))
        elif t and (t in str(e.get("model", "")).lower() or key.lower().startswith(t)):
            hits.append((key, e))
    return hits
