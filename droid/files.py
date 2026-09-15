"""Explorador de archivos: sandbox de apps (run-as) y directorios del dispositivo (sdcard/tmp/proc)."""
import shlex
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional, Tuple

from . import adb as adbmod
from . import db

SANDBOX_DIRS = ("databases", "files", "shared_prefs", "cache", "no_backup", "code_cache")
DEVICE_ROOTS = {"sdcard": "/sdcard", "tmp": "/data/local/tmp", "proc": "/proc/{pid}"}

KIND_MAP = {"d": "dir", "-": "file", "l": "link"}
TEXT_EXTS = {"txt", "xml", "json", "log", "prefs"}


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
        if stripped.startswith("ls:"):
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


def _device_path(root: str, rel: str, pid: Optional[str]) -> str:
    base = DEVICE_ROOTS[root]
    if "{pid}" in base:
        base = base.format(pid=pid)
    rel = (rel or "").strip("/")
    return f"{base}/{rel}" if rel else base


def list_dir(serial: str, root: str, rel: str = "", package: Optional[str] = None, pid: Optional[str] = None) -> List[FileEntry]:
    rel = (rel or "").strip("/")
    if root in DEVICE_ROOTS:
        abs_path = _device_path(root, rel, pid)
        cmd = f"ls -la {shlex.quote(abs_path)} 2>&1"
    else:
        dir_path = rel or "."
        cmd = f"run-as {package} sh -c 'ls -la {shlex.quote(dir_path)} 2>&1'"
    out = adbmod.shell(serial, cmd, timeout=20)
    return parse_ls(out, rel)


def check_access(serial: str, root: str, package: Optional[str] = None, pid: Optional[str] = None) -> Tuple[bool, str]:
    if root in DEVICE_ROOTS:
        abs_path = _device_path(root, "", pid)
        out = adbmod.shell(serial, f"ls -d {shlex.quote(abs_path)} 2>&1", timeout=10).strip()
        if out.startswith("ls:"):
            return False, out
        return True, out
    return db.check_run_as(serial, package)


def pull_path(serial: str, root: str, rel: str, package: Optional[str], dest_dir: Path, entries: Optional[List[FileEntry]] = None) -> List[Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    rel = (rel or "").strip("/")
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
            abs_path = _device_path(root, remote, None)
            adbmod.run(["pull", abs_path, str(local)], serial=serial, timeout=120)
        else:
            adbmod.exec_out(serial, ["run-as", package, "cat", remote], local, timeout=120)
        pulled.append(local)
    return pulled


def delete_path(serial: str, root: str, rel: str, package: Optional[str], is_dir: bool, *, confirmed: bool = False) -> Tuple[bool, str]:
    if not confirmed:
        raise ValueError("delete_path requiere confirmed=True")
    flag = "-rf" if is_dir else "-f"
    if root in DEVICE_ROOTS:
        abs_path = _device_path(root, rel, None)
        cmd = f"rm {flag} {shlex.quote(abs_path)} 2>&1"
    else:
        cmd = f"run-as {package} sh -c 'rm {flag} {shlex.quote(rel)} 2>&1'"
    out = adbmod.shell(serial, cmd, timeout=20)
    ok = out.strip() == ""
    return ok, out


def preview(serial: str, root: str, rel: str, package: Optional[str], max_bytes: int = 1_000_000) -> Tuple[str, bool]:
    if root in DEVICE_ROOTS:
        abs_path = _device_path(root, rel, None)
        cmd = f"head -c {max_bytes + 1} {shlex.quote(abs_path)} 2>&1"
    else:
        cmd = f"run-as {package} sh -c 'head -c {max_bytes + 1} {shlex.quote(rel)} 2>&1'"
    out = adbmod.shell(serial, cmd, timeout=20)
    truncated = len(out) > max_bytes
    text = out[:max_bytes] if truncated else out
    return text, truncated


def is_text_name(name: str) -> bool:
    if "." not in name:
        return False
    ext = name.rsplit(".", 1)[-1].lower()
    return ext in TEXT_EXTS
