"""Analisis de memoria: parseo de eventos GC de ART y deteccion de posibles fugas por pendiente."""
import re
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

from . import logs as logsmod

_UNIT_TO_KB = {"B": 1 / 1024, "KB": 1.0, "MB": 1024.0, "GB": 1024.0 * 1024.0}
_UNIT_TO_MS = {"us": 1 / 1000, "ms": 1.0, "s": 1000.0}

GC_RE = re.compile(
    r"^(?P<kind>.+? GC) freed "
    r"(?:(?P<freed_num>\d+(?:\.\d+)?)(?P<freed_unit>B|KB|MB|GB) AllocSpace bytes"
    r"|(?P<freed_objs>\d+)\((?P<freed_size>\d+(?:\.\d+)?)(?P<freed_size_unit>B|KB|MB|GB)\) AllocSpace objects), "
    r"(?P<los_objs>\d+)\((?P<los_size>\d+(?:\.\d+)?)(?P<los_unit>B|KB|MB|GB)\) LOS objects, "
    r"(?P<free_pct>\d+)% free, "
    r"(?P<heap_used>\d+(?:\.\d+)?)(?P<heap_used_unit>B|KB|MB|GB)/(?P<heap_total>\d+(?:\.\d+)?)(?P<heap_total_unit>B|KB|MB|GB), "
    r"paused (?P<paused>[\d.]+(?:us|ms|s)(?:,[\d.]+(?:us|ms|s))*) "
    r"total (?P<total>\d+(?:\.\d+)?)(?P<total_unit>us|ms|s)$"
)
_PAUSE_TOKEN_RE = re.compile(r"([\d.]+)(us|ms|s)")


def _to_kb(value: str, unit: str) -> float:
    return float(value) * _UNIT_TO_KB[unit]


def _to_ms(value: str, unit: str) -> float:
    return float(value) * _UNIT_TO_MS[unit]


@dataclass
class GcEvent:
    t: float
    kind: str
    freed_kb: float
    los_kb: float
    free_pct: Optional[int]
    heap_used_kb: float
    heap_total_kb: float
    pause_ms: float
    total_ms: float

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def parse_gc_message(msg: str) -> Optional[GcEvent]:
    m = GC_RE.match(msg.strip())
    if not m:
        return None
    if m.group("freed_unit"):
        freed_kb = _to_kb(m.group("freed_num"), m.group("freed_unit"))
    else:
        freed_kb = _to_kb(m.group("freed_size"), m.group("freed_size_unit"))
    los_kb = _to_kb(m.group("los_size"), m.group("los_unit"))
    free_pct = int(m.group("free_pct"))
    heap_used_kb = _to_kb(m.group("heap_used"), m.group("heap_used_unit"))
    heap_total_kb = _to_kb(m.group("heap_total"), m.group("heap_total_unit"))
    pause_ms = sum(_to_ms(v, u) for v, u in _PAUSE_TOKEN_RE.findall(m.group("paused")))
    total_ms = _to_ms(m.group("total"), m.group("total_unit"))
    return GcEvent(
        t=0.0,
        kind=m.group("kind"),
        freed_kb=freed_kb,
        los_kb=los_kb,
        free_pct=free_pct,
        heap_used_kb=heap_used_kb,
        heap_total_kb=heap_total_kb,
        pause_ms=pause_ms,
        total_ms=total_ms,
    )


def parse_gc_line(raw: str) -> Optional[GcEvent]:
    """Parsea una linea logcat threadtime; si la fecha no trae anio se asume el anio actual."""
    ll = logsmod.parse(raw)
    if ll is None:
        return None
    event = parse_gc_message(ll.msg)
    if event is None:
        return None
    date = ll.date
    if len(date) == 5:
        date = f"{datetime.now().year}-{date}"
    dt = datetime.strptime(f"{date} {ll.time}", "%Y-%m-%d %H:%M:%S.%f")
    event.t = dt.timestamp()
    return event


@dataclass
class LeakFlag:
    metric: str
    slope_kb_per_min: float
    samples: int
    since_t: float
    until_t: float
    first_kb: float
    last_kb: float
    gc_backed: bool
    epistemic: str = "Inferido"

    def to_dict(self) -> dict:
        return dict(self.__dict__)


def _lstsq_slope(points: List[Tuple[float, float]]) -> float:
    n = len(points)
    mean_t = sum(p[0] for p in points) / n
    mean_kb = sum(p[1] for p in points) / n
    num = sum((t - mean_t) * (kb - mean_kb) for t, kb in points)
    den = sum((t - mean_t) ** 2 for t, kb in points)
    if den == 0:
        return 0.0
    return num / den


class LeakDetector:
    def check(
        self,
        points: List[Tuple[float, float]],
        gcs: List[GcEvent] = (),
        min_samples: int = 6,
        min_span_s: float = 60.0,
        min_growth_pct: float = 5.0,
        min_growth_kb: float = 2048.0,
        metric: str = "",
    ) -> Optional[LeakFlag]:
        pts = sorted(points, key=lambda p: p[0])
        if len(pts) < min_samples:
            return None
        since_t, first_kb = pts[0]
        until_t, last_kb = pts[-1]
        if until_t - since_t < min_span_s:
            return None
        slope_per_s = _lstsq_slope(pts)
        if slope_per_s <= 0:
            return None
        growth = last_kb - first_kb
        threshold = max(first_kb * min_growth_pct / 100.0, min_growth_kb)
        if growth < threshold:
            return None
        window_gcs = [g for g in gcs if since_t <= g.t <= until_t]
        gc_backed = False
        if window_gcs:
            last_gc = max(window_gcs, key=lambda g: g.t)
            after = [p for p in pts if p[0] > last_gc.t]
            if not after:
                return None
            first_after_kb = after[0][1]
            if first_after_kb < first_kb * (1 + min_growth_pct / 100.0):
                return None
            gc_backed = True
        return LeakFlag(
            metric=metric,
            slope_kb_per_min=slope_per_s * 60.0,
            samples=len(pts),
            since_t=since_t,
            until_t=until_t,
            first_kb=first_kb,
            last_kb=last_kb,
            gc_backed=gc_backed,
        )

    def detect_all(self, meminfos, gcs: List[GcEvent] = (), **kwargs) -> List[LeakFlag]:
        flags = []
        for metric in ("java_heap_kb", "native_heap_kb", "pss_total_kb"):
            points = [(m.t, float(getattr(m, metric))) for m in meminfos]
            flag = self.check(points, gcs, metric=metric, **kwargs)
            if flag is not None:
                flags.append(flag)
        return flags
