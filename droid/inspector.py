"""Fase 1 del inspector: CPU por hilo, memoria (RSS/PSS/heaps), frames/jank, estado del proceso y salidas de la app.

Todo vía adb shell (dumpsys + /proc). Muestreo rápido cada tick (CPU, /proc/status, gfxinfo) y muestreo lento en
un hilo aparte (dumpsys meminfo) porque puede tardar segundos. Cada sesión se guarda en ~/.droid/inspect/ como JSONL.
"""
import json
import re
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field, fields as dc_fields
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Deque, Dict, List, Optional, Tuple

from . import adb as adbmod
from . import config
from . import memoria
from .adb import Device, sanitize
from .watch import LogWatcher

if TYPE_CHECKING:
    from . import salud

INSPECT_DIR = config.DROID_HOME / "inspect"
SIXTY_HZ_FRAME_DEADLINE_MS = 16.67
MAX_FRAME_MS_PER_TICK = 240

STAT_RE = re.compile(r"^(?:(?P<path>/proc/\d+/task/(?P<tid>\d+)/stat):)?(?P<id>\d+) \((?P<comm>.*)\) (?P<state>\S) (?P<rest>.*)$")
EXIT_REASONS = {0: "UNKNOWN", 1: "EXIT_SELF", 2: "SIGNALED", 3: "LOW_MEMORY", 4: "CRASH", 5: "CRASH_NATIVE", 6: "ANR", 7: "INITIALIZATION_FAILURE",
                8: "PERMISSION_CHANGE", 9: "EXCESSIVE_RESOURCE_USAGE", 10: "USER_REQUESTED", 11: "USER_STOPPED", 12: "DEPENDENCY_DIED",
                13: "OTHER", 14: "FREEZER", 15: "PACKAGE_STATE_CHANGE", 16: "PACKAGE_UPDATED"}
OOM_STATES = [(0, "primer plano"), (100, "visible"), (200, "perceptible"), (250, "perceptible"), (300, "backup"), (400, "servicio pesado"),
              (500, "servicio A"), (600, "home"), (700, "actividad previa"), (800, "servicio B"), (900, "en caché"), (1000, "en caché")]


def oom_label(adj: Optional[int]) -> str:
    if adj is None:
        return "?"
    label = "en caché"
    for limit, name in OOM_STATES:
        if adj <= limit:
            label = name
            break
    return label


# ----------------------------------------------------------------------------- muestras

@dataclass
class ThreadSample:
    tid: int
    name: str
    state: str
    ticks: int
    cpu: float = 0.0        # % de un core en el intervalo


@dataclass
class Sample:
    t: float
    pid: Optional[int]
    alive: bool
    cpu: float = 0.0                       # % de un core (100 = un core saturado)
    cpu_total_pct: float = 0.0             # % del total de cores
    ncpu: int = 1
    threads: List[ThreadSample] = field(default_factory=list)
    nthreads: int = 0
    rss_kb: int = 0
    rss_anon_kb: int = 0
    rss_file_kb: int = 0
    swap_kb: int = 0
    pss_kb: Optional[int] = None           # de smaps_rollup vía run-as (apps debuggables)
    oom_adj: Optional[int] = None
    frames: int = 0                        # frames renderizados en el intervalo
    janky: int = 0
    fps: float = 0.0
    jank_pct: float = 0.0
    p50: Optional[int] = None
    p90: Optional[int] = None
    p99: Optional[int] = None
    missed_vsync: int = 0
    slow_ui: int = 0
    gfx_total_frames: int = 0
    gfx_total_janky: int = 0
    interval: float = 0.0
    frame_ms: List[float] = field(default_factory=list)
    frames_truncated: int = 0
    """Duraciones de frame (ms) nuevas en este tick; recortado a las últimas 240 entradas."""

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "threads"}
        d["threads"] = [{"tid": th.tid, "name": th.name, "state": th.state, "cpu": round(th.cpu, 1)} for th in self.threads if th.cpu >= 0.05]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Sample":
        names = {f.name for f in dc_fields(cls)}
        kwargs = {k: v for k, v in d.items() if k in names and k != "threads"}
        s = cls(**kwargs)
        s.threads = [ThreadSample(tid=t["tid"], name=t.get("name", ""), state=t.get("state", ""), ticks=0, cpu=t.get("cpu", 0.0))
                     for t in d.get("threads", [])]
        return s


