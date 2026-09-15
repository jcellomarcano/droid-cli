"""Identidad de color de droid: el mismo concepto tiene siempre el mismo color, en tablas, menú y logs.

Colores en 256 (ANSI) para el stream de logs y en markup rich para tablas/menú.
"""
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
