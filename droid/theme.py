"""Identidad de color de droid: el mismo concepto tiene siempre el mismo color, en tablas, menú y logs.

Colores en 256 (ANSI) para el stream de logs y en markup rich para tablas/menú.
"""
import math
from typing import Optional

# --- paletas de identidad (hash estable -> color) ---------------------------
# Vivos y distinguibles entre sí; se usan para dispositivos, procesos, hilos y tags.
IDENTITY = [39, 208, 42, 171, 214, 51, 197, 118, 135, 220, 81, 202, 147, 84, 213, 178]
TAGS = [39, 45, 75, 81, 111, 117, 141, 147, 171, 177, 183, 208, 209, 215, 220, 221, 79, 85, 114, 150, 156, 186, 203, 210]

# --- semántica fija ----------------------------------------------------------
LEVEL_FG = {"V": 245, "D": 75, "I": 78, "W": 214, "E": 196, "F": 201, "S": 245}
LEVEL_BADGE = {"V": (240, 255), "D": (25, 255), "I": (28, 255), "W": (172, 16), "E": (160, 255), "F": (199, 255), "S": (240, 255)}
LEVEL_NAME = {"V": "verbose", "D": "debug", "I": "info", "W": "warn", "E": "error", "F": "fatal"}

TRANSPORT = {"usb": ("USB", 75), "wifi": ("WiFi", 51), "emu": ("Emu", 171), "?": ("?", 245)}
STATE = {
    "device": ("online", 78), "offline": ("offline", 196), "unauthorized": ("sin autorizar", 214),
    "authorizing": ("autorizando", 214), "connecting": ("conectando", 214), "no permissions": ("sin permisos", 196),
    "recovery": ("recovery", 171), "sideload": ("sideload", 171), "bootloader": ("bootloader", 171),
}
SOURCE = {"logs": 51, "record": 171, "ui": 39}   # origen de una sesión de caché
OK, WARN, ERR, INFO, MUTED = 78, 214, 196, 39, 245
CRASH_BG, CRASH_FG = 160, 255                 # FATAL EXCEPTION / ANR / has died
EXC_FG = 203                                  # nombres de excepción
URL_FG = 81
HL_BG, HL_FG = 226, 16                        # resaltado de -g / --hl
MENU_KEY = 220


def color_for(key: str, palette=IDENTITY) -> int:
    """Color estable para una cadena (dispositivo, pid, hilo…)."""
    h = 0
    for ch in key:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return palette[h % len(palette)]


def pid_color(pid: int) -> int:
    # los PIDs consecutivos deben salir distintos: mezcla bits antes del módulo
    x = (pid * 2654435761) & 0xFFFFFFFF
    return IDENTITY[(x >> 7) % len(IDENTITY)]


def thread_color(name: str) -> int:
    return color_for("t:" + name)


def battery_color(level: Optional[str]) -> int:
    try:
        n = int(level or "")
    except ValueError:
        return MUTED
    return OK if n > 50 else (WARN if n > 20 else ERR)


def cpu_color(pct: float) -> int:
    return ERR if pct >= 25 else (WARN if pct >= 5 else (OK if pct > 0 else MUTED))


def size_color(nbytes: float) -> int:
    mb = nbytes / (1024 * 1024)
    return ERR if mb >= 500 else (WARN if mb >= 100 else MUTED)


# --- helpers rich / textual ---------------------------------------------------------
_HEX = {}


def hx(n: int) -> str:
    """Color 256 -> '#rrggbb' (válido en markup de rich y de Textual)."""
    h = _HEX.get(n)
    if h is None:
        from rich.color import Color
        h = Color.from_ansi(n).get_truecolor().hex
        _HEX[n] = h
    return h


def rc(n: int) -> str:
    """Estilo de color para markup (hex; Textual no entiende color(N))."""
    return hx(n)


def paint(text: str, n: int, bold: bool = False) -> str:
    style = hx(n) + (" bold" if bold else "")
    return f"[{style}]{text}[/]"


def transport_markup(transport: str) -> str:
    label, n = TRANSPORT.get(transport, TRANSPORT["?"])
    return paint(label, n, bold=(transport == "usb"))


def state_markup(state: str) -> str:
    label, n = STATE.get(state, (state, MUTED))
    return paint(label, n)


def level_markup(level: str) -> str:
    bg, fg = LEVEL_BADGE.get(level, (240, 255))
    return f"[bold {hx(fg)} on {hx(bg)}] {level} [/]"


def device_markup(key: str, name: str, bold: bool = True) -> str:
    return paint(name, color_for(key), bold=bold)


def battery_markup(level: Optional[str]) -> str:
    if not level:
        return ""
    return paint(f"{level}%", battery_color(level))


def cpu_markup(pct: float) -> str:
    return paint(f"{pct:.1f}", cpu_color(pct))


BARS = "▁▂▃▄▅▆▇█"


