"""Fase 2 (mínima) del inspector de red: totales por UID (dumpsys netstats), sockets por UID (/proc/net/tcp*),
y tasa por interfaz (/proc/net/dev). Android no expone bytes por app en tiempo real sin root, así que la gráfica
por segundo es por interfaz (WiFi/móvil) y los totales por app se refrescan cuando el sistema los consolida."""
import re
import socket as socketmod
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional, Tuple

from . import adb as adbmod
from .adb import Device

TCP_STATES = {"01": "ESTAB", "02": "SYN_SENT", "03": "SYN_RECV", "04": "FIN_WAIT1", "05": "FIN_WAIT2", "06": "TIME_WAIT", "07": "CLOSE",
              "08": "CLOSE_WAIT", "09": "LAST_ACK", "0A": "LISTEN", "0B": "CLOSING"}
IFACE_SKIP = ("lo", "dummy", "ifb", "sit", "ip6tnl", "p2p", "tun", "gre")


def uid_of(serial: str, package: str) -> Optional[int]:
    out = adbmod.shell(serial, f"dumpsys package {package} 2>/dev/null | grep -m1 -E 'userId=|appId='", timeout=15)
    m = re.search(r"(?:userId|appId)=(\d+)", out)
    return int(m.group(1)) if m else None


def parse_netstats_totals(text: str, uid: int) -> dict:
    """Bytes rx/tx acumulados por tipo de red (WIFI/MOBILE/…) para un uid, desde un dump de netstats
    (`dumpsys netstats detail` o, cuando de verdad filtra, `dumpsys netstats --uid <uid>`)."""
    by_type: Dict[str, List[int]] = {}
    cur_type = None
    cur_uid = None
    for line in text.splitlines():
        s = line.strip()
        if "ident=[" in s and "uid=" in s:
            m = re.search(r"uid=(-?\d+)", s)
            cur_uid = int(m.group(1)) if m else None
            t = re.search(r"type=(\w+)", s)
            cur_type = (t.group(1) if t else "?").upper()
            if cur_type.isdigit():
                cur_type = {"0": "MOBILE", "1": "WIFI", "7": "BLUETOOTH", "9": "ETHERNET"}.get(cur_type, cur_type)
            continue
        if cur_uid == uid and s.startswith("st="):
            rb = re.search(r"rb=(\d+)", s)
            tb = re.search(r"tb=(\d+)", s)
            rp = re.search(r"rp=(\d+)", s)
            tp = re.search(r"tp=(\d+)", s)
            acc = by_type.setdefault(cur_type or "?", [0, 0, 0, 0])
            acc[0] += int(rb.group(1)) if rb else 0
            acc[1] += int(tb.group(1)) if tb else 0
            acc[2] += int(rp.group(1)) if rp else 0
            acc[3] += int(tp.group(1)) if tp else 0
    rx = sum(v[0] for v in by_type.values())
    tx = sum(v[1] for v in by_type.values())
    return {"rx": rx, "tx": tx, "rx_pkts": sum(v[2] for v in by_type.values()), "tx_pkts": sum(v[3] for v in by_type.values()),
            "by_type": {k: (v[0], v[1]) for k, v in by_type.items()}}


def _looks_like_full_dump(text: str, uid: int) -> bool:
    """`dumpsys netstats --uid <uid>` deberia filtrar, pero en algunas versiones (Android 17) no filtra e
    imprime el dump entero: detectamos eso viendo bloques ident=[...] de otros uids, o la ausencia total
    de lineas st= (nada que parsear, asi que tampoco vale la pena confiar en el filtrado)."""
    has_st = False
    other_uid = False
    for line in text.splitlines():
        s = line.strip()
        if "ident=[" in s and "uid=" in s:
            m = re.search(r"uid=(-?\d+)", s)
            if m and int(m.group(1)) != uid:
                other_uid = True
            continue
        if s.startswith("st="):
            has_st = True
    return other_uid or not has_st