@dataclass
class MemInfo:
    """dumpsys meminfo (lento): App Summary + heaps + objetos."""
    t: float
    pss_total_kb: int = 0
    rss_total_kb: int = 0
    java_heap_kb: int = 0
    native_heap_kb: int = 0
    code_kb: int = 0
    stack_kb: int = 0
    graphics_kb: int = 0
    private_other_kb: int = 0
    system_kb: int = 0
    dalvik_heap_size_kb: int = 0
    dalvik_heap_alloc_kb: int = 0
    dalvik_heap_free_kb: int = 0
    native_heap_size_kb: int = 0
    native_heap_alloc_kb: int = 0
    native_heap_free_kb: int = 0
    views: int = 0
    view_roots: int = 0
    activities: int = 0
    app_contexts: int = 0
    error: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict) -> "MemInfo":
        names = {f.name for f in dc_fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


@dataclass
class ExitRecord:
    timestamp: str
    pid: int
    reason: int
    reason_name: str
    subreason: str
    status: int
    importance: int
    description: str
    anr: str
    rss: str


# ----------------------------------------------------------------------------- parsers

def parse_stat_line(line: str):
    m = STAT_RE.match(line.strip())
    if not m:
        return None
    rest = m.group("rest").split()
    # campos tras state: ppid(4) pgrp session tty tpgid flags minflt cminflt majflt cmajflt utime(14) stime(15)
    try:
        ticks = int(rest[10]) + int(rest[11])
    except (IndexError, ValueError):
        return None
    tid = int(m.group("tid") or m.group("id"))
    return tid, m.group("comm"), m.group("state"), ticks


def parse_meminfo(text: str, t: Optional[float] = None) -> MemInfo:
    mi = MemInfo(t=t or time.time())
    if "IOException" in text or "Timeout" in text:
        mi.error = "timeout de dumpsys meminfo"
    summary = {}
    in_summary = False
    heaps = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("App Summary"):
            in_summary = True
            continue
        if in_summary:
            m = re.match(r"^(Java Heap|Native Heap|Code|Stack|Graphics|Private Other|System|Unknown):\s+(\d+)", s)
            if m:
                summary[m.group(1)] = int(m.group(2))
                continue
            m = re.match(r"^TOTAL PSS:\s+(\d+)\s+TOTAL RSS:\s+(\d+)", s)
            if m:
                mi.pss_total_kb, mi.rss_total_kb = int(m.group(1)), int(m.group(2))
                in_summary = False
                continue
            m = re.match(r"^TOTAL:\s+(\d+)", s)
            if m:
                mi.pss_total_kb = int(m.group(1))
                in_summary = False
                continue
        m = re.search(r"Heap Size:\s+(\d+)\s+Heap Alloc:\s+(\d+)\s+Heap Free:\s+(\d+)", s)
        if m:
            heaps.append((int(m.group(1)), int(m.group(2)), int(m.group(3))))
        m = re.match(r"^Views:\s+(\d+)\s+ViewRootImpl:\s+(\d+)", s)
        if m:
            mi.views, mi.view_roots = int(m.group(1)), int(m.group(2))
        m = re.match(r"^AppContexts:\s+(\d+)\s+Activities:\s+(\d+)", s)
        if m:
            mi.app_contexts, mi.activities = int(m.group(1)), int(m.group(2))
        m = re.match(r"^TOTAL PSS:\s+(\d+)", s)
        if m and not mi.pss_total_kb:
            mi.pss_total_kb = int(m.group(1))
    mi.java_heap_kb = summary.get("Java Heap", 0)
    mi.native_heap_kb = summary.get("Native Heap", 0)
    mi.code_kb = summary.get("Code", 0)
    mi.stack_kb = summary.get("Stack", 0)
    mi.graphics_kb = summary.get("Graphics", 0)
    mi.private_other_kb = summary.get("Private Other", 0)
    mi.system_kb = summary.get("System", 0)
    if heaps:
        mi.dalvik_heap_size_kb, mi.dalvik_heap_alloc_kb, mi.dalvik_heap_free_kb = heaps[0]
    if len(heaps) > 1:
        mi.native_heap_size_kb, mi.native_heap_alloc_kb, mi.native_heap_free_kb = heaps[1]
    return mi


def parse_gfxinfo(text: str) -> dict:
    out: dict = {}
    pats = {
        "total_frames": r"Total frames rendered:\s+(\d+)", "janky": r"Janky frames:\s+(\d+)",
        "p50": r"50th percentile:\s+(\d+)ms", "p90": r"90th percentile:\s+(\d+)ms", "p95": r"95th percentile:\s+(\d+)ms", "p99": r"99th percentile:\s+(\d+)ms",
        "missed_vsync": r"Number Missed Vsync:\s+(\d+)", "slow_ui": r"Number Slow UI thread:\s+(\d+)",
        "slow_bitmap": r"Number Slow bitmap uploads:\s+(\d+)", "slow_draw": r"Number Slow issue draw commands:\s+(\d+)",
        "deadline_missed": r"Number Frame deadline missed:\s+(\d+)",
    }
    for k, p in pats.items():
        m = re.search(p, text)
        if m:
            out[k] = int(m.group(1))
    return out


def parse_framestats(text: str) -> List[Tuple[int, float]]:
    lines = text.splitlines()
    header_idxs = [i for i, line in enumerate(lines) if line.startswith("Flags,FrameTimelineVsyncId")]
    by_vsync: Dict[int, float] = {}
    for header_idx in header_idxs:
        header = lines[header_idx].rstrip(",").split(",")
        try:
            flags_i = header.index("Flags")
            vsync_i = header.index("IntendedVsync")
            completed_i = header.index("FrameCompleted")
        except ValueError:
            continue
        need = max(flags_i, vsync_i, completed_i)
        for line in lines[header_idx + 1:]:
            s = line.strip()
            if not s or not (s[0].isdigit() or s[0] == "-"):
                break
            fields = s.rstrip(",").split(",")
            if len(fields) <= need:
                continue
            try:
                flags = int(fields[flags_i])
                if flags != 0:
                    continue
                vsync = int(fields[vsync_i])
                completed = int(fields[completed_i])
            except ValueError:
                continue
            by_vsync[vsync] = (completed - vsync) / 1e6
    return sorted(by_vsync.items())


def new_frames(frames: List[Tuple[int, float]], last_vsync: int) -> Tuple[List[float], int]:
    fresh = [(v, ms) for v, ms in frames if v > last_vsync]
    new_last = max((v for v, _ in fresh), default=last_vsync)
    return [ms for _, ms in fresh], new_last


def _percentile(values: List[float], pct: float) -> float:
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * pct / 100
    f, c = int(k), min(int(k) + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] * (c - k) + s[c] * (k - f)


def parse_exit_info(text: str) -> List[ExitRecord]:
    records: List[ExitRecord] = []
    cur: Dict[str, str] = {}

    def flush():
        if cur.get("timestamp"):
            reason = int(cur.get("reason", "0"))
            records.append(ExitRecord(cur.get("timestamp", ""), int(cur.get("pid", "0")), reason, EXIT_REASONS.get(reason) or cur.get("reason_name") or "?",
                                      cur.get("subreason", ""), int(cur.get("status", "0")), int(cur.get("importance", "0")),
                                      cur.get("description", ""), cur.get("anr", ""), cur.get("rss", "")))
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("ApplicationExitInfo #"):
            flush()
            cur = {}
            continue
        m = re.match(r"^timestamp=(\S+ \S+) pid=(\d+)", s)
        if m:
            cur["timestamp"], cur["pid"] = m.group(1), m.group(2)
        m = re.search(r"reason=(\d+) \((.+?)\) subreason=(\d+) \((.+?)\) status=(\d+)", s)
        if m:
            cur["reason"], cur["reason_name"], cur["subreason"], cur["status"] = m.group(1), m.group(2), m.group(4), m.group(5)
        m = re.search(r"importance=(\d+) pss=\S+ rss=(\S+)", s)
        if m:
            cur["importance"], cur["rss"] = m.group(1), m.group(2)
        if s.startswith("description="):
            cur["description"] = s[len("description="):]
        if s.startswith("anrInfo=") and not s.startswith("anrInfo=null"):
            cur["anr"] = s[len("anrInfo="):]
    flush()
    return records


# ----------------------------------------------------------------------------- recolección

def resolve_pid(serial: str, package: str) -> Optional[int]:
    out = adbmod.shell(serial, f"pidof {package} 2>/dev/null", timeout=8).split()
    for tok in out:
        if tok.isdigit():
            return int(tok)
    return None


def is_debuggable(serial: str, package: str) -> bool:
    out = adbmod.shell(serial, f"run-as {package} id 2>&1", timeout=8)
    return "uid=" in out and "not debuggable" not in out and "unknown package" not in out


def device_tz_offset(serial: str) -> Optional[str]:
    try:
        out = adbmod.shell(serial, "date +%z", timeout=8).strip()
    except Exception:
        return None
    return out if re.fullmatch(r"[+-]\d{4}", out) else None


def fast_sample_script(pid: int, package: str, debuggable: bool) -> str:
    parts = [
        "head -1 /proc/stat",
        "echo \"##NCPU $(grep -c '^cpu[0-9]' /proc/stat)\"",
        f"cat /proc/{pid}/stat",
        "echo '##TASKS'",
        f"grep '' /proc/{pid}/task/*/stat 2>/dev/null",
        "echo '##STATUS'",
        f"grep -E 'VmRSS|RssAnon|RssFile|VmSwap|Threads' /proc/{pid}/status 2>/dev/null",
        f"echo \"##OOM $(cat /proc/{pid}/oom_score_adj 2>/dev/null)\"",
    ]
    if debuggable:
        parts.append("echo '##PSS'")
        parts.append(f"run-as {package} cat /proc/{pid}/smaps_rollup 2>/dev/null | grep -E '^(Pss|Pss_Anon|Pss_File|Private_Dirty|Swap):'")
    parts.append("echo '##GFX'")
    gfx_grep = "grep -E 'Total frames|Janky frames:|percentile|Missed Vsync|Slow UI|deadline missed:|^Flags,|^[0-9]+,'"
    parts.append(f"dumpsys gfxinfo {package} framestats 2>/dev/null | {gfx_grep}")
    return "; ".join(parts)


def parse_fast_sample(text: str):
    """Devuelve (total_ticks, ncpu, proc_ticks, threads[(tid,name,state,ticks)], status{}, oom, pss{}, gfx{})."""
    section = "cpu"
    total_ticks = 0
    ncpu = 1
    proc = None
    threads = []
    status: Dict[str, int] = {}
    oom = None
    pss: Dict[str, int] = {}
    gfx_lines = []
    for line in text.splitlines():
        s = line.rstrip()
        if s.startswith("##NCPU"):
            try:
                ncpu = max(1, int(s.split()[1]))
            except (IndexError, ValueError):
                pass
            continue
        if s.startswith("##TASKS"):
            section = "tasks"; continue
        if s.startswith("##STATUS"):
            section = "status"; continue
        if s.startswith("##OOM"):
            try:
                oom = int(s.split()[1])
            except (IndexError, ValueError):
                oom = None
            continue
        if s.startswith("##PSS"):
            section = "pss"; continue
        if s.startswith("##GFX"):
            section = "gfx"; continue
        if section == "cpu":
            if s.startswith("cpu "):
                total_ticks = sum(int(x) for x in s.split()[1:] if x.isdigit())
            elif s and s[0].isdigit():
                proc = parse_stat_line(s)
        elif section == "tasks":
            r = parse_stat_line(s)
            if r:
                threads.append(r)
        elif section == "status":
            m = re.match(r"^(\w+):\s+(\d+)", s)
            if m:
                status[m.group(1)] = int(m.group(2))
        elif section == "pss":
            m = re.match(r"^(\w+):\s+(\d+)", s)
            if m:
                pss[m.group(1)] = int(m.group(2))
        elif section == "gfx":
            gfx_lines.append(s)
    gfx_text = "\n".join(gfx_lines)
    gfx = parse_gfxinfo(gfx_text)
    if gfx_text.strip():
        gfx["_raw_text"] = gfx_text  # NEEDS-COMMENT: stashed so _tick can also run parse_framestats without changing this function's 8-item return arity
    return total_ticks, ncpu, proc, threads, status, oom, pss, gfx


class InspectSession:
    """Muestreo periódico de una app en un dispositivo, con historial en memoria y grabación JSONL."""

    def __init__(self, dev: Device, package: str, interval: float = 1.0, meminfo_every: float = 10.0, record: bool = True,
                 history: int = 600, on_sample: Optional[Callable[[Sample], None]] = None, on_meminfo: Optional[Callable[[MemInfo], None]] = None,
                 on_status: Optional[Callable[[str, str], None]] = None):
        self.dev = dev
        self.package = package
        self.interval = max(0.3, interval)
        self.meminfo_every = meminfo_every
        self.record = record
        self.on_sample = on_sample or (lambda s: None)
        self.on_meminfo = on_meminfo or (lambda m: None)
        self.on_status = on_status or (lambda k, t: None)
        self.samples: Deque[Sample] = deque(maxlen=history)
        self.meminfos: Deque[MemInfo] = deque(maxlen=max(10, history // 5))
        self.exits: List[ExitRecord] = []
        self._exits_written: set = set()
        self.gc_events: Deque[memoria.GcEvent] = deque(maxlen=500)
        self.leak_flags: List[memoria.LeakFlag] = []
        self.crashes: List["salud.CrashRecord"] = []
        self.anrs: List["salud.AnrRecord"] = []
        self.proc_events: Deque["salud.ProcEvent"] = deque(maxlen=500)
        self.groups: List["salud.CrashGroup"] = []
        self._crash_lines: List[str] = []
        self._crash_last_t: float = 0.0
        self._anr_buf: List[str] = []
        self._watcher: Optional[LogWatcher] = None
        self.pid: Optional[int] = None
        self.debuggable = False
        self.running = False
        self.started = None
        self._prev = None            # (t, total_ticks, proc_ticks, {tid: ticks}, gfx)
        self._thread: Optional[threading.Thread] = None
        self._mem_thread: Optional[threading.Thread] = None
        self._mem_last = 0.0
        self.path: Optional[Path] = None
        self._fh = None
        self.error: Optional[str] = None
        self.thread_names: Dict[int, str] = {}
        self._last_vsync: int = 0
        self._framestats_seen: bool = False
        self.device_tz_offset: Optional[str] = None

    # --- ciclo de vida ---
    def start(self) -> None:
        self.running = True
        self.started = datetime.now()
        self.device_tz_offset = device_tz_offset(self.dev.serial)
        if self.record:
            INSPECT_DIR.mkdir(parents=True, exist_ok=True)
            d = INSPECT_DIR / f"{sanitize(self.dev.name)}-{self.dev.key}"
            d.mkdir(parents=True, exist_ok=True)
            self.path = d / f"{self.started.strftime('%Y%m%d-%H%M%S')}-{sanitize(self.package)}.jsonl"
            self._fh = open(self.path, "a", encoding="utf-8")
            self._write({"type": "meta", "device": self.dev.to_dict(), "package": self.package, "interval": self.interval,
                         "started": self.started.isoformat(timespec="seconds"), "device_tz_offset": self.device_tz_offset})
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.running = False
        if self._watcher:
            self._watcher.stop()
            self._watcher = None
        if self._fh:
            try:
                self._write({"type": "end", "ended": datetime.now().isoformat(timespec="seconds"), "samples": len(self.samples)})
                self._fh.close()
            except Exception:
                pass
            self._fh = None

    def _write(self, obj: dict) -> None:
        if self._fh:
            try:
                self._fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
                self._fh.flush()
            except Exception:
                pass

    # --- bucle ---
    def _loop(self) -> None:
        serial = self.dev.serial
        try:
            self.debuggable = is_debuggable(serial, self.package)
            self.on_status("info", f"{self.package}: {'debuggable (PSS vía run-as)' if self.debuggable else 'no debuggable (PSS solo con dumpsys meminfo)'}")
            self.refresh_exits()
        except Exception as e:
            self.on_status("warn", f"no pude comprobar debuggable: {e}")
        while self.running:
            t0 = time.time()
            try:
                self._tick()
            except Exception as e:
                self.error = str(e)
                self.on_status("err", f"muestra fallida: {e}")
            if self.running and time.time() - self._mem_last >= self.meminfo_every and self.pid and (self._mem_thread is None or not self._mem_thread.is_alive()):
                self._mem_last = time.time()
                self._mem_thread = threading.Thread(target=self._meminfo, args=(self.pid,), daemon=True)
                self._mem_thread.start()
            elapsed = time.time() - t0
            wait = max(0.05, self.interval - elapsed)
            end = time.time() + wait
            while self.running and time.time() < end:
                time.sleep(0.05)

    def _tick(self) -> None:
        serial = self.dev.serial
        if not self.pid:
            self.pid = resolve_pid(serial, self.package)
            if not self.pid:
                s = Sample(t=time.time(), pid=None, alive=False)
                self.samples.append(s)
                self.on_sample(s)
                return
            self._prev = None
            self.on_status("ok", f"proceso {self.package} pid {self.pid}")
            self._ensure_watcher()
        text = adbmod.shell(serial, fast_sample_script(self.pid, self.package, self.debuggable), timeout=15)
        total, ncpu, proc, threads, status, oom, pss, gfx = parse_fast_sample(text)
        now = time.time()
        if proc is None:
            # el proceso murió o cambió
            self.on_status("warn", f"proceso {self.pid} ya no existe; buscando de nuevo…")
            self.pid = None
            self._prev = None
            s = Sample(t=now, pid=None, alive=False)
            self.samples.append(s)
            self.on_sample(s)
            self.refresh_exits()
            return
        s = Sample(t=now, pid=self.pid, alive=True, ncpu=ncpu)
        s.nthreads = status.get("Threads", len(threads))
        s.rss_kb = status.get("VmRSS", 0)
        s.rss_anon_kb = status.get("RssAnon", 0)
        s.rss_file_kb = status.get("RssFile", 0)
        s.swap_kb = status.get("VmSwap", 0)
        s.pss_kb = pss.get("Pss") if pss else None
        s.oom_adj = oom
        s.gfx_total_frames = gfx.get("total_frames", 0)
        s.gfx_total_janky = gfx.get("janky", 0)
        s.p50, s.p90, s.p99 = gfx.get("p50"), gfx.get("p90"), gfx.get("p99")
        raw_gfx_text = gfx.get("_raw_text", "")
        frames_raw = parse_framestats(raw_gfx_text)
        new_ms, self._last_vsync = new_frames(frames_raw, self._last_vsync)
        if not self._prev:
            new_ms = []
        s.frame_ms = new_ms[-MAX_FRAME_MS_PER_TICK:]
        s.frames_truncated = max(0, len(new_ms) - MAX_FRAME_MS_PER_TICK)
        if "Flags,FrameTimelineVsyncId" in raw_gfx_text:
            self._framestats_seen = True
        has_framestats = self._framestats_seen
        tid_ticks = {tid: ticks for tid, _, _, ticks in threads}
        for tid, name, _, _ in threads:
            self.thread_names[tid] = name
        if self._prev:
            pt, ptotal, pproc, ptids, pgfx = self._prev
            dt_total = max(1, total - ptotal)
            s.interval = now - pt
            s.cpu = (proc[3] - pproc) / dt_total * ncpu * 100.0
            s.cpu_total_pct = (proc[3] - pproc) / dt_total * 100.0
            for tid, name, state, ticks in threads:
                th = ThreadSample(tid=tid, name=("main" if tid == self.pid else name), state=state, ticks=ticks)
                th.cpu = (ticks - ptids.get(tid, ticks)) / dt_total * ncpu * 100.0
                s.threads.append(th)
            s.threads.sort(key=lambda th: -th.cpu)
            if has_framestats:
                s.frames = len(new_ms)
                s.janky = sum(1 for ms in new_ms if ms > SIXTY_HZ_FRAME_DEADLINE_MS)
            else:
                s.frames = max(0, s.gfx_total_frames - pgfx.get("total_frames", s.gfx_total_frames))
                s.janky = max(0, s.gfx_total_janky - pgfx.get("janky", s.gfx_total_janky))
            s.fps = s.frames / s.interval if s.interval > 0 else 0.0
            s.jank_pct = (s.janky / s.frames * 100.0) if s.frames else 0.0
            s.missed_vsync = max(0, gfx.get("missed_vsync", 0) - pgfx.get("missed_vsync", 0))
            s.slow_ui = max(0, gfx.get("slow_ui", 0) - pgfx.get("slow_ui", 0))
        else:
            for tid, name, state, ticks in threads:
                s.threads.append(ThreadSample(tid=tid, name=("main" if tid == self.pid else name), state=state, ticks=ticks))
        self._prev = (now, total, proc[3], tid_ticks, gfx)
        self.samples.append(s)
        self._write({"type": "sample", **s.to_dict()})
        self.on_sample(s)

    def _ensure_watcher(self) -> None:
        if self._watcher is None:
            self._watcher = LogWatcher(self.dev.serial, self.dev.key, self._on_log_line, pid=self.pid,
                                        buffers=("main", "crash"), extra_tags=("ActivityManager",))
            self._watcher.start()
        else:
            self._watcher.set_pid(self.pid)

    def _on_log_line(self, ll, raw: str) -> None:
        now = time.time()
        if self._crash_lines and (ll.tag != "AndroidRuntime" or now - self._crash_last_t > 1.0):
            self._flush_crash_buf()
        if ll.tag == "AndroidRuntime" and ll.level == "E" and ll.pid == self.pid:
            self._crash_lines.append(raw)
            self._crash_last_t = now
            return
        if ll.tag == "ActivityManager":
            self._handle_am_line(ll, raw)
            return
        ev = memoria.parse_gc_line(raw)
        if ev is None:
            return
        self.gc_events.append(ev)
        self._write({"type": "gc", **ev.to_dict()})

    def _flush_crash_buf(self) -> None:
        from . import salud
        lines = self._crash_lines
        self._crash_lines = []
        if not lines:
            return
        rec = salud.parse_crash_block(lines)
        if rec is not None:
            self.crashes.append(rec)
            self._write({"type": "crash", **rec.to_dict()})
            self._recompute_groups()

    def _handle_am_line(self, ll, raw: str) -> None:
        from . import salud
        msg = ll.msg
        anr_m = salud.ANR_IN_RE.match(msg)
        if anr_m and anr_m.group(1).startswith(self.package):
            self._anr_buf = [raw]
            return
        if self._anr_buf:
            self._anr_buf.append(raw)
            if salud.ANR_REASON_RE.match(msg):
                self._flush_anr_buf()
            elif len(self._anr_buf) > 20:
                self._anr_buf = []
            return
        ev = salud.parse_am_line(raw)
        if ev is None or not ev.process.startswith(self.package):
            return
        self.proc_events.append(ev)
        self._write({"type": "procevent", **ev.to_dict()})
        if ev.kind in ("died", "killed"):
            self.on_status("warn", f"{self.package} {ev.kind} (pid {ev.pid})")

    def _flush_anr_buf(self) -> None:
        from . import salud
        lines = self._anr_buf
        self._anr_buf = []
        parsed = salud.parse_anr_logcat(lines)
        if parsed is None:
            return
        component, reason = parsed
        rec = None
        if self.debuggable:
            try:
                text = adbmod.shell(self.dev.serial, f"run-as {self.package} cat /data/anr/traces.txt", timeout=10)
                rec = salud.parse_anr_trace(text, self.package, component, reason)
            except Exception:
                rec = None
        if rec is None:
            reason_category = reason.split("(")[0].strip()
            rec = salud.AnrRecord(t=lines[0][:23] if lines else "", pid=self.pid or 0, component=component,
                                   reason=reason, main_frames=[], raw="\n".join(lines),
                                   sig=salud.signature_for("anr", reason_category, [component]))
        self.anrs.append(rec)
        self._write({"type": "anr", **rec.to_dict()})
        self._recompute_groups()

    def _recompute_groups(self) -> None:
        from . import salud
        exit_evs = [e for e in self.proc_events if e.kind == "exit"]
        self.groups = salud.group_events(self.crashes, self.anrs, exit_evs)

    def _check_leaks(self) -> None:
        flags = memoria.LeakDetector().detect_all(list(self.meminfos), list(self.gc_events))
        prev_metrics = {f.metric for f in self.leak_flags}
        self.leak_flags = flags
        for flag in flags:
            if flag.metric not in prev_metrics:
                self._write({"type": "leak", **flag.to_dict()})

    def _meminfo(self, pid: int) -> None:
        text = adbmod.shell(self.dev.serial, f"dumpsys meminfo {pid}", timeout=20)
        mi = parse_meminfo(text, time.time())
        if mi.error:
            self.on_status("warn", f"dumpsys meminfo: {mi.error}")
        self.meminfos.append(mi)
        self._write({"type": "meminfo", **mi.to_dict()})
        self.on_meminfo(mi)
        self._check_leaks()

    def refresh_exits(self) -> None:
        from . import salud
        try:
            text = adbmod.shell(self.dev.serial, f"dumpsys activity exit-info {self.package}", timeout=15)
            records = parse_exit_info(text)
        except Exception:
            return
        self.exits = records
        for r in records:
            key = (r.timestamp, r.pid)
            if key in self._exits_written:
                continue
            self._exits_written.add(key)
            self._write({"type": "exit", **asdict(r)})
            ev = salud.exit_to_event(r)
            self.proc_events.append(ev)
            self._write({"type": "procevent", **ev.to_dict()})
        self._recompute_groups()

    # --- resumen ---
    def summary(self) -> dict:
        gc_summary = {
            "gc_count": len(self.gc_events),
            "gc_pause_ms_total": round(sum(g.pause_ms for g in self.gc_events), 1),
            "leak_metrics": [f.metric for f in self.leak_flags],
            "crash_count": len(self.crashes),
            "anr_count": len(self.anrs),
            "groups": [g.to_dict() for g in self.groups[:5]],
        }
        alive = [s for s in self.samples if s.alive and s.interval > 0]
        if not alive:
            return {"samples": len(self.samples), "alive": 0, **gc_summary}
        cpu = [s.cpu for s in alive]
        rss = [s.rss_kb for s in alive]
        pss = [s.pss_kb for s in alive if s.pss_kb is not None]
        frames = sum(s.frames for s in alive)
        janky = sum(s.janky for s in alive)
        top: Dict[str, float] = {}
        for s in alive:
            for th in s.threads:
                top[th.name] = top.get(th.name, 0.0) + th.cpu * s.interval
        dur = sum(s.interval for s in alive)
        top_threads = sorted(((n, v / dur) for n, v in top.items()), key=lambda x: -x[1])[:8]
        frame_ms = [ms for s in alive for ms in s.frame_ms]
        out = {
            "samples": len(self.samples), "alive": len(alive), "duration_s": round(dur, 1),
            "cpu_avg": round(sum(cpu) / len(cpu), 1), "cpu_max": round(max(cpu), 1),
            "rss_kb_avg": int(sum(rss) / len(rss)), "rss_kb_max": max(rss),
            "pss_kb_avg": int(sum(pss) / len(pss)) if pss else None, "pss_kb_max": max(pss) if pss else None,
            "frames": frames, "janky": janky, "jank_pct": round(janky / frames * 100, 1) if frames else 0.0,
            "fps_avg": round(frames / dur, 1) if dur else 0.0,
            "top_threads": [(n, round(v, 1)) for n, v in top_threads],
            "last_meminfo": self.meminfos[-1].to_dict() if self.meminfos else None,
            "exits": len(self.exits),
            "frame_ms_count": len(frame_ms),
            **gc_summary,
        }
        if frame_ms:
            out["p50"] = round(_percentile(frame_ms, 50), 1)
            out["p90"] = round(_percentile(frame_ms, 90), 1)
            out["p99"] = round(_percentile(frame_ms, 99), 1)
        return out


def load_session(path: Path) -> dict:
    """Lee un JSONL grabado: {'meta':…, 'samples':[…], 'meminfo':[…]}"""
    out = {"meta": None, "samples": [], "meminfo": [], "exits": [], "gc": [], "leak": [],
           "crashes": [], "anrs": [], "procevents": [], "end": None}
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = o.get("type")
            if t == "meta":
                out["meta"] = o
            elif t == "sample":
                out["samples"].append(o)
            elif t == "meminfo":
                out["meminfo"].append(o)
            elif t == "exit":
                out["exits"].append(o)
            elif t == "gc":
                out["gc"].append(o)
            elif t == "leak":
                out["leak"].append(o)
            elif t == "crash":
                out["crashes"].append(o)
            elif t == "anr":
                out["anrs"].append(o)
            elif t == "procevent":
                out["procevents"].append(o)
            elif t == "end":
                out["end"] = o
    return out


def list_sessions() -> List[Path]:
    if not INSPECT_DIR.exists():
        return []
    return sorted(INSPECT_DIR.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
