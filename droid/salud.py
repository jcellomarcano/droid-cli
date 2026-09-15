"""Crash/ANR/process-event parsing: pure functions over logcat y trace text.

Representacion de tiempo unica: todo CrashRecord/AnrRecord/ProcEvent.t es un epoch float
(segundos desde epoch, con fraccion de milisegundos), nunca una cadena "HH:MM:SS.mmm"."""
import hashlib
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from . import inspector as inspectormod
from . import logs as logsmod


def _epoch_from_date_time(date: str, time_str: str) -> float:
    """Convierte fecha+hora de logcat threadtime a epoch float; si la fecha no trae anio
    se asume el anio actual (igual que memoria.parse_gc_line)."""
    d = date
    if len(d) == 5:
        d = f"{datetime.now().year}-{d}"
    dt = datetime.strptime(f"{d} {time_str}", "%Y-%m-%d %H:%M:%S.%f")
    return dt.timestamp()


def _epoch_from_full_datetime(text: str) -> float:
    """Convierte 'YYYY-MM-DD HH:MM:SS[.mmm]' (cabecera de traza ANR, exit-info) a epoch float."""
    date_part, _, time_part = text.partition(" ")
    sec_part, _, ms_part = time_part.partition(".")
    struct = time.strptime(f"{date_part} {sec_part}", "%Y-%m-%d %H:%M:%S")
    epoch = time.mktime(struct)
    if ms_part:
        digits = "".join(ch for ch in ms_part if ch.isdigit())[:3].ljust(3, "0")
        if digits:
            epoch += int(digits) / 1000.0
    return epoch

FRAMEWORK_PREFIXES = (
    "android.", "androidx.", "java.", "javax.", "kotlin.", "kotlinx.",
    "dalvik.", "com.android.internal.", "sun.", "libcore.",
)
LOWER_SEG_RE = re.compile(r"^[a-z]{1,2}$")

JAVA_FRAME_RE = re.compile(r"^\tat (.+)$")
FRAME_FILE_LINE_RE = re.compile(r"\([^)]*\)$")

NATIVE_SIGNAL_RE = re.compile(
    r"Fatal signal \d+ \((\w+)\).*?tid (\d+) \(([^)]*)\), pid (\d+) \(([^)]*)\)"
)
NATIVE_BT_FRAME_RE = re.compile(
    r"^\s*#\d+\s+pc\s+[0-9a-fA-F]+\s+(\S+)(?:\s+\(([^+()]+)(?:\+\d+)?\))?"
)

ANR_HEADER_RE = re.compile(
    r"^----- pid (\d+) at (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(?:\.\d+)?(?:[+-]\d{4})? -----$"
)
ANR_THREAD_HEADER_RE = re.compile(r'^"([^"]+)"')
ANR_FRAME_RE = re.compile(r"^\s+at (.+)$")
ANR_IN_RE = re.compile(r"^ANR in (\S+) \(([^)]+)\)")
ANR_REASON_RE = re.compile(r"^Reason: (.+)$")


@dataclass
class CrashRecord:
    t: float
    pid: int
    tid: int
    kind: str
    exc_type: str
    message: str
    frames: List[str]
    raw: str
    sig: str
    obfuscated: bool

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AnrRecord:
    t: float
    pid: int
    component: str
    reason: str
    main_frames: List[str]
    raw: str
    sig: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProcEvent:
    t: float
    kind: str
    pid: int
    process: str
    reason_name: str
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CrashGroup:
    sig: str
    kind: str
    title: str
    count: int
    first_t: float
    last_t: float
    pids: List[int]
    sample_raw: str

    def to_dict(self) -> dict:
        return asdict(self)


# ----------------------------------------------------------------------------- splitting


def _is_block_start(msg: str) -> bool:
    return "FATAL EXCEPTION" in msg or "Fatal signal" in msg or "*** *** ***" in msg


def split_crash_blocks(raw_text: str) -> List[List[str]]:
    blocks: List[List[str]] = []
    current: List[str] = []
    cur_pid = None
    cur_tag = None
    for raw in raw_text.splitlines():
        if raw.startswith("--------- beginning of"):
            if current:
                blocks.append(current)
                current = []
            cur_pid = cur_tag = None
            continue
        ll = logsmod.parse(raw)
        if ll is None:
            if current:
                current.append(raw)
            continue
        if current and ll.pid == cur_pid and ll.tag == cur_tag:
            current.append(raw)
            continue
        if current:
            blocks.append(current)
            current = []
            cur_pid = cur_tag = None
        if _is_block_start(ll.msg):
            current = [raw]
            cur_pid, cur_tag = ll.pid, ll.tag
    if current:
        blocks.append(current)
    return blocks


