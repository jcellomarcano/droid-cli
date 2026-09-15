"""Wrapper de adb: descubrimiento del binario, listado de dispositivos con detalles y utilidades WiFi."""
import os
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, asdict
from typing import List, Optional, Dict

from . import config

STATE_ONLINE = "device"
KNOWN_KV = ("usb", "product", "model", "device", "transport_id")
MDNS_RE = re.compile(r"^adb-(.+)-[A-Za-z0-9]+\._adb-tls-connect")
HOSTPORT_RE = re.compile(r"^[\w.\-]+:\d+$")
SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]+")


class AdbError(Exception):
    pass


_ADB: Optional[str] = None


def adb_path() -> str:
    global _ADB
    if _ADB:
        return _ADB
    cands: List[str] = []
    cfg_adb = config.get("adb")
    if cfg_adb:
        cands.append(os.path.expanduser(cfg_adb))
    for env in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        v = os.environ.get(env)
        if v:
            cands.append(os.path.join(v, "platform-tools", "adb"))
    w = shutil.which("adb")
    if w:
        cands.append(w)
    cands += [
        os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"),
        "/opt/android-sdk/platform-tools/adb",
        "/opt/homebrew/bin/adb",
        "/usr/local/bin/adb",
    ]
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            _ADB = c
            return c
    raise AdbError(
        "No encuentro el binario adb. Instala platform-tools o indica la ruta con: droid config adb /ruta/a/adb"
    )


def run(args, serial: Optional[str] = None, timeout: float = 20, input: Optional[str] = None) -> subprocess.CompletedProcess:
    cmd = [adb_path()]
    if serial:
        cmd += ["-s", serial]
    cmd += list(args)
    try:
        return subprocess.run(cmd, capture_output=True, text=True, errors="replace", timeout=timeout, input=input)
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        err = e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
        return subprocess.CompletedProcess(cmd, 124, out, err + f"\n[timeout tras {timeout}s]")


def shell(serial: str, cmd: str, timeout: float = 15) -> str:
    return run(["shell", cmd], serial=serial, timeout=timeout).stdout


def exec_out(serial: str, args: List[str], dest, timeout: float = 120) -> subprocess.CompletedProcess:
    """`adb -s <serial> exec-out <args...>` con stdout volcado a `dest`. Lanza AdbError si rc != 0."""
    cmd = [adb_path(), "-s", serial, "exec-out", *args]
    with open(dest, "wb") as fh:
        r = subprocess.run(cmd, stdout=fh, stderr=subprocess.PIPE, timeout=timeout)
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace") if isinstance(r.stderr, bytes) else (r.stderr or "")
        raise AdbError(f"exec-out falló: {err.strip()[:300]}")
    return r


def sanitize(s: str) -> str:
    return SANITIZE_RE.sub("_", s).strip("_") or "unknown"


def mdns_serial(serial: str) -> str:
    m = MDNS_RE.match(serial)
    return m.group(1) if m else ""


def is_hostport(serial: str) -> bool:
    return bool(HOSTPORT_RE.match(serial))


def classify_transport(serial: str, has_usb: bool) -> str:
    if serial.startswith("emulator-"):
        return "emu"
    if has_usb:
        return "usb"
    if is_hostport(serial) or "_adb-tls-connect" in serial:
        return "wifi"
    return "?"


@dataclass
class Device:
    serial: str
    state: str
    transport: str = "?"        # usb | wifi | emu | ?
    model: str = ""
    manufacturer: str = ""
    product: str = ""
    android: str = ""
    sdk: str = ""
    hw_serial: str = ""
    ip: str = ""
    ifaces: str = ""
    battery: str = ""
    transport_id: str = ""
    alias: str = ""

    @property
    def online(self) -> bool:
        return self.state == STATE_ONLINE

    @property
    def key(self) -> str:
        """Identificador estable del dispositivo físico (igual por USB y por WiFi)."""
        if self.transport == "emu":
            return sanitize(self.serial)
        base = self.hw_serial or mdns_serial(self.serial) or self.serial
        return sanitize(base)

    @property
    def name(self) -> str:
        return self.model or self.product or self.serial

    @property
    def display(self) -> str:
        return self.alias or self.name

    def label(self) -> str:
        via = {"usb": "USB", "wifi": "WiFi", "emu": "Emulador"}.get(self.transport, "?")
        return f"{self.display} · {self.serial} · {via}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["key"] = self.key
        d["online"] = self.online
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Device":
        fields = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**fields)