def spark(values, width: int = 30, lo: Optional[float] = None, hi: Optional[float] = None) -> str:
    """Sparkline de bloques para una serie (últimos `width` valores)."""
    vals = [float(v) for v in list(values)[-width:] if v is not None]
    if not vals:
        return ""
    lo = min(vals) if lo is None else lo
    hi = max(vals) if hi is None else hi
    span = (hi - lo) or 1.0
    return "".join(BARS[min(7, max(0, int((v - lo) / span * 7.999)))] for v in vals)


def kb(n_kb: Optional[float]) -> str:
    if n_kb is None:
        return "—"
    mb = n_kb / 1024.0
    return f"{mb:.0f} MB" if mb >= 10 else f"{mb:.1f} MB"


def rate(bps: float) -> str:
    if bps >= 1024 * 1024:
        return f"{bps / 1024 / 1024:.1f} MB/s"
    if bps >= 1024:
        return f"{bps / 1024:.0f} KB/s"
    return f"{bps:.0f} B/s"


# --- charts (renderables puros, sin I/O) -------------------------------------


def _finite_or_zero(v) -> float:
    if v is None:
        return 0.0
    try:
        v = float(v)
    except (TypeError, ValueError):
        return 0.0
    return v if math.isfinite(v) else 0.0


def hbar(value: float, max_value: float, width: int = 28, fill: str = "█", empty: str = "░") -> str:
    if width <= 0:
        return ""
    value = _finite_or_zero(value)
    max_value = _finite_or_zero(max_value)
    if max_value <= 0:
        return empty * width
    v = min(max(value, 0), max_value)
    filled = min(width, max(0, round(width * v / max_value)))
    return fill * filled + empty * (width - filled)


def _spark_vals(values, width: int):
    return [float(v) for v in list(values)[-width:] if v is not None]


def _default_fmt(v: float) -> str:
    return f"{v:.1f}"


def spark_labeled(values, width: int = 30, lo: Optional[float] = None, hi: Optional[float] = None,
                   unit: str = "", fmt=None) -> "Text":
    from rich.text import Text
    fmt = fmt or _default_fmt
    glyphs = spark(values, width, lo, hi)
    vals = _spark_vals(values, width)
    if not vals:
        return Text(f"{glyphs} sin datos".strip(), style=hx(MUTED))
    lo_v = min(vals) if lo is None else lo
    hi_v = max(vals) if hi is None else hi
    media = sum(vals) / len(vals)
    trailer = f" mín {fmt(lo_v)}{unit} · máx {fmt(hi_v)}{unit} · media {fmt(media)}{unit}"
    text = Text(glyphs)
    text.append(trailer, style=hx(MUTED))
    return text


def dual_spark(a, b, width: int = 30) -> tuple:
    vals_a = _spark_vals(a, width)
    vals_b = _spark_vals(b, width)
    all_vals = vals_a + vals_b
    hi = max(all_vals) if all_vals else 0.0
    lo = 0.0
    return spark(a, width, lo, hi), spark(b, width, lo, hi)


def histogram(buckets, width: int = 28, fmt=None) -> "Table":
    from rich.table import Table
    fmt = fmt or (lambda v: f"{_finite_or_zero(v):g}")
    table = Table(box=None, show_header=False, padding=(0, 1))
    table.add_column()
    table.add_column()
    table.add_column()
    max_value = max((_finite_or_zero(v) for _, v in buckets), default=0)
    for label, value in buckets:
        table.add_row(label, hbar(value, max_value, width), fmt(value))
    return table


def frame_buckets(durations_ms) -> list:
    labels = ["<16 ms", "16-32 ms", "32-48 ms", ">48 ms"]
    counts = [0, 0, 0, 0]
    for d in durations_ms:
        if d < 16:
            counts[0] += 1
        elif d < 32:
            counts[1] += 1
        elif d < 48:
            counts[2] += 1
        else:
            counts[3] += 1
    return list(zip(labels, counts))


_TIMELINE_GLYPHS = {"crash": "×", "anr": "!", "death": "†", "gc": "·", "lowmem": "▽"}
_TIMELINE_PRIORITY = {"crash": 0, "anr": 1, "death": 2, "lowmem": 3, "gc": 4}


def _timeline_style(kind: str) -> str:
    if kind in ("crash", "anr", "death"):
        return hx(ERR) + " bold"
    if kind == "lowmem":
        return hx(WARN)
    if kind == "gc":
        return hx(MUTED)
    return hx(MUTED)


def timeline_strip(events, t_start: float, t_end: float, width: int = 30, glyphs=None) -> "Text":
    from rich.text import Text
    glyphs = glyphs or _TIMELINE_GLYPHS
    text = Text()
    span = t_end - t_start
    if not events or span <= 0 or width <= 0:
        text.append(" " * max(0, width))
        return text
    slots = [None] * width
    for t, kind in events:
        idx = int((t - t_start) / span * width)
        idx = min(width - 1, max(0, idx))
        current = slots[idx]
        if current is None or _TIMELINE_PRIORITY.get(kind, 99) < _TIMELINE_PRIORITY.get(current, 99):
            slots[idx] = kind
    for kind in slots:
        if kind is None:
            text.append(" ")
        else:
            text.append(glyphs.get(kind, "?"), style=_timeline_style(kind))
    return text
