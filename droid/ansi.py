"""Colores ANSI rápidos para el stream de logs (sin pasar por rich por rendimiento)."""
import os
import sys

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
REV = "\x1b[7m"

_enabled = None


def enabled() -> bool:
    global _enabled
    if _enabled is None:
        if os.environ.get("NO_COLOR"):
            _enabled = False
        elif os.environ.get("DROID_COLOR") == "always":
            _enabled = True
        else:
            _enabled = sys.stdout.isatty()
    return _enabled


def force(value: bool) -> None:
    global _enabled
    _enabled = value


def fg(n: int) -> str:
    return f"\x1b[38;5;{n}m"


def bg(n: int) -> str:
    return f"\x1b[48;5;{n}m"


def style(text: str, *codes: str) -> str:
    if not enabled() or not codes:
        return text
    return "".join(codes) + text + RESET
