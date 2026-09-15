"""Stream de logcat: parseo, filtros (paquete/tag/nivel/regex), color, reconexión."""
import fnmatch
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterator, List, Optional, Set, Tuple

from . import adb as adbmod
from . import ansi, theme

LEVELS = "VDIWEFS"
LEVEL_RANK = {c: i for i, c in enumerate(LEVELS)}

LINE_RE = re.compile(
    r"^(?:(?P<year>\d{4})-)?(?P<md>\d{2}-\d{2}) (?P<time>\d{2}:\d{2}:\d{2}\.\d{3,6})"
    r"\s+(?P<pid>\d+)\s+(?P<tid>\d+) (?P<lvl>[VDIWEFS]) (?P<tag>.*?)\s*:\s?(?P<msg>.*)$"
)
SEP_RE = re.compile(r"^-{5,} (beginning of|switch to) (\w+)")
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})")
AM_START_RE = re.compile(r"^Start proc (\d+):([^/ ]+)/")
AM_DIED_RE = re.compile(r"^Process ([^ ]+) \(pid (\d+)\) has died")
AM_KILL_RE = re.compile(r"^Killing (\d+):([^/ ]+)/")


@dataclass
class LogLine:
    raw: str
    date: str
    time: str
    pid: int
    tid: int
    level: str
    tag: str
    msg: str


def parse(raw: str) -> Optional[LogLine]:
    m = LINE_RE.match(raw)
    if not m:
        return None
    date = m.group("md")
    if m.group("year"):
        date = m.group("year") + "-" + date
    return LogLine(raw, date, m.group("time"), int(m.group("pid")), int(m.group("tid")),
                   m.group("lvl"), m.group("tag"), m.group("msg"))


def normalize_level(s: str) -> str:
    s = (s or "V").strip().upper()
    aliases = {"VERBOSE": "V", "DEBUG": "D", "INFO": "I", "WARN": "W", "WARNING": "W", "ERROR": "E", "FATAL": "F", "ASSERT": "F"}
    s = aliases.get(s, s[:1])
    if s not in LEVEL_RANK:
        raise ValueError(f"Nivel desconocido: {s}")
    return s


# --- Filtros ---------------------------------------------------------------

def _name_match(name: str, patterns: List[str]) -> bool:
    n = name.lower()
    for p in patterns:
        if any(c in p for c in "*?["):
            if fnmatch.fnmatchcase(n, p):
                return True
        elif n == p or p in n:
            return True
    return False