class CrashBlockSplitter:
    def split(self, raw_text: str) -> List[List[str]]:
        return split_crash_blocks(raw_text)


# ----------------------------------------------------------------------------- normalization


def normalize_frames(frames: List[str], kind: str, app_prefix: Optional[str] = None) -> List[str]:
    if kind == "native":
        return list(frames[:3])
    stripped = [FRAME_FILE_LINE_RE.sub("", f).strip() for f in frames]
    if not stripped:
        return []
    if app_prefix:
        kept = [f for f in stripped if f.startswith(app_prefix)]
        if kept:
            return kept[:5]
    non_fw = [f for f in stripped if not any(f.startswith(p) for p in FRAMEWORK_PREFIXES)]
    if non_fw:
        return non_fw[:5]
    return stripped[:1]


def _is_obfuscated(norm_frames: List[str]) -> bool:
    for f in norm_frames:
        for seg in f.replace("!", ".").split("."):
            if LOWER_SEG_RE.match(seg):
                return True
    return False


def signature_for(kind: str, exc_type: str, norm_frames: List[str]) -> str:
    payload = kind + "|" + exc_type + "|" + "|".join(norm_frames)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


# ----------------------------------------------------------------------------- crash parsing


def _parse_java_crash_block(lines: List[str]) -> Optional[CrashRecord]:
    parsed = [logsmod.parse(l) for l in lines]
    parsed = [p for p in parsed if p is not None]
    if not parsed:
        return None
    if not any("FATAL EXCEPTION" in p.msg for p in parsed):
        return None
    pid = parsed[0].pid
    tid = parsed[0].tid
    t = _epoch_from_date_time(parsed[0].date, parsed[0].time)
    exc_type = None
    message = ""
    frames: List[str] = []
    seen_exc_line = False
    for p in parsed:
        msg = p.msg
        if msg.startswith("FATAL EXCEPTION") or msg.startswith("Process:"):
            continue
        m = JAVA_FRAME_RE.match(msg)
        if m:
            frames.append(m.group(1))
            continue
        if msg.startswith("Caused by:"):
            continue
        if not seen_exc_line:
            idx = msg.find(":")
            if idx != -1:
                exc_type = msg[:idx]
                message = msg[idx + 1:].strip()
            else:
                exc_type = msg
                message = ""
            seen_exc_line = True
    if exc_type is None:
        return None
    raw = "\n".join(lines)
    norm = normalize_frames(frames, "java", None)
    obf = _is_obfuscated(norm)
    sig = signature_for("java", exc_type, norm)
    return CrashRecord(t=t, pid=pid, tid=tid, kind="java", exc_type=exc_type, message=message,
                        frames=frames, raw=raw, sig=sig, obfuscated=obf)


def _parse_native_crash_block(lines: List[str]) -> Optional[CrashRecord]:
    parsed = [logsmod.parse(l) for l in lines]
    parsed = [p for p in parsed if p is not None]
    if not parsed:
        return None
    t = _epoch_from_date_time(parsed[0].date, parsed[0].time)
    signal_name = None
    pid = None
    tid = None
    frames: List[str] = []
    in_bt = False
    for p in parsed:
        msg = p.msg
        m = NATIVE_SIGNAL_RE.search(msg)
        if m:
            signal_name = m.group(1)
            tid = int(m.group(2))
            pid = int(m.group(4))
            continue
        if msg.strip() == "backtrace:":
            in_bt = True
            continue
        if in_bt:
            fm = NATIVE_BT_FRAME_RE.match(msg)
            if fm:
                lib_path = fm.group(1)
                lib_name = lib_path.rsplit("/", 1)[-1]
                sym = fm.group(2)
                if sym:
                    frames.append(f"{lib_name}!{sym}")
                else:
                    frames.append(lib_name)
    if signal_name is None:
        return None
    raw = "\n".join(lines)
    norm = normalize_frames(frames, "native", None)
    sig = signature_for("native", signal_name, norm)
    return CrashRecord(t=t, pid=pid if pid is not None else parsed[0].pid,
                        tid=tid if tid is not None else parsed[0].tid,
                        kind="native", exc_type=signal_name, message="", frames=frames,
                        raw=raw, sig=sig, obfuscated=False)


def parse_crash_block(lines: List[str]) -> Optional[CrashRecord]:
    if not lines:
        return None
    joined = "\n".join(lines)
    if "FATAL EXCEPTION" in joined:
        return _parse_java_crash_block(lines)
    if "Fatal signal" in joined or "backtrace:" in joined:
        return _parse_native_crash_block(lines)
    return None


