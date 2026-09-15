"""Explorador de archivos: sandbox de apps (run-as) y directorios del dispositivo (sdcard/tmp/proc)."""
import datetime
import posixpath
import shlex
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional, Tuple

from . import adb as adbmod
from . import config
from . import db

SANDBOX_DIRS = ("databases", "files", "shared_prefs", "cache", "no_backup", "code_cache")
DEVICE_ROOTS = {"sdcard": "/sdcard", "tmp": "/data/local/tmp", "proc": "/proc/{pid}"}

KIND_MAP = {"d": "dir", "-": "file", "l": "link"}
TEXT_EXTS = {"txt", "xml", "json", "log", "prefs", "md", "csv", "properties"}
TEXT_NAMES = {"hosts"}
AUDIT_LOG_NAME = "audit.log"


@dataclass
class FileEntry:
    name: str
    path: str
    kind: str      # dir | file | link | other | error
    size: int
    mtime: str
    perm: str
    owner: str
    link_target: Optional[str]
    message: Optional[str]

    def to_dict(self) -> dict:
        return asdict(self)


def parse_ls(text: str, cur_dir: str = "") -> List[FileEntry]:
    entries: List[FileEntry] = []
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("ls:", "error:", "adb:", "run-as:", "/system/bin/sh:")) or "not found" in stripped or "denied" in stripped.lower():
            entries.append(FileEntry(
                name="", path=cur_dir, kind="error", size=0, mtime="",
                perm="", owner="", link_target=None, message=stripped,
            ))
            continue
        if stripped.startswith("total "):
            continue
        if stripped.startswith("=="):
            continue
        if stripped.endswith(":") and " " not in stripped:
            continue     # cabecera de directorio estilo `ls -lR` (p.ej. "databases:")
        first = stripped[0]
        if first not in "dlcbps-":
            continue
        parts = stripped.split(None, 7)
        if len(parts) < 8:
            continue
        perm, links, owner, group, size_s, date, tm, rest = parts
        name = rest
        link_target = None
        if first == "l" and " -> " in rest:
            name, _, link_target = rest.partition(" -> ")
        if name in (".", ".."):
            continue
        try:
            size = int(size_s)
        except ValueError:
            size = 0
        kind = KIND_MAP.get(first, "other")
        path = f"{cur_dir}/{name}" if cur_dir else name
        entries.append(FileEntry(
            name=name, path=path, kind=kind, size=size, mtime=f"{date} {tm}",
            perm=perm, owner=owner, link_target=link_target, message=None,
        ))
    return entries


def _normalize_rel(rel: Optional[str]) -> str:
    """Unico punto de saneo de rutas relativas: normaliza y rechaza cualquier
    intento de salir de la raiz (rutas absolutas o con segmentos ..)."""
    rel = (rel or "").strip()
    if not rel or rel == ".":
        return ""
    if rel.startswith("/"):
        raise ValueError("ruta fuera de la raíz")
    norm = posixpath.normpath(rel)
    if norm in (".", ""):
        return ""
    if norm == ".." or norm.startswith("../") or norm.startswith("/"):
        raise ValueError("ruta fuera de la raíz")
    return norm


def _device_path(root: str, rel: str, pid: Optional[str]) -> str:
    base = DEVICE_ROOTS[root]
    if "{pid}" in base:
        base = base.format(pid=pid)
    rel = (rel or "").strip("/")
    return f"{base}/{rel}" if rel else base


def _append_audit_log(device_key: str, root: str, path: str, ok: bool) -> None:
    config.DROID_HOME.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().isoformat()
    line = f"{ts}\t{device_key}\t{root}\t{path}\t{ok}\n"
    with open(config.DROID_HOME / AUDIT_LOG_NAME, "a", encoding="utf-8") as fh:
        fh.write(line)


def list_dir(serial: str, root: str, rel: str = "", package: Optional[str] = None, pid: Optional[str] = None) -> List[FileEntry]:
    rel = _normalize_rel(rel)
    if root in DEVICE_ROOTS:
        abs_path = _device_path(root, rel, pid)
        cmd = f"ls -la {shlex.quote(abs_path + '/')} 2>&1"
    else:
        dir_path = rel or "."
        cmd = f"run-as {shlex.quote(package)} ls -la {shlex.quote(dir_path)} 2>&1"
    out = adbmod.shell(serial, cmd, timeout=20)
    return parse_ls(out, rel)


def check_access(serial: str, root: str, package: Optional[str] = None, pid: Optional[str] = None) -> Tuple[bool, str]:
    if root in DEVICE_ROOTS:
        abs_path = _device_path(root, "", pid)
        out = adbmod.shell(serial, f"ls -d {shlex.quote(abs_path)} 2>&1", timeout=10).strip()
        if out.startswith("ls:"):
            return False, out
        return True, out
    ok, msg = db.check_run_as(serial, package)
    if not ok and "not debuggable" in msg:
        return False, f"{package} no es debuggable: run-as no entra; usa /sdcard o /data/local/tmp"
    return ok, msg