class Filter:
    def __init__(self, packages: List[str] = (), tags: List[str] = (), min_level: str = "V",
                 grep: Optional[str] = None, exclude: Optional[str] = None, ignore_case: bool = True,
                 pids: List[int] = (), tids: List[int] = (), threads: List[str] = ()):
        self.packages = list(packages)
        self.tag_patterns = [t.lower() for t in tags]
        self.min_rank = LEVEL_RANK[normalize_level(min_level)]
        flags = re.IGNORECASE if ignore_case else 0
        self.grep = re.compile(grep, flags) if grep else None
        self.exclude = re.compile(exclude, flags) if exclude else None
        self.pids: Set[int] = set()                 # PIDs resueltos a partir de --package
        self.pid_names = {}
        self.explicit_pids: Set[int] = set(int(x) for x in pids)
        self.explicit_tids: Set[int] = set(int(x) for x in tids)
        self.thread_patterns = [t.lower() for t in threads]
        self.thread_tids: Set[int] = set()          # TIDs que cumplen --thread
        self.tid_names: dict = {}                   # tid -> nombre de hilo (comm)
        self.proc_names: dict = {}                  # pid -> nombre de proceso (todos los procesos)
        self.lock = threading.Lock()

    def set_proc_names(self, mapping: dict) -> None:
        with self.lock:
            self.proc_names = dict(mapping)

    def proc_name(self, pid: int) -> Optional[str]:
        return self.proc_names.get(pid) or self.pid_names.get(pid)

    @property
    def wants_threads(self) -> bool:
        return bool(self.thread_patterns)

    def all_pids(self) -> Set[int]:
        with self.lock:
            return set(self.pids) | self.explicit_pids

    def set_threads(self, mapping: dict) -> Tuple[Set[int], Set[int]]:
        """mapping tid->nombre. Devuelve (nuevos, desaparecidos) entre los que cumplen --thread.
        El hilo principal tiene como comm el nombre del proceso truncado, así que 'main' se resuelve por tid == pid."""
        with self.lock:
            old = set(self.thread_tids)
            self.tid_names = dict(mapping)
            pids = set(self.pids) | self.explicit_pids
            want_main = "main" in self.thread_patterns
            pats = [p for p in self.thread_patterns if p != "main"]
            if self.thread_patterns:
                self.thread_tids = {tid for tid, name in mapping.items()
                                    if (want_main and tid in pids) or (pats and _name_match(name, pats))}
            else:
                self.thread_tids = set()
            new = set(self.thread_tids)
        return new - old, old - new

    def thread_label(self, tid: int, pid: int) -> Optional[str]:
        if tid == pid:
            return "main"
        return self.tid_names.get(tid)

    def thread_name(self, tid: int) -> Optional[str]:
        return self.tid_names.get(tid)

    @property
    def active(self) -> bool:
        return bool(self.packages or self.tag_patterns or self.min_rank or self.grep or self.exclude)

    def describe(self) -> dict:
        return {
            "packages": self.packages,
            "tags": self.tag_patterns,
            "min_level": LEVELS[self.min_rank],
            "grep": self.grep.pattern if self.grep else None,
            "exclude": self.exclude.pattern if self.exclude else None,
            "pids": sorted(self.explicit_pids),
            "tids": sorted(self.explicit_tids),
            "threads": self.thread_patterns,
        }

    def _pkg_match(self, name: str) -> Optional[str]:
        for p in self.packages:
            if name == p or name.startswith(p + ":"):
                return p
        return None

    def observe(self, ll: LogLine) -> Optional[str]:
        """Sigue las líneas de ActivityManager para detectar arranque/muerte de procesos al instante."""
        if not self.packages or ll.tag != "ActivityManager":
            return None
        m = AM_START_RE.match(ll.msg)
        if m:
            pid, name = int(m.group(1)), m.group(2)
            if self._pkg_match(name):
                with self.lock:
                    self.pids.add(pid)
                    self.pid_names[pid] = name
                return f"proceso {name} iniciado (pid {pid})"
            return None
        m = AM_DIED_RE.match(ll.msg) or None
        if m:
            name, pid = m.group(1), int(m.group(2))
            if self._pkg_match(name):
                with self.lock:
                    self.pids.discard(pid)
                    self.pid_names.pop(pid, None)
                return f"proceso {name} murió (pid {pid})"
            return None
        m = AM_KILL_RE.match(ll.msg)
        if m:
            pid, name = int(m.group(1)), m.group(2)
            if self._pkg_match(name):
                with self.lock:
                    self.pids.discard(pid)
                    self.pid_names.pop(pid, None)
                return f"proceso {name} terminado (pid {pid})"
        return None

    def set_pids(self, mapping: dict) -> Tuple[Set[int], Set[int]]:
        """mapping pid->name. Devuelve (nuevos, desaparecidos)."""
        with self.lock:
            old = set(self.pids)
            new = set(mapping)
            self.pids = new
            self.pid_names = dict(mapping)
        return new - old, old - new

    def matches(self, ll: LogLine) -> bool:
        if self.packages or self.explicit_pids:
            with self.lock:
                if ll.pid not in self.pids and ll.pid not in self.explicit_pids:
                    return False
        if self.explicit_tids and ll.tid not in self.explicit_tids:
            return False
        if self.thread_patterns:
            with self.lock:
                if ll.tid not in self.thread_tids:
                    return False
        if LEVEL_RANK.get(ll.level, 0) < self.min_rank:
            return False
        if self.tag_patterns:
            tl = ll.tag.lower()
            if not any(fnmatch.fnmatchcase(tl, p) if any(c in p for c in "*?[") else tl == p for p in self.tag_patterns):
                return False
        if self.grep and not (self.grep.search(ll.msg) or self.grep.search(ll.tag)):
            return False
        if self.exclude and (self.exclude.search(ll.msg) or self.exclude.search(ll.tag)):
            return False
        return True