# ----------------------------------------------------------------------------- ANR parsing


def parse_anr_trace(text: str, package: str, component: Optional[str] = None,
                     reason: Optional[str] = None) -> Optional[AnrRecord]:
    lines = text.splitlines()
    pid = None
    t = None
    for line in lines:
        m = ANR_HEADER_RE.match(line.strip())
        if m:
            pid = int(m.group(1))
            t = _epoch_from_full_datetime(m.group(2))
            break
    if pid is None:
        return None
    main_frames: List[str] = []
    in_main = False
    for line in lines:
        hm = ANR_THREAD_HEADER_RE.match(line.strip())
        if hm:
            in_main = hm.group(1) == "main"
            continue
        if in_main:
            fm = ANR_FRAME_RE.match(line)
            if fm:
                main_frames.append(fm.group(1).strip())
    if not main_frames:
        return None
    comp = component or package
    reas = reason or ""
    reason_category = reas.split("(")[0].strip()
    first_app_frame = main_frames[0]
    sig = signature_for("anr", reason_category, [comp, first_app_frame])
    return AnrRecord(t=t, pid=pid, component=comp, reason=reas, main_frames=main_frames,
                      raw=text, sig=sig)


def parse_anr_logcat(lines: List[str]) -> Optional[Tuple[str, str]]:
    component = None
    reason = None
    for raw in lines:
        ll = logsmod.parse(raw)
        msg = ll.msg if ll is not None else raw
        m = ANR_IN_RE.match(msg)
        if m:
            component = m.group(2)
        m2 = ANR_REASON_RE.match(msg)
        if m2:
            reason = m2.group(1)
    if component is None or reason is None:
        return None
    return component, reason


# ----------------------------------------------------------------------------- process events


def parse_am_line(raw: str) -> Optional[ProcEvent]:
    ll = logsmod.parse(raw)
    if ll is None:
        return None
    msg = ll.msg
    t = _epoch_from_date_time(ll.date, ll.time)
    m = logsmod.AM_START_RE.match(msg)
    if m:
        return ProcEvent(t=t, kind="start", pid=int(m.group(1)), process=m.group(2),
                          reason_name="", detail=msg)
    m = logsmod.AM_DIED_RE.match(msg)
    if m:
        return ProcEvent(t=t, kind="died", pid=int(m.group(2)), process=m.group(1),
                          reason_name="", detail=msg)
    m = logsmod.AM_KILL_RE.match(msg)
    if m:
        return ProcEvent(t=t, kind="killed", pid=int(m.group(1)), process=m.group(2),
                          reason_name="", detail=msg)
    return None


def exit_to_event(rec: "inspectormod.ExitRecord") -> ProcEvent:
    try:
        epoch = _epoch_from_full_datetime(rec.timestamp)
    except ValueError:
        epoch = 0.0
    detail = rec.description or rec.reason_name
    return ProcEvent(t=epoch, kind="exit", pid=rec.pid, process="", reason_name=rec.reason_name,
                      detail=detail)


# ----------------------------------------------------------------------------- grouping


def group_events(crashes: List[CrashRecord], anrs: List[AnrRecord],
                  exits: List[ProcEvent]) -> List[CrashGroup]:
    groups: Dict[str, CrashGroup] = {}

    def _update(sig: str, kind: str, title: str, t, pid: int, raw: str) -> None:
        g = groups.get(sig)
        if g is None:
            groups[sig] = CrashGroup(sig=sig, kind=kind, title=title, count=1, first_t=t,
                                      last_t=t, pids=[pid], sample_raw=raw)
            return
        g.count += 1
        if pid not in g.pids:
            g.pids.append(pid)
        if t < g.first_t:
            g.first_t = t
        if t > g.last_t:
            g.last_t = t

    for c in crashes:
        if c.kind == "java":
            title = f"{c.exc_type}: {c.message[:60]}"
        else:
            top = c.frames[0] if c.frames else ""
            title = f"{c.exc_type} in {top}"
        _update(c.sig, c.kind, title, c.t, c.pid, c.raw)

    for a in anrs:
        _update(a.sig, "anr", a.reason, a.t, a.pid, a.raw)

    for e in exits:
        sig = "exit:" + e.reason_name
        _update(sig, "exit", e.reason_name, e.t, e.pid, e.detail)

    result = list(groups.values())
    result.sort(key=lambda g: g.last_t, reverse=True)
    result.sort(key=lambda g: g.count, reverse=True)
    return result