def uid_totals(serial: str, uid: int, filter_works: Optional[bool] = None) -> dict:
    """Bytes rx/tx acumulados por tipo de red (WIFI/MOBILE/…) para un uid.

    `filter_works` es lo que ya se aprendió en ciclos anteriores (NetMonitor._uid_filter_works):
    None -> aun no se sabe, se paga el trial de `--uid` y, si resulta ser un volcado completo, se
    cae a `detail` (dos llamadas, solo la primera vez); True -> el filtro funciona en este
    dispositivo, se usa solo `--uid` (una llamada); False -> el filtro no funciona, se usa solo
    `detail` directamente (una llamada), sin repetir el trial en cada ciclo."""
    if filter_works is True:
        text = adbmod.shell(serial, f"dumpsys netstats --uid {uid} 2>/dev/null", timeout=30)
        result = parse_netstats_totals(text, uid)
        result["source"] = "uid"
        return result
    if filter_works is False:
        text = adbmod.shell(serial, "dumpsys netstats detail 2>/dev/null", timeout=60)
        result = parse_netstats_totals(text, uid)
        result["source"] = "detail"
        return result
    trial = adbmod.shell(serial, f"dumpsys netstats --uid {uid} 2>/dev/null", timeout=30)
    if _looks_like_full_dump(trial, uid):
        text = adbmod.shell(serial, "dumpsys netstats detail 2>/dev/null", timeout=60)
        result = parse_netstats_totals(text, uid)
        result["source"] = "detail"
        return result
    result = parse_netstats_totals(trial, uid)
    result["source"] = "uid"
    return result


def iface_counters(serial: str) -> Dict[str, Tuple[int, int]]:
    out = adbmod.shell(serial, "cat /proc/net/dev", timeout=10)
    res = {}
    for line in out.splitlines()[2:]:
        if ":" not in line:
            continue
        name, _, rest = line.partition(":")
        name = name.strip()
        parts = rest.split()
        if len(parts) < 10 or name.startswith(IFACE_SKIP):
            continue
        try:
            res[name] = (int(parts[0]), int(parts[8]))
        except ValueError:
            pass
    return res


def _hex_addr(h: str) -> str:
    ip, _, port = h.partition(":")
    p = int(port, 16)
    if len(ip) == 8:
        b = bytes.fromhex(ip)[::-1]
        return f"{'.'.join(str(x) for x in b)}:{p}"
    if len(ip) == 32:
        raw = bytes.fromhex(ip)
        # cada palabra de 4 bytes viene en little-endian
        words = b"".join(raw[i:i + 4][::-1] for i in range(0, 16, 4))
        if words[:12] == b"\x00" * 10 + b"\xff\xff":
            return f"{'.'.join(str(x) for x in words[12:])}:{p}"
        hexs = words.hex()
        return "[" + ":".join(hexs[i:i + 4] for i in range(0, 32, 4)) + f"]:{p}"
    return f"{ip}:{p}"


def probe_qtaguid(serial: str) -> bool:
    out = adbmod.shell(serial, "test -r /proc/net/xt_qtaguid/stats && echo yes", timeout=10)
    return out.strip() == "yes"


def qtaguid_totals(serial: str, uid: int) -> Optional[dict]:
    """Contadores legados por uid+iface de /proc/net/xt_qtaguid/stats (root/kernels antiguos): suma
    rx_bytes/tx_bytes de las filas sin tag (acct_tag_hex=0x0) que pertenecen al uid pedido."""
    out = adbmod.shell(serial, "cat /proc/net/xt_qtaguid/stats 2>/dev/null", timeout=10)
    lines = out.splitlines()
    if not lines or not lines[0].strip().startswith("idx"):
        return None
    rx = 0
    tx = 0
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 9:
            continue
        acct_tag_hex = parts[2]
        try:
            uid_tag_int = int(parts[3])
            rx_bytes = int(parts[5])
            tx_bytes = int(parts[7])
        except ValueError:
            continue
        if uid_tag_int != uid or acct_tag_hex != "0x0":
            continue
        rx += rx_bytes
        tx += tx_bytes
    return {"rx": rx, "tx": tx}


class ReverseDns:
    """Resuelve PTR de IPs remotas fuera del hilo de UI, con cache (incluidos fallos)."""

    def __init__(self, timeout: float = 1.5, max_workers: int = 2):
        self.timeout = timeout
        self._cache: Dict[str, Optional[str]] = {}
        self._executor = ThreadPoolExecutor(max_workers=max_workers)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)

    def lookup(self, ip: str) -> Optional[str]:
        if ip in self._cache:
            return self._cache[ip]
        future = self._executor.submit(socketmod.gethostbyaddr, ip)
        try:
            host = future.result(timeout=self.timeout)[0]
        except Exception:
            host = None
        self._cache[ip] = host
        return host