_DETAIL_SCRIPT = (
    'echo "MODEL=$(getprop ro.product.model)";'
    'echo "MANUF=$(getprop ro.product.manufacturer)";'
    'echo "REL=$(getprop ro.build.version.release)";'
    'echo "SDK=$(getprop ro.build.version.sdk)";'
    'echo "SERIAL=$(getprop ro.serialno)";'
    "echo \"BATT=$(dumpsys battery 2>/dev/null | grep -m1 ' level:' | tr -dc 0-9)\";"
    "echo \"IPS=$(ip -o -4 addr show 2>/dev/null | awk '{print $2\"=\"$4}' | tr '\\n' ' ')\""
)

_IFACE_PREF = ("wlan", "eth", "swlan", "ap")
_IFACE_SKIP = ("lo", "rmnet", "dummy", "tun", "ccmni", "p2p", "docker", "vpn")


def pick_ip(ifaces: str) -> str:
    entries = []
    for tok in ifaces.split():
        if "=" not in tok:
            continue
        name, _, cidr = tok.partition("=")
        ip = cidr.split("/")[0]
        if any(name.startswith(s) for s in _IFACE_SKIP):
            continue
        entries.append((name, ip))
    for pref in _IFACE_PREF:
        for name, ip in entries:
            if name.startswith(pref):
                return ip
    return entries[0][1] if entries else ""


def fill_details(d: Device, timeout: float = 8) -> Device:
    out = shell(d.serial, _DETAIL_SCRIPT, timeout=timeout)
    kv: Dict[str, str] = {}
    for line in out.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            kv[k.strip()] = v.strip()
    d.model = kv.get("MODEL") or d.model
    d.manufacturer = kv.get("MANUF", "")
    d.android = kv.get("REL", "")
    d.sdk = kv.get("SDK", "")
    d.battery = kv.get("BATT", "")
    d.ifaces = kv.get("IPS", "")
    d.ip = pick_ip(d.ifaces)
    if d.transport != "emu":
        d.hw_serial = kv.get("SERIAL", "")
    return d


def parse_devices_output(text: str) -> List[Device]:
    devices: List[Device] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, rest = parts[0], parts[1:]
        state = rest[0]
        if state == "no" and len(rest) > 1 and rest[1].startswith("permissions"):
            state = "no permissions"
        kv: Dict[str, str] = {}
        for p in rest[1:]:
            if ":" in p:
                k, _, v = p.partition(":")
                if k in KNOWN_KV:
                    kv[k] = v
        d = Device(serial=serial, state=state)
        d.product = kv.get("product", "")
        d.model = kv.get("model", "").replace("_", " ")
        d.transport_id = kv.get("transport_id", "")
        d.transport = classify_transport(serial, "usb" in kv)
        devices.append(d)
    return devices


def list_devices(details: bool = True) -> List[Device]:
    r = run(["devices", "-l"], timeout=15)
    if r.returncode != 0 and not r.stdout:
        raise AdbError(f"adb devices falló: {r.stderr.strip()}")
    devices = parse_devices_output(r.stdout)
    if details:
        online = [d for d in devices if d.online]
        if online:
            with ThreadPoolExecutor(max_workers=min(8, len(online))) as ex:
                list(ex.map(fill_details, online))
    try:
        from . import registry
        registry.apply_aliases(devices)
    except Exception:
        pass
    return devices


