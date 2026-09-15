"""Consola dirigida a un dispositivo: ejecuta una línea de shell local en la que `adb` ya apunta al dispositivo (-s serial).

- `adb shell getprop | grep -iE "…"`  → tal cual; el grep corre en el Mac.
- `getprop ro.product.model`           → se antepone `adb shell `.
- `!ls ~/Desktop` o `$ ls`             → shell local sin tocar.
"""
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional, Tuple

from . import adb as adbmod
from . import config

SHELL = "/bin/zsh" if os.path.exists("/bin/zsh") else "/bin/sh"


def wrapper_dir(serial: str) -> Path:
    """Directorio con un `adb` que inyecta -s <serial>; se antepone al PATH."""
    config.ensure_dirs()
    d = config.RUN_DIR / "adbwrap" / adbmod.sanitize(serial)
    d.mkdir(parents=True, exist_ok=True)
    script = d / "adb"
    real = adbmod.adb_path()
    content = f'#!/bin/sh\nexec "{real}" -s "{serial}" "$@"\n'
    if not script.exists() or script.read_text() != content:
        script.write_text(content)
        script.chmod(0o755)
    return d


def normalize(cmd: str) -> Tuple[str, str]:
    """Devuelve (comando_final, modo) con modo en {'adb', 'shell', 'local'}."""
    c = cmd.strip()
    if c.startswith("!"):
        return c[1:].strip(), "local"
    if c.startswith("$ "):
        return c[2:].strip(), "local"
    if c == "adb" or c.startswith("adb "):
        return c, "adb"
    return "adb shell " + c, "shell"


def run(serial: str, cmd: str, on_line: Optional[Callable[[str], None]] = None, timeout: float = 120.0,
        cwd: Optional[str] = None, proc_holder: Optional[dict] = None) -> Tuple[int, float, str]:
    """Ejecuta y devuelve (código, segundos, salida completa). on_line recibe cada línea según llega."""
    final, _ = normalize(cmd)
    env = dict(os.environ)
    env["PATH"] = str(wrapper_dir(serial)) + os.pathsep + env.get("PATH", "")
    env["ANDROID_SERIAL"] = serial          # por si algo llama al adb real
    env["DROID_SERIAL"] = serial
    t0 = time.time()
    proc = subprocess.Popen([SHELL, "-c", final], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, cwd=cwd or os.path.expanduser("~"),
                            start_new_session=True)
    if proc_holder is not None:
        proc_holder["proc"] = proc
    lines = []
    assert proc.stdout is not None
    try:
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            lines.append(line)
            if on_line:
                on_line(line)
            if time.time() - t0 > timeout:
                proc.kill()
                lines.append(f"[droid] cortado por timeout ({timeout:.0f}s)")
                if on_line:
                    on_line(lines[-1])
                break
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
    rc = proc.wait()
    return rc, time.time() - t0, "\n".join(lines)


def kill(proc_holder: dict) -> bool:
    p = proc_holder.get("proc")
    if p and p.poll() is None:
        try:
            os.killpg(os.getpgid(p.pid), 15)
        except Exception:
            try:
                p.terminate()
            except Exception:
                return False
        return True
    return False


def block_mode(text: str) -> str:
    """'local' si la primera línea útil empieza por adb/!/$ ; si no, 'device' (todo el bloque va a adb shell)."""
    for line in text.splitlines():
        t = line.strip()
        if not t or t.startswith("#"):
            continue
        if t == "adb" or t.startswith("adb ") or t.startswith("!") or t.startswith("$ "):
            return "local"
        return "device"
    return "device"


def run_block(serial: str, text: str, on_line: Optional[Callable[[str], None]] = None, timeout: float = 300.0,
              proc_holder: Optional[dict] = None) -> Tuple[int, float, str, str]:
    """Ejecuta un bloque pegado. Devuelve (código, segundos, salida, modo)."""
    mode = block_mode(text)
    env = dict(os.environ)
    env["PATH"] = str(wrapper_dir(serial)) + os.pathsep + env.get("PATH", "")
    env["ANDROID_SERIAL"] = serial
    if mode == "local":
        lines = []
        for line in text.splitlines():
            t = line.lstrip()
            if t.startswith("!"):
                line = line.replace("!", "", 1)
            elif t.startswith("$ "):
                line = line.replace("$ ", "", 1)
            lines.append(line)
        cmd = [SHELL, "-c", "\n".join(lines)]
    else:
        cmd = [adbmod.adb_path(), "-s", serial, "shell", text]
    t0 = time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, cwd=os.path.expanduser("~"), start_new_session=True)
    if proc_holder is not None:
        proc_holder["proc"] = proc
    out = []
    assert proc.stdout is not None
    try:
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            out.append(line)
            if on_line:
                on_line(line)
            if time.time() - t0 > timeout:
                proc.kill()
                out.append(f"[droid] cortado por timeout ({timeout:.0f}s)")
                if on_line:
                    on_line(out[-1])
                break
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
    rc = proc.wait()
    return rc, time.time() - t0, "\n".join(out), mode