@dataclass
class Socket:
    proto: str
    state: str
    local: str
    remote: str
    uid: int


def sockets_for_uid(serial: str, uid: Optional[int]) -> List[Socket]:
    out = adbmod.shell(serial, "for f in tcp tcp6 udp udp6; do echo \"##$f\"; cat /proc/net/$f 2>/dev/null | tail -n +2; done", timeout=15)
    socks: List[Socket] = []
    proto = "tcp"
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("##"):
            proto = s[2:]
            continue
        parts = s.split()
        if len(parts) < 8:
            continue
        try:
            u = int(parts[7])
        except ValueError:
            continue
        if uid is not None and u != uid:
            continue
        state = TCP_STATES.get(parts[3].upper(), parts[3]) if proto.startswith("tcp") else ("ESTAB" if parts[3].upper() == "01" else "UDP")
        socks.append(Socket(proto, state, _hex_addr(parts[1]), _hex_addr(parts[2]), u))
    socks.sort(key=lambda x: (x.proto, x.state != "ESTAB", x.remote))
    return socks


def host_table(sockets: List[Socket], dns: ReverseDns, now: Optional[float] = None) -> List[dict]:
    """Agrupa los sockets actuales por IP:puerto remoto, con el hostname (si la cache de dns lo tiene)."""
    now = time.time() if now is None else now
    rows: Dict[str, dict] = {}
    for sock in sockets:
        ip, _, _port = sock.remote.rpartition(":")
        ip = ip.strip("[]") or sock.remote
        row = rows.get(sock.remote)
        if row is None:
            row = {"host": dns.lookup(ip), "ip": ip, "connections": 0,
                   "states": {}, "first_seen": now, "last_seen": now}
            rows[sock.remote] = row
        row["connections"] += 1
        row["states"][sock.state] = row["states"].get(sock.state, 0) + 1
        row["last_seen"] = now
    return sorted(rows.values(), key=lambda r: (-r["connections"], r["ip"]))


def connection_events(prev: List[Socket], cur: List[Socket], now: float) -> List[dict]:
    """Diferencia por (proto, local, remote) entre dos lecturas de sockets: altas y bajas."""
    def key(s: Socket) -> Tuple[str, str, str]:
        return (s.proto, s.local, s.remote)

    prev_by_key = {key(s): s for s in prev}
    cur_by_key = {key(s): s for s in cur}
    events: List[dict] = []
    for k, s in cur_by_key.items():
        if k not in prev_by_key:
            events.append({"t": now, "kind": "new", "proto": s.proto, "local": s.local, "remote": s.remote, "state": s.state})
    for k, s in prev_by_key.items():
        if k not in cur_by_key:
            events.append({"t": now, "kind": "closed", "proto": s.proto, "local": s.local, "remote": s.remote, "state": s.state})
    return events


@dataclass
class NetSample:
    t: float
    rates: Dict[str, Tuple[float, float]] = field(default_factory=dict)   # iface -> (rx B/s, tx B/s)
    counters: Dict[str, Tuple[int, int]] = field(default_factory=dict)
    interval: float = 0.0
    app_rx_rate: Optional[float] = None
    app_tx_rate: Optional[float] = None

    @property
    def rx_rate(self) -> float:
        return sum(r for r, _ in self.rates.values())

    @property
    def tx_rate(self) -> float:
        return sum(t for _, t in self.rates.values())