def pull_path(serial: str, root: str, rel: str, package: Optional[str], dest_dir: Path,
               entries: Optional[List[FileEntry]] = None, pid: Optional[str] = None) -> List[Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    rel = _normalize_rel(rel)
    device = root in DEVICE_ROOTS
    targets: List[Tuple[str, str]] = []
    if entries:
        for e in entries:
            if e.kind != "file":
                continue
            remote = f"{rel}/{e.name}" if rel else e.name
            targets.append((remote, e.name))
    else:
        name = rel.rsplit("/", 1)[-1] if rel else ""
        targets.append((rel, name))
    pulled: List[Path] = []
    for remote, name in targets:
        local = dest_dir / name
        if device:
            abs_path = _device_path(root, remote, pid)
            r = adbmod.run(["pull", abs_path, str(local)], serial=serial, timeout=120)
            if r.returncode != 0:
                if local.exists():
                    try:
                        local.unlink()
                    except OSError:
                        pass
                err = (r.stderr or "").strip()
                raise adbmod.AdbError(f"adb pull falló: {err[:300]}")
        else:
            adbmod.exec_out(serial, ["run-as", package, "cat", remote], local, timeout=120)
        pulled.append(local)
    return pulled


def delete_path(serial: str, root: str, rel: str, package: Optional[str], is_dir: bool, *,
                 confirmed: bool = False, pid: Optional[str] = None, device_key: Optional[str] = None) -> Tuple[bool, str]:
    if not confirmed:
        raise ValueError("delete_path requiere confirmed=True")
    if root == "proc":
        raise ValueError("no se borra bajo /proc")
    rel = _normalize_rel(rel)
    if not rel:
        raise ValueError("no se borra la raíz: indica una ruta dentro de ella")
    flag = "-rf" if is_dir else "-f"
    if root in DEVICE_ROOTS:
        abs_path = _device_path(root, rel, pid)
        cmd = f"rm {flag} {shlex.quote(abs_path)} 2>&1"
        display_path = abs_path
    else:
        cmd = f"run-as {shlex.quote(package)} rm {flag} {shlex.quote(rel)} 2>&1"
        display_path = rel
    out = adbmod.shell(serial, cmd, timeout=20)
    ok = out.strip() == ""
    _append_audit_log(device_key or serial, root, display_path, ok)
    return ok, out


def preview(serial: str, root: str, rel: str, package: Optional[str], max_bytes: int = 1_000_000,
            pid: Optional[str] = None) -> Tuple[str, bool]:
    rel = _normalize_rel(rel)
    if root in DEVICE_ROOTS:
        abs_path = _device_path(root, rel, pid)
        cmd = f"head -c {max_bytes + 1} {shlex.quote(abs_path)} 2>&1"
    else:
        cmd = f"run-as {shlex.quote(package)} head -c {max_bytes + 1} {shlex.quote(rel)} 2>&1"
    out = adbmod.shell(serial, cmd, timeout=20)
    truncated = len(out) > max_bytes
    text = out[:max_bytes] if truncated else out
    return text, truncated


def preview_binary(serial: str, root: str, rel: str, package: Optional[str], pid: Optional[str] = None,
                    n: int = 512) -> bytes:
    """Lee los primeros `n` bytes REALES (sin pasar por la decodificacion utf-8 de `shell`)
    via exec-out, para que el hexdump de un binario muestre los bytes de verdad."""
    rel = _normalize_rel(rel)
    with tempfile.TemporaryDirectory(prefix="droid-preview-") as tmp:
        dest = Path(tmp) / "preview.bin"
        if root in DEVICE_ROOTS:
            abs_path = _device_path(root, rel, pid)
            adbmod.exec_out(serial, ["head", "-c", str(n), abs_path], dest, timeout=20)
        else:
            adbmod.exec_out(serial, ["run-as", package, "head", "-c", str(n), rel], dest, timeout=20)
        return dest.read_bytes()


def is_text_name(name: str) -> bool:
    base = name.rsplit("/", 1)[-1]
    if base in TEXT_NAMES:
        return True
    if "." not in base:
        return False
    ext = base.rsplit(".", 1)[-1].lower()
    return ext in TEXT_EXTS


def is_prefs_path(rel: str, name: str) -> bool:
    """INV-04: True si la ruta cae bajo shared_prefs/ o el nombre parece un XML de prefs."""
    segments = [s for s in (rel or "").split("/") if s]
    if "shared_prefs" in segments:
        return True
    lname = name.lower()
    return "prefs" in lname and lname.endswith(".xml")
