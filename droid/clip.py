"""Portapapeles y recortes: copiar texto fuera de la app y guardar secciones de log para revisarlas."""
import os
import platform
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from . import config

CLIPS_DIR = config.DROID_HOME / "clips"


def _backends() -> List[List[str]]:
    if platform.system() == "Darwin":
        return [["pbcopy"]]
    cmds = []
    if shutil.which("wl-copy"):
        cmds.append(["wl-copy"])
    if shutil.which("xclip"):
        cmds.append(["xclip", "-selection", "clipboard"])
    if shutil.which("xsel"):
        cmds.append(["xsel", "--clipboard", "--input"])
    return cmds


def copy(text: str) -> Tuple[bool, str]:
    """Copia al portapapeles del sistema. Devuelve (ok, backend)."""
    if not text:
        return False, "vacío"
    for cmd in _backends():
        try:
            p = subprocess.run(cmd, input=text.encode("utf-8"), timeout=10)
            if p.returncode == 0:
                return True, cmd[0]
        except Exception:
            continue
    return False, "sin portapapeles local"


def save_clip(text: str, prefix: str = "clip", ext: str = "log") -> Path:
    """Guarda un recorte en ~/.droid/clips/ y devuelve la ruta."""
    CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    path = CLIPS_DIR / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{prefix}.{ext}"
    path.write_text(text, encoding="utf-8")
    return path


def open_file(path: Path) -> bool:
    """Abre el archivo en el editor de texto por defecto."""
    try:
        if platform.system() == "Darwin":
            subprocess.Popen(["open", "-t", str(path)])
        else:
            editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "xdg-open"
            subprocess.Popen([editor, str(path)])
        return True
    except Exception:
        return False


def reveal(path: Path) -> bool:
    try:
        if platform.system() == "Darwin":
            subprocess.Popen(["open", "-R", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path.parent)])
        return True
    except Exception:
        return False


GUTTER_RE = re.compile(r"^\s*│ ?")


def unwrap_gutter(text: str) -> str:
    """Une las líneas partidas por el ajuste de pantalla (las que empiezan por el marco │),
    para que una selección hecha con el ratón se pegue como líneas de log completas."""
    out: List[str] = []
    for line in text.splitlines():
        m = GUTTER_RE.match(line)
        if m:
            rest = line[m.end():]
            if out:
                out[-1] = out[-1].rstrip("\n") + rest
            else:
                out.append(rest)
        else:
            out.append(line)
    return "\n".join(out)


# --- extracción de texto de widgets --------------------------------------------

def richlog_lines(widget, visible_only: bool = False) -> List[str]:
    """Texto plano (sin color) de un RichLog: todo el buffer o solo lo que se ve."""
    try:
        strips = widget.lines
    except Exception:
        return []
    if visible_only:
        try:
            y = int(widget.scroll_offset.y)
            h = int(widget.size.height)
            strips = strips[y: y + h]
        except Exception:
            pass
    return [s.text.rstrip() for s in strips]


def _plain(value) -> str:
    if hasattr(value, "plain"):
        return value.plain
    return str(value)


def table_lines(table, only_row: bool = False, sep: str = "\t") -> List[str]:
    """Tabla como texto tabulado (cabecera + filas), o solo la fila del cursor."""
    try:
        header = sep.join(_plain(c.label) for c in table.columns.values())
        if only_row:
            row = table.cursor_row
            if row is None or table.row_count == 0:
                return []
            return [header, sep.join(_plain(v) for v in table.get_row_at(row))]
        out = [header]
        for i in range(table.row_count):
            out.append(sep.join(_plain(v) for v in table.get_row_at(i)))
        return out
    except Exception:
        return []