class NetMonitor:
    def __init__(self, dev: Device, package: Optional[str], interval: float = 1.0, totals_every: float = 15.0, history: int = 600,
                 on_sample: Optional[Callable[[NetSample], None]] = None, on_totals: Optional[Callable[[dict], None]] = None,
                 on_status: Optional[Callable[[str, str], None]] = None):
        self.dev = dev
        self.package = package
        self.interval = max(0.3, interval)
        self.totals_every = totals_every
        self.samples: Deque[NetSample] = deque(maxlen=history)
        self.on_sample = on_sample or (lambda s: None)
        self.on_totals = on_totals or (lambda t: None)
        self.on_status = on_status or (lambda k, t: None)
        self.uid: Optional[int] = None
        self.totals: Optional[dict] = None
        self.totals_start: Optional[dict] = None
        self.sockets: List[Socket] = []
        self.running = False
        self._prev: Optional[Tuple[float, Dict[str, Tuple[int, int]]]] = None
        self._thread: Optional[threading.Thread] = None
        self._tot_thread: Optional[threading.Thread] = None
        self._tot_last = 0.0
        self.qtaguid_available = False
        self.cadence_label = f"por app acumulado cada {int(self.totals_every)} s (dumpsys netstats)"
        self._prev_qtaguid: Optional[Tuple[float, dict]] = None
        self.dns = ReverseDns()
        self.events: Deque[dict] = deque(maxlen=500)
        self.hosts: List[dict] = []
        self._prev_sockets: List[Socket] = []
        self._sockets_baseline_set = False
        self._uid_filter_works: Optional[bool] = None

    def start(self) -> None:
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.running = False
        self.dns.shutdown()

    def _loop(self) -> None:
        serial = self.dev.serial
        if self.package:
            try:
                self.uid = uid_of(serial, self.package)
                self.on_status("info", f"{self.package}: uid {self.uid}" if self.uid else f"no encuentro el uid de {self.package}")
            except Exception as e:
                self.on_status("warn", str(e))
        try:
            self.qtaguid_available = probe_qtaguid(serial)
        except Exception:
            self.qtaguid_available = False
        self.cadence_label = ("por app cada 1 s (xt_qtaguid)" if self.qtaguid_available else
                               f"por app acumulado cada {int(self.totals_every)} s (dumpsys netstats)")
        while self.running:
            t0 = time.time()
            try:
                counters = iface_counters(serial)
                now = time.time()
                s = NetSample(t=now, counters=counters)
                if self._prev:
                    pt, pc = self._prev
                    s.interval = now - pt
                    for name, (rx, tx) in counters.items():
                        prx, ptx = pc.get(name, (rx, tx))
                        s.rates[name] = (max(0.0, (rx - prx) / s.interval), max(0.0, (tx - ptx) / s.interval))
                self._prev = (now, counters)
                if self.qtaguid_available and self.uid is not None:
                    try:
                        qt = qtaguid_totals(serial, self.uid)
                    except Exception:
                        qt = None
                    if qt is not None:
                        if self._prev_qtaguid:
                            pqt_t, pqt = self._prev_qtaguid
                            dt = now - pqt_t
                            if dt > 0:
                                s.app_rx_rate = max(0.0, (qt["rx"] - pqt["rx"]) / dt)
                                s.app_tx_rate = max(0.0, (qt["tx"] - pqt["tx"]) / dt)
                        self._prev_qtaguid = (now, qt)
                self.samples.append(s)
                self.on_sample(s)
                if self.uid is not None and (now - self._tot_last) >= self.totals_every and (self._tot_thread is None or not self._tot_thread.is_alive()):
                    self._tot_last = now
                    self._tot_thread = threading.Thread(target=self._refresh_totals, daemon=True)
                    self._tot_thread.start()
            except Exception as e:
                self.on_status("err", f"red: {e}")
            wait = max(0.05, self.interval - (time.time() - t0))
            end = time.time() + wait
            while self.running and time.time() < end:
                time.sleep(0.05)

    def _refresh_totals(self) -> None:
        try:
            new_sockets = sockets_for_uid(self.dev.serial, self.uid)
            now = time.time()
            for sock in new_sockets:
                ip, _, _port = sock.remote.rpartition(":")
                ip = ip.strip("[]") or sock.remote
                self.dns.lookup(ip)
            if self._sockets_baseline_set:
                self.events.extend(connection_events(self._prev_sockets, new_sockets, now))
            else:
                self._sockets_baseline_set = True
            self.hosts = host_table(new_sockets, self.dns, now)
            self._prev_sockets = new_sockets
            self.sockets = new_sockets
            tot = uid_totals(self.dev.serial, self.uid, self._uid_filter_works)
            self._uid_filter_works = tot["source"] == "uid"
            if not self.qtaguid_available:
                self.cadence_label = (f"por app acumulado cada {int(self.totals_every)} s "
                                       f"(dumpsys netstats {tot['source']})")
            if self.totals_start is None:
                self.totals_start = tot
            self.totals = tot
            self.on_totals(tot)
        except Exception as e:
            self.on_status("warn", f"totales: {e}")