def list_pids(serial: str, packages: List[str]) -> dict:
    out = adbmod.shell(serial, "ps -A -o PID,NAME 2>/dev/null", timeout=10)
    found = {}
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2 or not parts[0].isdigit():
            continue
        pid, name = int(parts[0]), parts[1].strip()
        for p in packages:
            if name == p or name.startswith(p + ":"):
                found[pid] = name
    return found


def list_threads(serial: str, pids) -> dict:
    """tid -> nombre de hilo (comm) para los PIDs dados. Un solo grep: un fork por hilo tardaba segundos."""
    pids = [str(p) for p in pids]
    if not pids:
        return {}
    globs = " ".join(f"/proc/{p}/task/*/comm" for p in pids)
    out = adbmod.shell(serial, f'grep "" {globs} 2>/dev/null', timeout=15)
    found = {}
    for line in out.splitlines():
        # /proc/2981/task/3452/comm:RenderThread
        path, sep, name = line.partition(":")
        if not sep:
            continue
        parts = path.split("/")
        if len(parts) >= 5 and parts[4].isdigit():
            found[int(parts[4])] = name.strip()
    return found


def list_proc_names(serial: str) -> dict:
    """pid -> nombre de proceso (una llamada ligera)."""
    out = adbmod.shell(serial, "ps -A -o PID,NAME 2>/dev/null", timeout=10)
    found = {}
    for line in out.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            found[int(parts[0])] = parts[1].strip()
    return found


def list_processes(serial: str) -> List[dict]:
    """Procesos del dispositivo: pid, user, cpu, rss(KB), name."""
    out = adbmod.shell(serial, "ps -A -o PID,USER,PCPU,RSS,NAME 2>/dev/null", timeout=15)
    procs = []
    for line in out.splitlines()[1:]:
        parts = line.split(None, 4)
        if len(parts) < 5 or not parts[0].isdigit():
            continue
        try:
            procs.append({"pid": int(parts[0]), "user": parts[1], "cpu": float(parts[2]), "rss": int(parts[3]), "name": parts[4].strip()})
        except ValueError:
            continue
    return procs


def thread_cpu(serial: str, pids) -> dict:
    """tid -> %CPU (ps -T)."""
    pids = [str(p) for p in pids]
    if not pids:
        return {}
    out = adbmod.shell(serial, "ps -T -o TID,PCPU -p " + ",".join(pids) + " 2>/dev/null", timeout=15)
    res = {}
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit():
            try:
                res[int(parts[0])] = float(parts[1])
            except ValueError:
                pass
    return res


class PidWatcher(threading.Thread):
    """Refresca periódicamente los PIDs de los paquetes filtrados (y los hilos si se filtra por hilo)."""

    def __init__(self, get_serial: Callable[[], str], filt: Filter, on_event: Callable[[str], None], interval: float = 2.0,
                 want_thread_names: bool = False, want_proc_names: bool = False, on_refresh: Optional[Callable[[], None]] = None):
        super().__init__(daemon=True)
        self.get_serial = get_serial
        self.filt = filt
        self.on_event = on_event
        self.interval = interval
        self.want_thread_names = want_thread_names
        self.want_proc_names = want_proc_names
        self.on_refresh = on_refresh
        self._first = True
        self._first_threads = True
        self._stop_ev = threading.Event()

    def run(self) -> None:
        while not self._stop_ev.is_set():
            self.tick()
            self._stop_ev.wait(self.interval)

    def tick(self) -> None:
        """Una pasada de resolución (también se llama de forma síncrona antes de arrancar el stream)."""
        try:
            serial = self.get_serial()
            if self.want_proc_names:
                self.filt.set_proc_names(list_proc_names(serial))
                if self.on_refresh:
                    self.on_refresh()
            if self.filt.packages:
                mapping = list_pids(serial, self.filt.packages)
                new, gone = self.filt.set_pids(mapping)
                if self._first:
                    self._first = False
                    if mapping:
                        names = ", ".join(f"{n} (pid {p})" for p, n in sorted(mapping.items()))
                        self.on_event(f"filtrando por proceso: {names}")
                    else:
                        self.on_event("el proceso no está corriendo; esperando a que arranque…")
                else:
                    for p in sorted(new):
                        self.on_event(f"proceso {mapping[p]} detectado (pid {p})")
                    for p in sorted(gone):
                        self.on_event(f"proceso (pid {p}) ya no está")
            if self.filt.wants_threads or self.want_thread_names:
                pids = self.filt.all_pids()
                if pids:
                    tmap = list_threads(serial, pids)
                    new_t, gone_t = self.filt.set_threads(tmap)
                    if self.filt.wants_threads:
                        if self._first_threads:
                            self._first_threads = False
                            if new_t:
                                names = ", ".join(f"{self.filt.thread_label(t, t if t in pids else -1) or tmap[t]} (tid {t})" for t in sorted(new_t))
                                self.on_event(f"filtrando por hilo: {names}")
                            else:
                                self.on_event("ningún hilo coincide todavía con " + ", ".join(self.filt.thread_patterns))
                        else:
                            for t in sorted(new_t):
                                self.on_event(f"hilo {tmap[t]} detectado (tid {t})")
        except Exception:
            if os.environ.get("DROID_DEBUG"):
                import traceback
                traceback.print_exc()

    def stop(self) -> None:
        self._stop_ev.set()


