"""Configuración y reconexión de dispositivos por WiFi (tcpip clásico y pairing Android 11+)."""
import re
import time
from typing import List, Optional

from . import adb as adbmod
from . import config, registry, ui
from .adb import Device


def setup(dev: Device, port: Optional[int] = None) -> str:
    """Activa adb sobre TCP en un dispositivo USB y se conecta. Devuelve host:port."""
    port = port or int(config.get("wifi_port"))
    if dev.transport != "usb":
        raise ui.UserError(f"{dev.label()} no está por USB. Conéctalo por cable para activar el modo WiFi.")
    if not dev.ip:
        detail = dev.ifaces.strip() or "sin interfaces IPv4"
        raise ui.UserError(f"{dev.display} no tiene IP WiFi (interfaces: {detail}). Conéctalo a la misma red WiFi que tu Mac.")
    ui.info(f"Activando adb TCP en {dev.display} ({dev.serial}) puerto {port}…")
    r = adbmod.tcpip(dev.serial, port)
    if r.returncode != 0 or "error" in (r.stdout + r.stderr).lower():
        raise ui.UserError(f"adb tcpip falló: {(r.stdout + r.stderr).strip()}")
    time.sleep(2.0)
    hostport = f"{dev.ip}:{port}"
    ui.info(f"Conectando a {hostport}…")
    out = _connect(hostport)
    registry.upsert(dev, ip=dev.ip, port=port)
    ui.ok(out)
    ui.info("Ya puedes desconectar el cable. Para volver a conectar más tarde: "
            f"[bold]droid wifi connect {dev.alias or dev.ip}[/]")
    return hostport


def _connect(hostport: str) -> str:
    r = adbmod.connect(hostport)
    out = (r.stdout + r.stderr).strip()
    low = out.lower()
    if "connected to" in low or "already connected" in low:
        if not adbmod.wait_online(hostport, timeout=8):
            ui.warn(f"{hostport} figura conectado pero aún no está 'device'. Revisa `droid ls` en unos segundos.")
        return out
    raise ui.UserError(f"No se pudo conectar a {hostport}: {out or 'sin respuesta'}")


def connect(target: Optional[str], all_known: bool = False, port: Optional[int] = None) -> List[str]:
    known = registry.load()
    results = []
    if all_known:
        targets = [registry.hostport(e) for e in known.values() if e.get("ip")]
        if not targets:
            raise ui.UserError("No hay dispositivos WiFi conocidos. Usa `droid wifi setup` primero.")
    elif target:
        if re.fullmatch(r"[\w.\-]+(:\d+)?", target) and re.search(r"\d+\.\d+\.\d+\.\d+", target):
            hp = target if ":" in target else f"{target}:{port or config.get('wifi_port')}"
            targets = [hp]
        else:
            hits = registry.find(target)
            if not hits:
                raise ui.UserError(f"No conozco ningún dispositivo '{target}'. Mira `droid ls -a`.")
            if len(hits) > 1:
                raise ui.UserError("Varios dispositivos coinciden: " + ", ".join(e.get("alias") or k for k, e in hits))
            key, e = hits[0]
            if not e.get("ip"):
                raise ui.UserError(f"{e.get('model', key)} no tiene IP guardada. Conéctalo por USB y ejecuta `droid wifi setup`.")
            targets = [registry.hostport(e)]
    else:
        entries = [(k, e) for k, e in known.items() if e.get("ip")]
        if not entries:
            raise ui.UserError("No hay dispositivos WiFi conocidos. Conecta uno por USB y ejecuta `droid wifi setup`.")
        if len(entries) == 1:
            targets = [registry.hostport(entries[0][1])]
        else:
            ui.console.print("[bold]Dispositivos conocidos[/]:")
            for i, (k, e) in enumerate(entries, 1):
                ui.console.print(f"  [cyan]{i}[/]) {e.get('alias') or e.get('model', k)} · {registry.hostport(e)} · visto {e.get('last_seen', '?')}")
            ans = ui.console.input("[dim]›[/] elige [1-%d] o 'a' para todos: " % len(entries)).strip().lower()
            if ans == "a":
                targets = [registry.hostport(e) for _, e in entries]
            elif ans.isdigit() and 1 <= int(ans) <= len(entries):
                targets = [registry.hostport(entries[int(ans) - 1][1])]
            else:
                raise ui.UserError("Cancelado.")
    for hp in targets:
        try:
            results.append(_connect(hp))
            ui.ok(results[-1])
        except ui.UserError as e:
            ui.fail(str(e))
    return results


def disconnect(target: Optional[str], all_devices: bool = False) -> None:
    if all_devices:
        r = adbmod.disconnect()
        ui.ok((r.stdout + r.stderr).strip() or "desconectados todos")
        return
    if not target:
        raise ui.UserError("Indica un objetivo (ip:puerto, alias) o --all.")
    hp = target
    if not re.search(r"\d+\.\d+\.\d+\.\d+", target):
        hits = registry.find(target)
        if len(hits) != 1:
            raise ui.UserError(f"No identifico '{target}'. Usa ip:puerto o un alias único.")
        hp = registry.hostport(hits[0][1])
    elif ":" not in hp:
        hp = f"{hp}:{config.get('wifi_port')}"
    r = adbmod.disconnect(hp)
    ui.ok((r.stdout + r.stderr).strip())


def pair(hostport: str, code: Optional[str]) -> None:
    """Android 11+: Ajustes › Opciones de desarrollador › Depuración inalámbrica › Vincular con código."""
    if not re.fullmatch(r"[\w.\-]+:\d+", hostport):
        raise ui.UserError("Formato esperado: IP:PUERTO (el puerto de *vinculación* que muestra el diálogo del móvil).")
    if not code:
        code = ui.console.input("Código de vinculación (6 dígitos): ").strip()
    r = adbmod.pair(hostport, code)
    out = (r.stdout + r.stderr).strip()
    if "successfully paired" not in out.lower():
        raise ui.UserError(f"Vinculación fallida: {out}")
    ui.ok(out)
    ip = hostport.split(":")[0]
    # tras vincular, el puerto de conexión es otro: intenta descubrirlo por mDNS
    ui.info("Buscando el puerto de conexión por mDNS…")
    found = None
    for _ in range(6):
        for s in adbmod.mdns_services():
            if s["service"].startswith("_adb-tls-connect") and s["hostport"].startswith(ip + ":"):
                found = s["hostport"]
                break
        if found:
            break
        time.sleep(1)
    if found:
        out = _connect(found)
        ui.ok(out)
        _remember_connected(found)
    else:
        ui.warn("No detecté el puerto por mDNS. Mira el puerto bajo 'Depuración inalámbrica' en el móvil y ejecuta: "
                f"[bold]droid wifi connect {ip}:<puerto>[/]")


def _remember_connected(hostport: str) -> None:
    for d in adbmod.list_devices():
        if d.serial == hostport and d.online:
            ip, _, port = hostport.rpartition(":")
            registry.upsert(d, ip=ip, port=int(port))
            return


def to_usb(dev: Device) -> None:
    r = adbmod.usb(dev.serial)
    ui.ok((r.stdout + r.stderr).strip() or "adb vuelve a modo USB")
