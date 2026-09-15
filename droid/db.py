"""Inspector de base de datos: descubre y copia SQLite de apps debuggables (run-as) y las consulta con sqlite3 local."""
import csv
import io
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

from . import adb as adbmod
from .adb import Device, sanitize
from .inspector import INSPECT_DIR

DB_EXT = (".db", ".sqlite", ".sqlite3", ".database")
SEARCH_DIRS = ("databases", "files", "no_backup", "cache")


@dataclass
class DbFile:
    path: str          # relativo al dir de datos de la app
    size: int
    mtime: str
    wal: bool = False
    shm: bool = False

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]


def check_run_as(serial: str, package: str) -> Tuple[bool, str]:
    out = adbmod.shell(serial, f"run-as {package} id 2>&1", timeout=8).strip()
    if "uid=" in out:
        return True, out
    return False, out or "run-as no respondió"


def list_databases(serial: str, package: str) -> List[DbFile]:
    script = "for d in " + " ".join(SEARCH_DIRS) + "; do [ -d \"$d\" ] && ls -lR \"$d\" 2>/dev/null; done"
    out = adbmod.shell(serial, f"run-as {package} sh -c '{script}'", timeout=20)
    files: dict = {}
    cur_dir = ""
    for line in out.splitlines():
        line = line.rstrip()
        if not line:
            continue
        if line.endswith(":") and " " not in line and not re.match(r"^[-dl][rwxsStT-]{9}", line):
            cur_dir = line[:-1].rstrip("/")     # cabecera de directorio de `ls -lR` (p.ej. "databases:")
            continue
        if line.startswith("total "):
            continue
        parts = line.split(None, 7)
        if len(parts) < 8 or not parts[0].startswith("-"):
            continue
        size, date, tm, name = parts[4], parts[5], parts[6], parts[7]
        try:
            size_i = int(size)
        except ValueError:
            continue
        rel = f"{cur_dir}/{name}" if cur_dir else name
        files[rel] = (size_i, f"{date} {tm}")
    dbs: List[DbFile] = []
    for rel, (size, mtime) in sorted(files.items()):
        low = rel.lower()
        if low.endswith(("-wal", "-shm", "-journal", ".lck")):
            continue
        if low.endswith(DB_EXT) or "/databases/" in rel or rel.startswith("databases/"):
            if low.endswith((".xml", ".json", ".txt", ".pb", ".preferences_pb")):
                continue
            dbs.append(DbFile(rel, size, mtime, wal=(rel + "-wal") in files, shm=(rel + "-shm") in files))
    return dbs


def snapshot_dir(dev: Device, package: str) -> Path:
    d = INSPECT_DIR / f"{sanitize(dev.name)}-{dev.key}" / "db" / sanitize(package) / datetime.now().strftime("%Y%m%d-%H%M%S")
    d.mkdir(parents=True, exist_ok=True)
    return d


def pull_database(serial: str, package: str, remote: str, dest_dir: Path) -> Path:
    """Copia la DB (y -wal/-shm si existen) con `adb exec-out run-as pkg cat`. Devuelve la ruta local."""
    local = dest_dir / remote.rsplit("/", 1)[-1]
    for suffix in ("", "-wal", "-shm"):
        dest = Path(str(local) + suffix)
        try:
            adbmod.exec_out(serial, ["run-as", package, "cat", remote + suffix], dest, timeout=120)
        except adbmod.AdbError as e:
            try:
                dest.unlink()
            except OSError:
                pass
            if suffix == "":
                raise RuntimeError(f"no pude copiar {remote}: {e}") from e
            continue
        if suffix and dest.exists() and dest.stat().st_size == 0:
            try:
                dest.unlink()
            except OSError:
                pass
    return local


def open_db(local: Path) -> sqlite3.Connection:
    # check_same_thread=False: la TUI abre el snapshot en un worker y consulta desde el hilo de la interfaz
    conn = sqlite3.connect(str(local), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def tables(conn: sqlite3.Connection) -> List[Tuple[str, int, str]]:
    """[(nombre, filas, tipo)] para tablas y vistas."""
    out = []
    for name, typ in conn.execute("SELECT name, type FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY type, name"):
        try:
            n = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        except sqlite3.Error:
            n = -1
        out.append((name, n, typ))
    return out


def schema(conn: sqlite3.Connection, table: str) -> List[Tuple[str, str, bool, bool]]:
    """[(columna, tipo, notnull, pk)]"""
    return [(r[1], r[2], bool(r[3]), bool(r[5])) for r in conn.execute(f'PRAGMA table_info("{table}")')]


def create_sql(conn: sqlite3.Connection, table: str) -> str:
    r = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()
    return r[0] if r and r[0] else ""


def query(conn: sqlite3.Connection, sql: str, limit: Optional[int] = 200) -> Tuple[List[str], List[tuple], bool]:
    cur = conn.execute(sql)
    cols = [d[0] for d in cur.description] if cur.description else []
    rows = cur.fetchmany(limit + 1) if limit else cur.fetchall()
    truncated = bool(limit) and len(rows) > limit
    if truncated:
        rows = rows[:limit]
    return cols, [tuple(r) for r in rows], truncated


def rows(conn: sqlite3.Connection, table: str, limit: int = 100, offset: int = 0, order: Optional[str] = None):
    sql = f'SELECT * FROM "{table}"' + (f" ORDER BY {order}" if order else "") + f" LIMIT {int(limit)} OFFSET {int(offset)}"
    return query(conn, sql, None)


def to_csv(cols: List[str], data: List[tuple]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(cols)
    w.writerows(data)
    return buf.getvalue()


def shared_prefs(serial: str, package: str) -> dict:
    """{archivo: contenido xml} de shared_prefs/ (y lista de DataStore .preferences_pb)."""
    out = adbmod.shell(serial, f"run-as {package} sh -c 'ls shared_prefs 2>/dev/null; echo ##DS; ls files/datastore 2>/dev/null'", timeout=15)
    files, ds, section = [], [], "sp"
    for line in out.splitlines():
        s = line.strip()
        if s == "##DS":
            section = "ds"; continue
        if s:
            (files if section == "sp" else ds).append(s)
    result = {}
    for f in files:
        result[f"shared_prefs/{f}"] = adbmod.shell(serial, f"run-as {package} cat shared_prefs/{f}", timeout=15)
    for f in ds:
        result[f"files/datastore/{f}"] = "(DataStore binario; usa --export para copiarlo)"
    return result


def diff_tables(a: sqlite3.Connection, b: sqlite3.Connection) -> List[Tuple[str, int, int]]:
    """Diferencia de filas por tabla entre dos snapshots: [(tabla, filas_a, filas_b)] solo donde cambia."""
    ta = {n: c for n, c, t in tables(a) if t == "table"}
    tb = {n: c for n, c, t in tables(b) if t == "table"}
    out = []
    for n in sorted(set(ta) | set(tb)):
        if ta.get(n) != tb.get(n):
            out.append((n, ta.get(n, -1), tb.get(n, -1)))
    return out