# --- Render ----------------------------------------------------------------

class Renderer:
    """Convierte LogLine en una línea ANSI. Identidad de color: PID (y nombre de proceso) por pid,
    hilo por nombre, tag por nombre, nivel con badge fijo. Resaltado inteligente de crashes/excepciones/URLs."""

    THREAD_WIDTH = 15
    PROC_WIDTH = 20
    SMART = [
        # (regex, códigos, prioridad) — mayor prioridad pinta encima
        (re.compile(r"FATAL EXCEPTION|ANR in |has died|Force finishing|Caused by:|Fatal signal|SIGSEGV|SIGABRT|OutOfMemoryError|StrictMode policy violation"),
         (ansi.bg(theme.CRASH_BG), ansi.fg(theme.CRASH_FG), ansi.BOLD), 3),
        (re.compile(r"\b[A-Z][\w$]*(?:Exception|Error|Throwable)\b"), (ansi.fg(theme.EXC_FG), ansi.BOLD), 2),
        (re.compile(r"https?://[^\s'\"<>)\]]+"), (ansi.fg(theme.URL_FG), "\x1b[4m"), 1),
        (re.compile(r"^\s+at [\w$.<>/]+\(.*?\)$"), (ansi.DIM,), 0),
    ]

    def __init__(self, color: Optional[bool] = None, tag_width: int = 22, show_time: bool = True, show_date: bool = False,
                 show_pid: bool = True, show_tid: bool = False, highlight: Optional[str] = None, wrap: bool = True,
                 show_thread: bool = False, thread_name: Optional[Callable[[int], Optional[str]]] = None,
                 show_proc: bool = False, proc_name: Optional[Callable[[int], Optional[str]]] = None, smart: bool = True):
        self.color = ansi.enabled() if color is None else color
        self.tag_width = max(6, tag_width)
        self.show_time, self.show_date, self.show_pid, self.show_tid = show_time, show_date, show_pid, show_tid
        self.show_thread = show_thread
        self.thread_name = thread_name or (lambda tid: None)
        self.show_proc = show_proc
        self.proc_name = proc_name or (lambda pid: None)
        self.highlight = re.compile(highlight, re.IGNORECASE) if highlight else None
        self.smart = smart
        self.wrap = wrap
        self._width = 0
        self._width_at = 0.0
        self._tag_cache = {}

    def width(self) -> int:
        now = time.time()
        if now - self._width_at > 1.0:
            self._width = shutil.get_terminal_size((160, 40)).columns
            self._width_at = now
        return self._width

    def set_width(self, width: int) -> None:
        """Ancho fijo (p.ej. dentro de la TUI)."""
        self._width = width
        self._width_at = float("inf")

    def _tag_color(self, tag: str) -> int:
        c = self._tag_cache.get(tag)
        if c is None:
            c = theme.color_for(tag, theme.TAGS)
            self._tag_cache[tag] = c
        return c

    def _st(self, text: str, *codes: str) -> str:
        return ansi.style(text, *codes) if self.color else text

    def header(self, ll: LogLine) -> Tuple[str, int]:
        parts, width = [], 0
        if self.show_date:
            parts.append(self._st(ll.date, ansi.DIM)); width += len(ll.date) + 1
        if self.show_time:
            t = ll.time[:12]
            parts.append(self._st(t, ansi.DIM)); width += len(t) + 1
        pid_code = ansi.fg(theme.pid_color(ll.pid))
        if self.show_pid:
            parts.append(self._st(f"{ll.pid:>5}", pid_code)); width += 6
        if self.show_proc:
            name = self.proc_name(ll.pid) or "?"
            if len(name) > self.PROC_WIDTH:
                name = "…" + name[-(self.PROC_WIDTH - 1):]
            parts.append(self._st(f"{name:<{self.PROC_WIDTH}}", pid_code)); width += self.PROC_WIDTH + 1
        if self.show_tid:
            parts.append(self._st(f"{ll.tid:>5}", ansi.DIM)); width += 6
        if self.show_thread:
            name = ("main" if ll.tid == ll.pid else None) or self.thread_name(ll.tid) or str(ll.tid)
            name = name[:self.THREAD_WIDTH]
            parts.append(self._st(f"{name:<{self.THREAD_WIDTH}}", ansi.fg(theme.thread_color(name)))); width += self.THREAD_WIDTH + 1
        bgc, fgc = theme.LEVEL_BADGE.get(ll.level, (240, 255))
        parts.append(self._st(f" {ll.level} ", ansi.bg(bgc), ansi.fg(fgc), ansi.BOLD)); width += 4
        tag = ll.tag
        if len(tag) > self.tag_width:
            tag = "…" + tag[-(self.tag_width - 1):]
        parts.append(self._st(f"{tag:>{self.tag_width}}", ansi.fg(self._tag_color(ll.tag)))); width += self.tag_width + 1
        sep = self._st("│", ansi.DIM)
        return " ".join(parts) + " " + sep + " ", width + 2

    def _spans(self, msg: str) -> List[Tuple[int, int, int, Tuple[str, ...]]]:
        spans = []
        if self.smart:
            for rx, codes, prio in self.SMART:
                for m in rx.finditer(msg):
                    spans.append((m.start(), m.end(), prio, codes))
        if self.highlight:
            for m in self.highlight.finditer(msg):
                if m.end() > m.start():
                    spans.append((m.start(), m.end(), 9, (ansi.bg(theme.HL_BG), ansi.fg(theme.HL_FG))))
        return spans

    def _paint_range(self, msg: str, a: int, b: int, base: str, style_at: Optional[list]) -> str:
        chunk = msg[a:b]
        if not self.color:
            return chunk
        if style_at is None:
            return base + chunk + ansi.RESET if base else chunk
        out, cur, run = [], None, []
        for i in range(a, b):
            st = style_at[i]
            if st != cur:
                if run:
                    out.append("".join(run))
                run = []
                cur = st
                out.append(ansi.RESET + base + (("".join(st)) if st else ""))
            run.append(msg[i])
        if run:
            out.append("".join(run))
        return "".join(out) + ansi.RESET

    def line(self, ll: LogLine) -> str:
        head, hw = self.header(ll)
        base = ""
        if self.color and ll.level in ("W", "E", "F"):
            base = ansi.fg(theme.LEVEL_FG[ll.level]) + (ansi.BOLD if ll.level == "F" else "")
        msg = ll.msg
        style_at = None
        if self.color:
            spans = self._spans(msg)
            if spans:
                style_at = [None] * len(msg)
                for s0, e0, _, codes in sorted(spans, key=lambda x: x[2]):
                    for i in range(s0, min(e0, len(msg))):
                        style_at[i] = codes
        avail = self.width() - hw
        if not self.wrap or avail < 20 or len(msg) <= avail:
            return head + self._paint_range(msg, 0, len(msg), base, style_at)
        cont = " " * (hw - 2) + self._st("│", ansi.DIM) + " "
        pieces = []
        for i, start in enumerate(range(0, len(msg), avail)):
            pieces.append((head if i == 0 else cont) + self._paint_range(msg, start, min(start + avail, len(msg)), base, style_at))
        return "\n".join(pieces)

    def separator(self, raw: str) -> str:
        return self._st(raw, ansi.DIM)

    def banner(self, text: str, kind: str = "info") -> str:
        colors = {"info": theme.INFO, "ok": theme.OK, "warn": theme.WARN, "err": theme.ERR}
        return self._st(f"─── {text} ", ansi.fg(colors.get(kind, theme.INFO)), ansi.BOLD)

    def plain(self, ll: LogLine) -> str:
        return ll.raw


