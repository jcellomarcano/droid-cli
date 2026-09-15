"""Salida con rich (tablas, mensajes) y selección interactiva de dispositivos."""
import sys
from typing import List, Optional

from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich import box

from . import theme
from .adb import Device, dedupe

console = Console(highlight=False)
errc = Console(stderr=True, highlight=False)


class UserError(Exception):
    """Error esperado: se muestra sin traceback y termina con código 1."""


def info(msg: str) -> None:
    console.print(f"[cyan]›[/] {msg}")


def ok(msg: str) -> None:
    console.print(f"[green]✔[/] {msg}")


def warn(msg: str) -> None:
    errc.print(f"[yellow]![/] {msg}")


def fail(msg: str) -> None:
    errc.print(f"[red]✖[/] {msg}")


def devices_table(devices: List[Device], known: Optional[dict] = None, title: Optional[str] = None) -> Table:
    t = Table(box=box.SIMPLE_HEAD, title=title, title_justify="left", pad_edge=False)
    t.add_column("#", justify="right", style="dim")
    t.add_column("Dispositivo", overflow="fold")
    t.add_column("Serial", overflow="fold")
    t.add_column("Vía", no_wrap=True)
    t.add_column("Estado", no_wrap=True)
    t.add_column("Android", overflow="fold")
    t.add_column("IP", overflow="fold")
    t.add_column("Bat", justify="right", no_wrap=True)
    seen = {}
    for i, d in enumerate(devices, 1):
        dup = ""
        if d.online and d.key in seen:
            dup = f" [dim]= #{seen[d.key]}[/]"
        elif d.online:
            seen[d.key] = i
        dot = theme.paint("●", theme.color_for(d.key)) if d.online else "[dim]○[/]"
        if d.alias:
            name = f"{theme.device_markup(d.key, escape(d.alias))} [dim]{escape(d.name)}[/]"
        else:
            name = theme.device_markup(d.key, escape(d.name))
            if d.manufacturer and d.manufacturer.lower() not in d.name.lower():
                name += f" [dim]{escape(d.manufacturer)}[/]"
        android = f"{d.android} [dim](API {d.sdk})[/]" if d.android else ""
        t.add_row(str(i), f"{dot} {name}{dup}", d.serial, theme.transport_markup(d.transport), theme.state_markup(d.state),
                  android, theme.paint(d.ip, theme.TRANSPORT["wifi"][1]) if d.ip else "", theme.battery_markup(d.battery))
    if known:
        for key, e in known.items():
            name = theme.device_markup(key, escape(e["alias"])) + f" [dim]{escape(e.get('model',''))}[/]" if e.get("alias") else theme.device_markup(key, escape(e.get("model", key)))
            t.add_row("", f"[dim]○[/] {name}", f"[dim]{e.get('serial', key)}[/]", theme.transport_markup("wifi"), "[dim]desconectado[/]",
                      f"[dim]{e.get('android','')}[/]", f"[dim]{e.get('ip','')}:{e.get('port','')}[/]", "")
    return t


def _match(d: Device, t: str) -> bool:
    tl = t.lower()
    exact = {d.serial.lower(), d.key.lower(), (d.hw_serial or "").lower(), (d.alias or "").lower(), (d.ip or "").lower()}
    exact.discard("")
    if tl in exact:
        return True
    if d.serial.lower().startswith(tl):
        return True
    if tl in (d.model or "").lower() or tl in (d.product or "").lower():
        return True
    return False


def select_device(
    devices: List[Device],
    target: Optional[str],
    *,
    require_online: bool = True,
    allow_transports: Optional[tuple] = None,
    dedupe_physical: bool = True,
    prompt: str = "Elige un dispositivo",
) -> Device:
    """Resuelve `target` (índice de `droid ls`, serial, alias, modelo, ip, 'usb'/'wifi') o pregunta."""
    pool = [d for d in devices if (d.online or not require_online)]
    if allow_transports:
        pool = [d for d in pool if d.transport in allow_transports]
    if target:
        t = target.strip()
        if t.isdigit():
            idx = int(t)
            if 1 <= idx <= len(devices):
                d = devices[idx - 1]
                if require_online and not d.online:
                    raise UserError(f"El dispositivo #{idx} ({d.serial}) está '{d.state}'.")
                return d
            raise UserError(f"No hay dispositivo #{idx}. Ejecuta `droid ls`.")
        if t.lower() in ("usb", "wifi", "emu"):
            hits = [d for d in pool if d.transport == t.lower()]
        else:
            hits = [d for d in pool if _match(d, t)]
        if dedupe_physical:
            hits = dedupe(hits)
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise UserError(f"Ningún dispositivo coincide con '{target}'. Ejecuta `droid ls`.")
        raise UserError("Varios dispositivos coinciden con '%s': %s" % (target, ", ".join(d.serial for d in hits)))

    cands = dedupe(pool) if dedupe_physical else pool
    if not cands:
        if devices:
            raise UserError("Hay dispositivos pero ninguno está online/autorizado. Revisa `droid ls`.")
        raise UserError("No hay dispositivos conectados.")
    if len(cands) == 1:
        return cands[0]
    if not sys.stdin.isatty():
        raise UserError("Hay varios dispositivos; indica cuál: " + ", ".join(d.serial for d in cands))
    console.print(f"[bold]{prompt}[/]:")
    for i, d in enumerate(cands, 1):
        console.print(f"  {theme.paint(str(i), theme.MENU_KEY, bold=True)}) {theme.device_markup(d.key, escape(d.display))} [dim]· {escape(d.serial)} ·[/] {theme.transport_markup(d.transport)}")
    while True:
        try:
            ans = console.input(f"[dim]›[/] [1-{len(cands)}] (Enter = 1): ").strip()
        except (EOFError, KeyboardInterrupt):
            raise UserError("Cancelado.")
        if ans == "":
            return cands[0]
        if ans.isdigit() and 1 <= int(ans) <= len(cands):
            return cands[int(ans) - 1]
        for d in cands:
            if _match(d, ans):
                return d
        console.print("[red]Opción no válida[/]")


def confirm(msg: str, default: bool = False) -> bool:
    if not sys.stdin.isatty():
        return default
    suffix = "\\[S/n]" if default else "\\[s/N]"
    try:
        ans = console.input(f"{msg} {suffix} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    if ans == "":
        return default
    return ans in ("s", "si", "sí", "y", "yes")


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"