def dedupe(devices: List[Device], prefer=("usb", "wifi", "emu", "?")) -> List[Device]:
    """Una entrada por dispositivo físico, prefiriendo USB sobre WiFi."""
    rank = {t: i for i, t in enumerate(prefer)}
    best: Dict[str, Device] = {}
    order: List[str] = []
    for d in devices:
        k = d.key
        if k not in best:
            best[k] = d
            order.append(k)
        elif rank.get(d.transport, 99) < rank.get(best[k].transport, 99):
            best[k] = d
    return [best[k] for k in order]


# --- WiFi helpers -----------------------------------------------------------

def tcpip(serial: str, port: int) -> subprocess.CompletedProcess:
    return run(["tcpip", str(port)], serial=serial, timeout=20)


def connect(hostport: str) -> subprocess.CompletedProcess:
    return run(["connect", hostport], timeout=25)


def disconnect(hostport: Optional[str] = None) -> subprocess.CompletedProcess:
    return run(["disconnect"] + ([hostport] if hostport else []), timeout=15)


def pair(hostport: str, code: str) -> subprocess.CompletedProcess:
    return run(["pair", hostport, code], timeout=40)


def usb(serial: str) -> subprocess.CompletedProcess:
    return run(["usb"], serial=serial, timeout=20)


def mdns_services() -> List[dict]:
    r = run(["mdns", "services"], timeout=15)
    out = []
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1].startswith("_adb"):
            out.append({"name": parts[0], "service": parts[1], "hostport": parts[2]})
    return out


def wait_online(serial: str, timeout: float = 10) -> bool:
    import time
    end = time.time() + timeout
    while time.time() < end:
        for d in parse_devices_output(run(["devices"], timeout=10).stdout):
            if d.serial == serial and d.online:
                return True
        time.sleep(0.5)
    return False


# --- refresco ---------------------------------------------------------------

def reconnect(mode: str = "offline") -> str:
    """adb reconnect [offline|device|host]: fuerza reconexión (offline = resetea offline/unauthorized)."""
    args = ["reconnect"] + ([mode] if mode in ("offline", "device") else [])
    r = run(args, timeout=20)
    return (r.stdout + r.stderr).strip()


def restart_server() -> str:
    """Reinicia el servidor adb (refresco duro: re-enumera USB y mDNS)."""
    out = []
    for args in (["kill-server"], ["start-server"]):
        r = run(args, timeout=30)
        out.append((r.stdout + r.stderr).strip())
    return "\n".join(x for x in out if x)


class DeviceTracker(threading.Thread):
    """Sigue `adb track-devices -l`: llama a on_change(devices) cada vez que aparece/desaparece un dispositivo.
    Cada bloque llega como 4 dígitos hex de longitud + texto con líneas 'serial estado …'."""

    def __init__(self, on_change, restart_delay: float = 2.0):
        super().__init__(daemon=True)
        self.on_change = on_change
        self.restart_delay = restart_delay
        self.proc = None
        self._stop_ev = threading.Event()

    def run(self) -> None:
        while not self._stop_ev.is_set():
            try:
                self.proc = subprocess.Popen([adb_path(), "track-devices", "-l"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                buf = b""
                assert self.proc.stdout is not None
                while not self._stop_ev.is_set():
                    chunk = self.proc.stdout.read1(65536) if hasattr(self.proc.stdout, "read1") else self.proc.stdout.read(1)
                    if not chunk:
                        break
                    buf += chunk
                    while len(buf) >= 4:
                        try:
                            n = int(buf[:4].decode("ascii"), 16)
                        except ValueError:
                            buf = buf[1:]      # resincroniza
                            continue
                        if len(buf) < 4 + n:
                            break
                        block = buf[4:4 + n].decode("utf-8", "replace")
                        buf = buf[4 + n:]
                        try:
                            self.on_change(parse_devices_output(block))
                        except Exception:
                            pass
            except Exception:
                pass
            finally:
                self._kill()
            if not self._stop_ev.is_set():
                time.sleep(self.restart_delay)

    def _kill(self) -> None:
        p = self.proc
        if p and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=2)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    def stop(self) -> None:
        self._stop_ev.set()
        self._kill()