# --- Stream con reconexión --------------------------------------------------

class LogcatStream:
    def __init__(self, serial: str, key: str, buffers: Optional[List[str]] = None, tail: Optional[int] = None,
                 clear: bool = False, reconnect: bool = True, status: Optional[Callable[[str, str], None]] = None,
                 since: Optional[str] = None):
        self.serial = serial
        self.key = key
        self.buffers = buffers or []
        self.tail = tail
        self.since = since              # 'YYYY-MM-DD HH:MM:SS.mmm': continuar desde aquí (reinicio con nuevos filtros)
        self.clear = clear
        self.reconnect = reconnect
        self.status = status or (lambda kind, text: None)
        self.stopped = False
        self.proc: Optional[subprocess.Popen] = None
        self.last_ts: Optional[str] = None
        self.use_year = True
        self.reconnects = 0

    def _cmd(self, since: Optional[str], tail: Optional[int]) -> List[str]:
        cmd = [adbmod.adb_path(), "-s", self.serial, "logcat", "-v", "threadtime,year" if self.use_year else "threadtime"]
        for b in self.buffers:
            cmd += ["-b", b]
        if since:
            cmd += ["-T", since]
        elif tail is not None:
            cmd += ["-T", str(tail)]
        return cmd

    def _launch(self, since: Optional[str], tail: Optional[int]) -> subprocess.Popen:
        self.proc = subprocess.Popen(self._cmd(since, tail), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return self.proc

    def stream(self) -> Iterator[Tuple[str, str]]:
        if self.clear:
            adbmod.run(["logcat", "-c"] + sum([["-b", b] for b in self.buffers], []), serial=self.serial, timeout=15)
        since = self.since
        first = True
        while not self.stopped:
            started = time.time()
            proc = self._launch(since, self.tail if first else None)
            got_lines = 0
            first_line = None
            assert proc.stdout is not None
            for rawb in iter(proc.stdout.readline, b""):
                raw = rawb.decode("utf-8", "replace").rstrip("\r\n")
                got_lines += 1
                if first_line is None:
                    first_line = raw
                m = TS_RE.match(raw)
                if m:
                    self.last_ts = m.group(1)
                yield ("line", raw)
            rc = proc.wait()
            if self.stopped:
                break
            # logcat viejo sin soporte de "-v year": reintenta sin él
            if self.use_year and got_lines <= 2 and time.time() - started < 2 and first_line and (
                    "Invalid" in first_line or "Unknown" in first_line or "usage" in first_line.lower()):
                self.use_year = False
                self.status("warn", "este logcat no soporta '-v year'; usando formato clásico")
                continue
            first = False
            if not self.reconnect:
                yield ("end", f"logcat terminó (código {rc})")
                break
            yield ("lost", f"se perdió {self.serial}; esperando a que vuelva… (Ctrl+C para salir)")
            new_serial = self._wait_for_device()
            if not new_serial:
                break
            self.reconnects += 1
            self.serial = new_serial
            since = self.last_ts
            yield ("reconnected", f"reconectado a {new_serial}" + (f" (desde {since})" if since else ""))

    def _wait_for_device(self) -> Optional[str]:
        last_connect = 0.0
        while not self.stopped:
            time.sleep(1.5)
            try:
                devs = adbmod.list_devices(details=False)
            except Exception:
                continue
            for d in devs:
                if d.serial == self.serial and d.online:
                    return d.serial
            if adbmod.is_hostport(self.serial) and time.time() - last_connect > 6:
                last_connect = time.time()
                try:
                    adbmod.connect(self.serial)
                except Exception:
                    pass
                continue
            # ¿volvió por otra vía (USB<->WiFi)? compara el serial físico
            online = [d for d in devs if d.online]
            for d in adbmod.dedupe([adbmod.fill_details(x, timeout=5) for x in online]):
                if d.key == self.key:
                    return d.serial
        return None

    def close(self) -> None:
        self.stopped = True
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
