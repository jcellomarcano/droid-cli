"""Punto de entrada: `droid <comando>`."""
import argparse
import json
import os
import re
import sys
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from rich.markup import escape
from rich.table import Table
from rich.text import Text
from rich import box

from . import __version__
from . import adb as adbmod
from . import ansi, cache, config, record, registry, theme, ui, wifi
from .logs import Filter, LogcatStream, PidWatcher, Renderer, SEP_RE, parse


# ---------------------------------------------------------------- ls

def _ls_table(devices, known):
    from rich.console import Group
    online = [d for d in devices if d.online]
    physical = adbmod.dedupe(online)
    usb = sum(1 for d in online if d.transport == "usb")
    wf = sum(1 for d in online if d.transport == "wifi")
    both = sum(1 for k in {d.key for d in physical} if {d.transport for d in online if d.key == k} >= {"usb", "wifi"})
    parts = [f"{len(physical)} dispositivo(s) físico(s)", f"{usb} USB", f"{wf} WiFi"]
    if both:
        parts.append(f"{both} por ambas vías")
    bad = [d for d in devices if not d.online]
    if bad:
        parts.append(f"[yellow]{len(bad)} sin conexión útil[/]")
    return Group(ui.devices_table(devices, known), Text.from_markup("[dim]" + " · ".join(parts) + "[/]"))


def cmd_ls_watch(args) -> int:
    """Tabla en vivo: se actualiza al instante con adb track-devices y cada --interval s (batería/IP)."""
    import threading as _th
    import time as _time
    from rich.live import Live
    from datetime import datetime as _dt
    changed = _th.Event()
    last_serials = {}

    def _on_change(devs):
        nonlocal last_serials
        now = {d.serial: d.state for d in devs}
        if now != last_serials:
            last_serials = now
            changed.set()
    tracker = adbmod.DeviceTracker(_on_change)
    tracker.start()
    import signal as _sig

    def _term(signum, frame):
        raise KeyboardInterrupt
    for _s in (_sig.SIGTERM, _sig.SIGHUP):
        try:
            _sig.signal(_s, _term)
        except Exception:
            pass

    def _render():
        devices = adbmod.list_devices()
        registry.touch_all(devices)
        online_keys = {d.key for d in devices if d.online}
        known = {k: e for k, e in registry.load().items() if k not in online_keys and e.get("ip")} if args.all else None
        from rich.console import Group
        return Group(_ls_table(devices, known), Text(f"⟳ en vivo · {_dt.now().strftime('%H:%M:%S')} · Ctrl+C para salir", style="dim"))
    try:
        with Live(_render(), console=ui.console, refresh_per_second=4) as live:
            while True:
                fired = changed.wait(timeout=max(1.0, float(args.interval)))
                changed.clear()
                if fired:
                    _time.sleep(0.6)      # deja que adb complete la conexión antes de pedir detalles
                live.update(_render())
    except KeyboardInterrupt:
        pass
    finally:
        tracker.stop()
    return 0


def cmd_refresh(args) -> int:
    """Fuerza a adb a re-detectar dispositivos y vuelve a listar."""
    if args.hard:
        ui.info("Reiniciando el servidor adb…")
        out = adbmod.restart_server()
    else:
        ui.info("adb reconnect offline (resetea dispositivos offline/sin autorizar)…")
        out = adbmod.reconnect("offline")
    if out:
        ui.console.print(f"[dim]{escape(out)}[/]")
    import time as _time
    _time.sleep(1.5 if not args.hard else 3.0)
    args.json = False
    args.watch = False
    return cmd_ls(args)


def cmd_ls(args) -> int:
    if getattr(args, "watch", False):
        return cmd_ls_watch(args)
    if getattr(args, "reconnect", False):
        out = adbmod.reconnect("offline")
        if out:
            ui.console.print(f"[dim]{escape(out)}[/]")
        import time as _time
        _time.sleep(1.5)
    devices = adbmod.list_devices()
    registry.touch_all(devices)
    online_keys = {d.key for d in devices if d.online}
    known = {}
    if getattr(args, "all", False):
        known = {k: e for k, e in registry.load().items() if k not in online_keys and e.get("ip")}
    if getattr(args, "json", False):
        print(json.dumps({"devices": [d.to_dict() for d in devices], "known_offline": known}, indent=2, ensure_ascii=False))
        return 0
    if not devices and not known:
        ui.warn("No hay dispositivos conectados. Conecta uno por USB o usa `droid wifi connect`.")
        return 0
    ui.console.print(ui.devices_table(devices, known))
    online = [d for d in devices if d.online]
    physical = adbmod.dedupe(online)
    usb = sum(1 for d in online if d.transport == "usb")
    wf = sum(1 for d in online if d.transport == "wifi")
    both = sum(1 for k in {d.key for d in physical} if {d.transport for d in online if d.key == k} >= {"usb", "wifi"})
    parts = [f"{len(physical)} dispositivo(s) físico(s)", f"{usb} USB", f"{wf} WiFi"]
    if both:
        parts.append(f"{both} por ambas vías")
    bad = [d for d in devices if not d.online]
    if bad:
        parts.append(f"[yellow]{len(bad)} sin conexión útil[/]")
    ui.console.print("[dim]" + " · ".join(parts) + "[/]")
    for d in bad:
        if d.state == "unauthorized":
            ui.warn(f"{d.serial}: acepta el diálogo de depuración USB en el dispositivo.")
        elif d.state == "offline" and d.transport == "wifi":
            ui.warn(f"{d.serial}: offline. Prueba `droid wifi disconnect {d.serial}` y `droid wifi connect {d.serial}`.")
    offline_known = [k for k in registry.load() if k not in online_keys]
    if offline_known and not getattr(args, "all", False):
        ui.console.print(f"[dim]{len(offline_known)} dispositivo(s) WiFi conocido(s) sin conectar → `droid ls -a` / `droid wifi connect`[/]")
    return 0


# ---------------------------------------------------------------- wifi

def cmd_wifi_setup(args) -> int:
    devices = adbmod.list_devices()
    dev = ui.select_device(devices, args.device, allow_transports=("usb",), prompt="¿Qué dispositivo USB pasar a WiFi?")
    wifi.setup(dev, args.port)
    return 0


def cmd_wifi_connect(args) -> int:
    wifi.connect(args.target, all_known=args.all, port=args.port)
    return 0


def cmd_wifi_disconnect(args) -> int:
    wifi.disconnect(args.target, all_devices=args.all)
    return 0


def cmd_wifi_pair(args) -> int:
    wifi.pair(args.hostport, args.code)
    return 0


def cmd_wifi_usb(args) -> int:
    devices = adbmod.list_devices()
    dev = ui.select_device(devices, args.device, prompt="¿Qué dispositivo devolver a modo USB?")
    wifi.to_usb(dev)
    # limpia las entradas TCP de ese mismo dispositivo (quedarían 'offline')
    for d in devices:
        if d.key == dev.key and adbmod.is_hostport(d.serial):
            adbmod.disconnect(d.serial)
            ui.info(f"desconectada la entrada {d.serial}")
    return 0


def cmd_wifi_forget(args) -> int:
    hits = registry.find(args.target)
    if len(hits) != 1:
        raise ui.UserError(f"No identifico '{args.target}' de forma única. Mira `droid ls -a`.")
    registry.remove(hits[0][0])
    ui.ok(f"Olvidado {hits[0][1].get('alias') or hits[0][1].get('model') or hits[0][0]}")
    return 0


# ---------------------------------------------------------------- logs

def _apply_color_flag(value: Optional[str]) -> None:
    if value == "always":
        ansi.force(True)
    elif value == "never":
        ansi.force(False)


def _make_filter(args, live: bool = True) -> Filter:
    try:
        f = Filter(packages=args.package or [], tags=args.tag or [], min_level=args.level or "V",
                   grep=args.grep, exclude=args.exclude, pids=args.pid or [], tids=args.tid or [],
                   threads=args.thread or [])
    except ValueError as e:
        raise ui.UserError(str(e))
    if live and f.thread_patterns and not (f.packages or f.explicit_pids):
        raise ui.UserError("--thread necesita saber de qué proceso: añade -p <paquete> o --pid <pid> (mira `droid ps`).")
    return f


def _make_renderer(args, filt: Optional[Filter] = None) -> Renderer:
    show_tid = args.show_tid or bool(args.tid)
    show_thread = args.show_thread or bool(args.thread)
    return Renderer(tag_width=args.tag_width or int(config.get("tag_width")), show_time=not args.no_time,
                    show_date=args.date, show_pid=not args.no_pid, show_tid=show_tid,
                    highlight=args.highlight or args.grep, wrap=not args.no_wrap,
                    show_thread=show_thread, thread_name=(filt.thread_name if filt else None),
                    show_proc=args.show_proc, proc_name=(filt.proc_name if filt else None), smart=not args.no_smart)


def _filters_text(filt: Filter) -> str:
    d = filt.describe()
    bits = []
    if d["packages"]:
        bits.append("pkg=" + ",".join(d["packages"]))
    if d["tags"]:
        bits.append("tag=" + ",".join(d["tags"]))
    if d["min_level"] != "V":
        bits.append("nivel≥" + d["min_level"])
    if d["grep"]:
        bits.append(f"grep=/{d['grep']}/")
    if d["exclude"]:
        bits.append(f"excluye=/{d['exclude']}/")
    if d.get("pids"):
        bits.append("pid=" + ",".join(map(str, d["pids"])))
    if d.get("tids"):
        bits.append("tid=" + ",".join(map(str, d["tids"])))
    if d.get("threads"):
        bits.append("hilo=" + ",".join(d["threads"]))
    return " ".join(bits) if bits else "sin filtros"


def cmd_logs(args) -> int:
    _apply_color_flag(args.color)
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    devices = adbmod.list_devices()
    dev = ui.select_device(devices, args.device, prompt="¿De qué dispositivo ver logs?")
    filt = _make_filter(args)
    renderer = _make_renderer(args, filt)
    out = sys.stdout

    def emit(s: str) -> None:
        out.write(s + "\n")

    session = None
    if not args.no_cache:
        rec = record.status_of(dev.key)
        if rec:
            ui.info(f"Hay una grabación en background para este dispositivo ({rec.get('session')}); no duplico la caché.")
        else:
            session = cache.Session(dev, "logs", filt.describe())

    stream = LogcatStream(dev.serial, dev.key, buffers=args.buffer or [], tail=args.tail, clear=args.clear,
                          reconnect=(not args.no_reconnect) and bool(config.get("reconnect")),
                          status=lambda k, t: emit(renderer.banner(t, k)))
    def _pids_changed() -> None:
        if session:
            session.set_pids(filt.pid_names)
            session.set_threads(filt.tid_names)
            session.set_procs(filt.proc_names)

    watcher = None
    if filt.packages or filt.wants_threads or (renderer.show_thread and filt.explicit_pids) or renderer.show_proc:
        def _on_pid_event(text: str) -> None:
            emit(renderer.banner(text, "info"))
            _pids_changed()
        watcher = PidWatcher(lambda: stream.serial, filt, _on_pid_event, want_thread_names=renderer.show_thread,
                             want_proc_names=renderer.show_proc, on_refresh=_pids_changed)
        watcher.tick()      # resuelve PIDs/hilos antes de arrancar para no perder el buffer inicial
        watcher.start()

    import signal

    def _term(signum, frame):
        raise KeyboardInterrupt

    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, _term)
        except Exception:
            pass

    emit(renderer.banner(f"{dev.label()}  ·  {_filters_text(filt)}" + (f"  ·  caché → {session.log_path}" if session else "  ·  sin caché"), "ok"))
    code = 0
    try:
        for kind, payload in stream.stream():
            if kind == "line":
                if session:
                    session.write(payload)
                ll = parse(payload)
                if ll is None:
                    if not filt.packages and SEP_RE.match(payload):
                        emit(renderer.separator(payload))
                    continue
                ev = filt.observe(ll)
                if ev:
                    emit(renderer.banner(ev, "warn" if ("muri" in ev or "termin" in ev) else "ok"))
                    _pids_changed()
                if filt.matches(ll):
                    emit(renderer.plain(ll) if args.raw else renderer.line(ll))
            else:
                emit(renderer.banner(payload, {"lost": "err", "reconnected": "ok", "end": "warn"}.get(kind, "info")))
                if session:
                    session.note(payload)
    except KeyboardInterrupt:
        code = 130
    except BrokenPipeError:
        try:
            sys.stdout = open(os.devnull, "w")
        except Exception:
            pass
    finally:
        stream.close()
        if watcher:
            watcher.stop()
        if session:
            session.close()
            ui.errc.print(f"\n[dim]caché: {session.lines} líneas → {session.log_path}[/]")
    return code


# ---------------------------------------------------------------- cache

def _fmt_dt(dt: Optional[datetime]) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""


def cmd_cache_ls(args) -> int:
    cached = cache.list_cached()
    if not cached:
        ui.info(f"Aún no hay logs en caché ({config.LOGS_DIR}). Se crean al usar `droid logs` o `droid record`.")
        return 0
    if args.device:
        cdev = cache.find_device(args.device)
        if not cdev:
            raise ui.UserError(f"No hay caché para '{args.device}'.")
        t = Table(box=box.SIMPLE_HEAD, title=f"{theme.device_markup(cdev.key, escape(cdev.name))} · {cdev.info.get('serial', cdev.key)} · [dim]{cdev.dir}[/]", title_justify="left")
        for col in ("#", "Sesión", "Inicio", "Duración", "Líneas", "Tamaño", "Origen", "Filtros", "Estado"):
            t.add_column(col, justify="right" if col in ("#", "Líneas", "Tamaño") else "left")
        for i, s in enumerate(cdev.sessions, 1):
            f = s.meta.get("filters") or {}
            fbits = []
            if f.get("packages"):
                fbits.append(",".join(f["packages"]))
            if f.get("tags"):
                fbits.append("tag:" + ",".join(f["tags"]))
            if f.get("grep"):
                fbits.append("/" + f["grep"] + "/")
            if f.get("pids"):
                fbits.append("pid:" + ",".join(map(str, f["pids"])))
            if f.get("threads"):
                fbits.append("hilo:" + ",".join(f["threads"]))
            state = "[green]en curso[/]" if s.live else ("[yellow]sin cerrar[/]" if not s.ended else "")
            if s.meta.get("reconnects"):
                state += f" [dim]{s.meta['reconnects']} reconex.[/]"
            src = s.meta.get("source", "?")
            t.add_row(str(i), s.id, _fmt_dt(s.started), s.duration(), str(s.meta.get("lines", "?")),
                      theme.paint(ui.human_size(s.size), theme.size_color(s.size)),
                      theme.paint(src, theme.SOURCE.get(src, theme.MUTED)), escape(" ".join(fbits)), state)
        ui.console.print(t)
        ui.console.print(f"[dim]ver: droid cache show {args.device} -s <#>   ·   ruta: droid cache path {args.device}[/]")
        return 0
    t = Table(box=box.SIMPLE_HEAD, title=f"Caché de logs · {config.LOGS_DIR}", title_justify="left")
    for col in ("#", "Dispositivo", "Serial", "Sesiones", "Tamaño", "Última sesión", "Estado"):
        t.add_column(col, justify="right" if col in ("#", "Sesiones", "Tamaño") else "left")
    for i, c in enumerate(cached, 1):
        last = c.sessions[0] if c.sessions else None
        live = any(s.live for s in c.sessions)
        rec = record.status_of(c.key)
        state = "[green]grabando[/]" if rec else ("[green]en curso[/]" if live else "")
        t.add_row(str(i), theme.paint("●", theme.color_for(c.key)) + " " + theme.device_markup(c.key, escape(c.name)),
                  str(c.info.get("serial", c.key)), str(len(c.sessions)),
                  theme.paint(ui.human_size(c.total_size), theme.size_color(c.total_size)), _fmt_dt(last.started) if last else "", state)
    ui.console.print(t)
    ui.console.print("[dim]sesiones de un dispositivo: droid cache ls <#|modelo|alias>   ·   ver la última: droid cache show <dispositivo>[/]")
    return 0


def cmd_cache_show(args) -> int:
    _apply_color_flag(args.color)
    cdev = cache.find_device(args.device)
    if not cdev:
        raise ui.UserError("No hay caché para ese dispositivo (o no hay caché aún).")
    s = cache.find_session(cdev, args.session)
    if not s:
        raise ui.UserError(f"No encuentro la sesión '{args.session}' en {cdev.name}. Mira `droid cache ls {cdev.key}`.")
    filt = _make_filter(args, live=False)
    renderer = _make_renderer(args, filt)
    # semillas a partir de lo que vio la sesión (PIDs del paquete, nombres de hilo)
    seen = (s.meta.get("filters") or {}).get("pids_seen") or {}
    if filt.packages and seen:
        filt.set_pids({int(p): n for p, n in seen.items() if any(n == pk or n.startswith(pk + ":") for pk in filt.packages)})
    tseen = (s.meta.get("filters") or {}).get("threads_seen") or {}
    if tseen:
        filt.set_threads({int(t): n for t, n in tseen.items()})
    pseen = (s.meta.get("filters") or {}).get("procs_seen") or {}
    if pseen:
        filt.set_proc_names({int(p): n for p, n in pseen.items()})
    elif args.show_proc and seen:
        filt.set_proc_names({int(p): n for p, n in seen.items()})
    elif filt.thread_patterns:
        ui.warn("Esta sesión no guardó nombres de hilos (se guardan cuando `logs` usa --thread/--show-thread). Usa --tid.")
    ui.errc.print(f"[dim]{cdev.name} · sesión {s.id} · {_fmt_dt(s.started)} → {_fmt_dt(s.ended) or ('en curso' if s.live else '?')} · {s.log_path}[/]")

    def render(raw: str) -> Optional[str]:
        if raw.startswith("#droid "):
            return renderer.banner(raw[7:], "warn")
        ll = parse(raw)
        if ll is None:
            return renderer.separator(raw) if (SEP_RE.match(raw) and not filt.packages) else None
        filt.observe(ll)
        if filt.matches(ll):
            return renderer.plain(ll) if args.raw else renderer.line(ll)
        return None

    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    try:
        with open(s.log_path, "r", encoding="utf-8", errors="replace") as fh:
            if args.tail:
                buf = deque(maxlen=args.tail)
                for raw in fh:
                    r = render(raw.rstrip("\n"))
                    if r is not None:
                        buf.append(r)
                for r in buf:
                    print(r)
            else:
                for raw in fh:
                    r = render(raw.rstrip("\n"))
                    if r is not None:
                        print(r)
            if args.follow:
                import time
                ui.errc.print("[dim]siguiendo… (Ctrl+C para salir)[/]")
                while True:
                    line = fh.readline()
                    if line:
                        r = render(line.rstrip("\n"))
                        if r is not None:
                            print(r)
                    else:
                        time.sleep(0.4)
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        pass
    return 0


def cmd_cache_path(args) -> int:
    if args.device:
        cdev = cache.find_device(args.device)
        if not cdev:
            raise ui.UserError("No hay caché para ese dispositivo.")
        print(cdev.dir)
    else:
        print(config.LOGS_DIR)
    return 0


def cmd_cache_open(args) -> int:
    p = config.LOGS_DIR
    if args.device:
        cdev = cache.find_device(args.device)
        if cdev:
            p = cdev.dir
    os.system(f'open "{p}"')
    return 0


def cmd_cache_clean(args) -> int:
    cached = cache.list_cached()
    if args.device:
        cdev = cache.find_device(args.device)
        if not cdev:
            raise ui.UserError("No hay caché para ese dispositivo.")
        cached = [cdev]
    if not (args.older_than or args.keep is not None or args.all):
        raise ui.UserError("Indica qué borrar: --older-than 30d, --keep N (por dispositivo) o --all.")
    cutoff = None
    if args.older_than:
        try:
            cutoff = datetime.now() - cache.parse_age(args.older_than)
        except ValueError as e:
            raise ui.UserError(str(e))
    victims = []
    for c in cached:
        keep_n = args.keep if args.keep is not None else None
        kept = 0
        for s in c.sessions:      # ya vienen de más nueva a más vieja
            if s.live:
                continue
            if args.all:
                victims.append((c, s)); continue
            old = cutoff is not None and s.started is not None and s.started < cutoff
            over = keep_n is not None and kept >= keep_n
            if old or over:
                victims.append((c, s))
            else:
                kept += 1
    if not victims:
        ui.info("Nada que borrar.")
        return 0
    total = sum(s.size for _, s in victims)
    for c, s in victims:
        ui.console.print(f"  [red]✗[/] {c.name} · {s.id} · {ui.human_size(s.size)}")
    ui.console.print(f"{len(victims)} sesión(es), {ui.human_size(total)}")
    if args.dry_run:
        return 0
    if not args.yes and not ui.confirm("¿Borrar?"):
        ui.info("Cancelado.")
        return 0
    for _, s in victims:
        for p in (s.log_path, s.log_path.with_suffix(".json")):
            try:
                p.unlink()
            except OSError:
                pass
    ui.ok(f"Borradas {len(victims)} sesiones ({ui.human_size(total)}).")
    return 0


# ---------------------------------------------------------------- record

def cmd_record_start(args) -> int:
    devices = adbmod.list_devices()
    if args.all:
        targets = adbmod.dedupe([d for d in devices if d.online])
        if not targets:
            raise ui.UserError("No hay dispositivos online.")
    else:
        targets = [ui.select_device(devices, args.device, prompt="¿Qué dispositivo grabar en background?")]
    for dev in targets:
        try:
            info = record.start(dev, buffers=args.buffer or [])
            ui.ok(f"Grabando {dev.label()} en background (pid {info['pid']}) → {info.get('session') or 'iniciando…'}")
        except RuntimeError as e:
            ui.warn(str(e))
    ui.console.print("[dim]estado: droid record status · parar: droid record stop <dispositivo>|--all · ver: droid cache show <dispositivo> -f[/]")
    return 0


def _running_match(target: Optional[str], running: List[dict]) -> List[dict]:
    if not target:
        return running
    t = target.lower()
    if t.isdigit() and 1 <= int(t) <= len(running):
        return [running[int(t) - 1]]
    hits = [r for r in running if t in {str(r.get(k, "")).lower() for k in ("key", "serial", "alias")}
            or t in str(r.get("model", "")).lower() or str(r.get("key", "")).lower().startswith(t)]
    return hits


def cmd_record_stop(args) -> int:
    running = record.all_status()
    if not running:
        ui.info("No hay grabaciones activas.")
        return 0
    if args.all:
        targets = running
    else:
        targets = _running_match(args.device, running)
        if not args.device:
            if len(running) > 1:
                raise ui.UserError("Hay varias grabaciones; indica cuál o usa --all. Mira `droid record status`.")
        if not targets:
            raise ui.UserError(f"No hay grabación para '{args.device}'.")
    for r in targets:
        record.stop(r["key"])
        ui.ok(f"Detenida grabación de {r.get('alias') or r.get('model') or r['key']} → {r.get('session')}")
    return 0


def cmd_record_status(args) -> int:
    running = record.all_status()
    if not running:
        ui.info("No hay grabaciones en background. Inicia una con `droid record start \\[dispositivo|--all]`.")
        return 0
    t = Table(box=box.SIMPLE_HEAD, title="Grabaciones en background", title_justify="left")
    for col in ("#", "Dispositivo", "Serial", "PID", "Desde", "Líneas", "Tamaño", "Sesión"):
        t.add_column(col, justify="right" if col in ("#", "PID", "Líneas", "Tamaño") else "left")
    for i, r in enumerate(running, 1):
        lines, size = "?", 0
        sp = r.get("session")
        if sp:
            p = Path(sp)
            try:
                size = p.stat().st_size
                meta = json.loads(p.with_suffix(".json").read_text())
                lines = str(meta.get("lines", "?"))
                if meta.get("events"):
                    last = meta["events"][-1]["text"]
                    if "perdió" in last:
                        lines += " [red](dispositivo perdido)[/]"
            except Exception:
                pass
        t.add_row(str(i), theme.paint("●", theme.OK) + " " + theme.device_markup(r["key"], escape(r.get("alias") or r.get("model") or r["key"])), r.get("serial", ""), str(r["pid"]),
                  r.get("started", ""), lines, ui.human_size(size), sp or "")
    ui.console.print(t)
    return 0


def cmd_worker(args) -> int:
    dev = adbmod.Device.from_dict(json.loads(args.device_json))
    return record.worker_main(dev, args.buffer or [])


# ---------------------------------------------------------------- alias / apps / inspect / shell / config

def cmd_alias(args) -> int:
    devices = adbmod.list_devices()
    dev = None
    try:
        dev = ui.select_device(devices, args.device, require_online=False)
    except ui.UserError:
        hits = registry.find(args.device)
        if len(hits) != 1:
            raise
        key = hits[0][0]
    else:
        key = dev.key
        if dev.online:
            registry.upsert(dev)
    if args.remove:
        registry.set_alias(key, "")
        ui.ok(f"Alias eliminado de {key}")
    else:
        registry.set_alias(key, args.name)
        ui.ok(f"{key} → alias [bold]{args.name}[/]. Úsalo en cualquier comando: droid logs {args.name}")
    return 0


def _apps_table(serial: str, projects_dir: Path, show_all: bool) -> None:
    from . import apps as appsmod
    projects = appsmod.scan_projects(projects_dir)
    if not projects:
        raise ui.UserError(f"No encontré módulos Android con applicationId en {projects_dir}.")
    found = appsmod.find_project_apps(serial, projects)
    t = Table(box=box.SIMPLE_HEAD, title=f"Apps de tus proyectos instaladas ({projects_dir})", title_justify="left")
    for col in ("Proyecto", "Módulo", "Package", "Versión", "Debug", "Proceso", "Actualizada"):
        t.add_column(col, overflow="fold", no_wrap=(col in ("Package", "Debug")))
    for a in found:
        proc = ", ".join(theme.paint(f"pid {p}", theme.pid_color(p), bold=True) for p in a.pids) if a.pids else "[dim]parada[/]"
        dbg = theme.paint("✔ debug", theme.OK) if a.debuggable else theme.paint("✖ release", theme.ERR)
        ver = f"{a.version_name} [dim]({a.version_code})[/]" if a.version_name else ""
        proj = theme.paint(escape(a.project.project), theme.color_for(a.project.project), bold=True)
        if a.others:
            proj += " [dim](también en " + ", ".join(escape(o.project) for o in a.others) + ")[/]"
        t.add_row(proj, a.project.module, a.package, ver, dbg, proc, a.last_update)
    if found:
        ui.console.print(t)
    else:
        ui.info("Ninguna app de tus proyectos está instalada en este dispositivo.")
    matched_ids = {a.project.app_id for a in found}
    rest = [p for p in projects if p.app_id not in matched_ids]
    if show_all and rest:
        t2 = Table(box=box.SIMPLE_HEAD, title="Apps de proyectos NO instaladas en este dispositivo", title_justify="left")
        for col in ("Proyecto", "Módulo", "applicationId", "Sufijos"):
            t2.add_column(col, overflow="fold")
        for p in rest:
            style = "dim" if p.is_template else ""
            t2.add_row(f"[{style}]{p.project}[/{style}]" if style else p.project, p.module, p.app_id, ",".join(p.suffixes))
        ui.console.print(t2)
    elif rest:
        ui.console.print(f"[dim]{len(rest)} app(s) de proyectos no instaladas aquí → `droid apps --all` para verlas[/]")
    debuggable = [a for a in found if a.debuggable]
    if debuggable:
        ui.console.print(f"[dim]{len(debuggable)} app(s) debuggable(s): candidatas para `droid inspect` (ver docs/ROADMAP-inspector.md)[/]")


def cmd_apps(args) -> int:
    devices = adbmod.list_devices()
    dev = ui.select_device(devices, args.device, prompt="¿En qué dispositivo buscar tus apps?")
    _apps_table(dev.serial, Path(args.projects or config.get("projects_dir")).expanduser(), args.all)
    return 0


def cmd_ps(args) -> int:
    from .logs import list_processes, list_threads, thread_cpu, list_pids
    devices = adbmod.list_devices()
    dev = ui.select_device(devices, args.device, prompt="¿Procesos de qué dispositivo?")
    pkgs = args.package or []
    if args.threads:
        pids = set(args.pid or [])
        if pkgs:
            pids |= set(list_pids(dev.serial, pkgs))
        if not pids:
            raise ui.UserError("Indica el proceso: -p <paquete> o --pid <pid>." if not pkgs else f"{', '.join(pkgs)} no está corriendo.")
        names = list_threads(dev.serial, sorted(pids))
        cpu = thread_cpu(dev.serial, sorted(pids))
        procs = {p["pid"]: p for p in list_processes(dev.serial)}
        t = Table(box=box.SIMPLE_HEAD, title=f"Hilos · {dev.display} · " + ", ".join(f"{procs.get(p, {}).get('name', '?')} (pid {p})" for p in sorted(pids)), title_justify="left")
        for col in ("TID", "Hilo", "%CPU", "PID"):
            t.add_column(col, justify="right" if col in ("TID", "%CPU", "PID") else "left", overflow="fold")
        rows = []
        # tid -> pid: /proc/<pid>/task solo tiene sus hilos; re-listamos por pid para saber a cuál pertenece
        for p in sorted(pids):
            for tid, name in list_threads(dev.serial, [p]).items():
                rows.append((tid, name, cpu.get(tid, 0.0), p))
        if args.sort == "cpu":
            rows.sort(key=lambda r: (-r[2], r[0]))
        else:
            rows.sort(key=lambda r: (r[1].lower(), r[0]))
        for tid, name, c, p in rows:
            shown = "main" if tid == p else name
            label = theme.paint(escape(shown), theme.thread_color(shown), bold=(tid == p)) + (f" [dim]{escape(name)}[/]" if tid == p else "")
            t.add_row(str(tid), label, theme.cpu_markup(c), theme.paint(str(p), theme.pid_color(p), bold=True))
        ui.console.print(t)
        ui.console.print(f"[dim]{len(rows)} hilos · filtrar logs: droid logs {args.device or ''} -p <pkg> --thread '<nombre>'  ·  o --tid <TID>[/]")
        return 0
    procs = list_processes(dev.serial)
    if pkgs:
        procs = [p for p in procs if any(p["name"] == k or p["name"].startswith(k + ":") or k.lower() in p["name"].lower() for k in pkgs)]
    elif not args.all:
        procs = [p for p in procs if p["user"].startswith("u0_a") or p["user"].startswith("u10_a")]
    if args.sort == "cpu":
        procs.sort(key=lambda p: (-p["cpu"], -p["rss"]))
    elif args.sort == "mem":
        procs.sort(key=lambda p: -p["rss"])
    else:
        procs.sort(key=lambda p: p["name"].lower())
    t = Table(box=box.SIMPLE_HEAD, title=f"Procesos · {dev.display}" + (" (todos)" if args.all else " (apps de usuario; --all para todos)"), title_justify="left")
    for col in ("PID", "Usuario", "%CPU", "RSS", "Proceso"):
        t.add_column(col, justify="right" if col in ("PID", "%CPU", "RSS") else "left", overflow="fold")
    for p in procs[: args.limit] if args.limit else procs:
        rss = p["rss"] * 1024
        t.add_row(theme.paint(str(p["pid"]), theme.pid_color(p["pid"]), bold=True), p["user"], theme.cpu_markup(p["cpu"]),
                  theme.paint(ui.human_size(rss), theme.size_color(rss)), theme.paint(escape(p["name"]), theme.pid_color(p["pid"])))
    ui.console.print(t)
    ui.console.print(f"[dim]{len(procs)} procesos · hilos de uno: droid ps {args.device or ''} --threads -p <pkg>  ·  filtrar logs: droid logs -p <pkg> / --pid <PID>[/]")
    return 0


def _parse_duration(spec: Optional[str]) -> Optional[float]:
    if not spec:
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([smh]?)", spec.strip().lower())
    if not m:
        raise ui.UserError("Duración no válida; usa 30s, 5m, 1h")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


_TIMELINE_LEGEND = "× crash  ! anr  † murió  ▽ poca memoria"


def _exit_kind(reason: int) -> str:
    if reason in (4, 5):
        return "crash"
    if reason == 6:
        return "anr"
    if reason == 3:
        return "lowmem"
    return "death"


def _device_tzinfo(device_tz_offset: Optional[str]):
    if not device_tz_offset:
        return None
    m = re.fullmatch(r"([+-])(\d{2})(\d{2})", device_tz_offset)
    if not m:
        return None
    sign = 1 if m.group(1) == "+" else -1
    minutes = sign * (int(m.group(2)) * 60 + int(m.group(3)))
    return timezone(timedelta(minutes=minutes))


def _exit_events(exits, device_tz_offset: Optional[str] = None) -> list:
    tzinfo = _device_tzinfo(device_tz_offset)
    events = []
    for e in exits:
        ts_raw = e.timestamp if hasattr(e, "timestamp") else e.get("timestamp")
        reason = e.reason if hasattr(e, "reason") else e.get("reason")
        try:
            dt = datetime.strptime(ts_raw, "%Y-%m-%d %H:%M:%S.%f")
        except (ValueError, TypeError):
            continue
        if tzinfo is not None:
            dt = dt.replace(tzinfo=tzinfo)
        events.append((dt.timestamp(), _exit_kind(reason)))
    return events


def _heap_buckets(m) -> list:
    get = (lambda k: getattr(m, k)) if hasattr(m, "java_heap_kb") else (lambda k: m.get(k, 0))
    return [
        ("Java heap", get("java_heap_kb")),
        ("Native heap", get("native_heap_kb")),
        ("Code", get("code_kb")),
        ("Stack", get("stack_kb")),
        ("Graphics", get("graphics_kb")),
        ("Private other", get("private_other_kb")),
        ("System", get("system_kb")),
    ]


def _pctl_buckets(sample) -> list:
    get = (lambda k: getattr(sample, k)) if hasattr(sample, "p50") else (lambda k: sample.get(k))
    return [("p50", get("p50") or 0), ("p90", get("p90") or 0), ("p99", get("p99") or 0)]


def _replay_chart(data):
    from rich.console import Group
    from rich.panel import Panel
    meta = data.get("meta") or {}
    samples = [x for x in data.get("samples", []) if x.get("alive")]
    width = max(10, min(40, ui.console.width - 30))
    if not samples:
        return Group(Text("sin muestras", style=theme.hx(theme.MUTED)))
    cpu = [x["cpu"] for x in samples]
    rss_mb = [x["rss_kb"] / 1024.0 for x in samples]
    fps = [x["fps"] for x in samples]
    frames = sum(x["frames"] for x in samples)
    janky = sum(x["janky"] for x in samples)
    jank_pct = (janky / frames * 100.0) if frames else 0.0
    t_start = samples[0]["t"]
    t_end = samples[-1]["t"]
    exits = data.get("exits", [])
    summary = (
        f"{len(samples)} muestras · {t_end - t_start:.0f}s\n"
        f"CPU media {sum(cpu) / len(cpu):.1f}% · máx {max(cpu):.1f}%\n"
        f"RSS media {theme.kb(sum(rss_mb) / len(rss_mb) * 1024)} · máx {theme.kb(max(rss_mb) * 1024)}\n"
        f"Frames {frames} · jank {jank_pct:.1f}%\n"
        f"Salidas {len(exits)}"
    )
    dev_name = (meta.get("device") or {}).get("model", "?")
    panel = Panel(summary, title=f"{meta.get('package', '?')} · {dev_name}", border_style=theme.hx(theme.INFO))
    lines = [panel]
    t = Text("CPU "); t.append_text(theme.spark_labeled(cpu, width, lo=0, unit="%")); lines.append(t)
    t = Text("RSS "); t.append_text(theme.spark_labeled(rss_mb, width, fmt=lambda v: f"{v:.0f}", unit=" MB")); lines.append(t)
    t = Text("FPS "); t.append_text(theme.spark_labeled(fps, width, lo=0, unit=" fps")); lines.append(t)
    events = _exit_events(exits, meta.get("device_tz_offset"))
    gc_list = data.get("gc", [])
    events_all = events + [(g["t"], "gc") for g in gc_list]
    lines.append(theme.timeline_strip(events_all, t_start, t_end, width))
    if events_all:
        lines.append(Text(_TIMELINE_LEGEND, style=theme.hx(theme.MUTED)))
    if gc_list:
        total_pause = sum(g.get("pause_ms", 0.0) for g in gc_list)
        lines.append(Text(f"GC: {len(gc_list)} colecciones · pausa total {total_pause:.0f} ms", style=theme.hx(theme.MUTED)))
    leak_list = data.get("leak", [])
    if leak_list:
        parts = []
        for f in leak_list:
            growth_mb = (f.get("last_kb", 0) - f.get("first_kb", 0)) / 1024.0
            span_s = max(0.0, f.get("until_t", 0) - f.get("since_t", 0))
            parts.append(f"{f.get('metric')} +{growth_mb:.1f} MB en {span_s:.0f} s")
        lines.append(Text("Posible fuga: " + " · ".join(parts) + " (heurística)", style=theme.hx(theme.WARN) + " bold"))
    meminfo_list = data.get("meminfo", [])
    if meminfo_list:
        lines.append(Text("Memoria por heap", style="bold"))
        lines.append(theme.histogram(_heap_buckets(meminfo_list[-1]), width=width, fmt=theme.kb))
    frame_all = [ms for x in samples for ms in x.get("frame_ms", [])]
    lines.append(Text("Frames (histograma)", style="bold"))
    if frame_all:
        lines.append(theme.histogram(theme.frame_buckets(frame_all), width=width))
    else:
        lines.append(theme.histogram(_pctl_buckets(samples[-1]), width=width, fmt=lambda v: f"{v:.0f} ms"))
    top: dict = {}
    counts: dict = {}
    for x in samples:
        for th in x.get("threads", []):
            top[th["name"]] = top.get(th["name"], 0.0) + th["cpu"]
            counts[th["name"]] = counts.get(th["name"], 0) + 1
    top_avg = sorted(((n, v / counts[n]) for n, v in top.items()), key=lambda kv: -kv[1])[:8]
    if top_avg:
        tt = Table(box=box.SIMPLE_HEAD, title="Hilos con más CPU (media)", title_justify="left")
        tt.add_column("Hilo"); tt.add_column(""); tt.add_column("%CPU", justify="right")
        for name, avg in top_avg:
            tt.add_row(theme.paint(escape(name), theme.thread_color(name)), theme.hbar(avg, 100, 24), f"{avg:.1f}")
        lines.append(tt)
    return Group(*lines)


def _inspect_view(ses, top: int, thread_pat: Optional[str]):
    from rich.console import Group
    from rich.panel import Panel
    from . import inspector as insp
    samples = [x for x in ses.samples if x.alive]
    last = ses.samples[-1] if ses.samples else None
    head = Text()
    dev = theme.color_for(ses.dev.key)
    head.append(ses.dev.display, style=f"bold {theme.hx(dev)}")
    head.append(f" · {ses.package} · ", style="dim")
    if last and last.alive:
        head.append(f"pid {last.pid}", style=f"bold {theme.hx(theme.pid_color(last.pid))}")
        head.append(f" · {insp.oom_label(last.oom_adj)} · {last.nthreads} hilos · {'debuggable' if ses.debuggable else 'release'}", style="dim")
    else:
        head.append("proceso no corriendo", style=f"bold {theme.hx(theme.ERR)}")
    lines = [head]
    width = max(10, min(40, ui.console.width - 30))
    if last and last.alive:
        cpu_hist = [x.cpu for x in samples]
        rss_hist_mb = [x.rss_kb / 1024.0 for x in samples]
        fps_hist = [x.fps for x in samples]
        t = Text()
        t.append("CPU ", style="bold"); t.append(f"{last.cpu:5.1f}%", style=theme.hx(theme.cpu_color(last.cpu)))
        t.append(f" de 1 core ({last.cpu_total_pct:.1f}% de {last.ncpu}) ", style="dim")
        t.append_text(theme.spark_labeled(cpu_hist, width, lo=0, unit="%"))
        lines.append(t)
        if ses.samples:
            t_start = ses.samples[0].t
            t_end = ses.samples[-1].t
            events = _exit_events(ses.exits, getattr(ses, "device_tz_offset", None))
            lines.append(theme.timeline_strip(events, t_start, t_end, width))
            if events:
                lines.append(Text(_TIMELINE_LEGEND, style=theme.hx(theme.MUTED)))
        t = Text()
        t.append("RSS ", style="bold"); t.append(theme.kb(last.rss_kb), style=theme.hx(theme.WARN))
        t.append(f" (anon {theme.kb(last.rss_anon_kb)} · file {theme.kb(last.rss_file_kb)} · swap {theme.kb(last.swap_kb)}) ", style="dim")
        if last.pss_kb is not None:
            t.append("PSS ", style="bold"); t.append(theme.kb(last.pss_kb) + " ", style=theme.hx(theme.WARN))
        t.append_text(theme.spark_labeled(rss_hist_mb, width, fmt=lambda v: f"{v:.0f}", unit=" MB"))
        lines.append(t)
        if ses.meminfos:
            m = ses.meminfos[-1]
            t = Text()
            t.append("meminfo ", style="bold"); t.append(f"PSS {theme.kb(m.pss_total_kb)} · Java heap {theme.kb(m.java_heap_kb)}"
                     + (f" ({theme.kb(m.dalvik_heap_alloc_kb)}/{theme.kb(m.dalvik_heap_size_kb)})" if m.dalvik_heap_size_kb else "")
                     + f" · Native {theme.kb(m.native_heap_kb)} · Graphics {theme.kb(m.graphics_kb)} · Code {theme.kb(m.code_kb)}"
                     + f" · Views {m.views} · Activities {m.activities}", style="dim")
            lines.append(t)
            lines.append(Text("Memoria por heap", style="bold"))
            lines.append(theme.histogram(_heap_buckets(m), width=width, fmt=theme.kb))
        t = Text()
        t.append("Frames ", style="bold")
        t.append(f"{last.fps:4.1f} fps ", style=theme.hx(theme.OK if last.fps > 0 else theme.MUTED))
        t.append(f"jank {last.jank_pct:.0f}% ", style=theme.hx(theme.ERR if last.jank_pct > 10 else theme.OK))
        t.append(f"p50 {last.p50}ms p90 {last.p90}ms p99 {last.p99}ms · vsync perdidos {last.missed_vsync} · UI lenta {last.slow_ui} ", style="dim")
        t.append_text(theme.spark_labeled(fps_hist, width, lo=0, unit=" fps"))
        lines.append(t)
        frame_all = [ms for x in samples for ms in x.frame_ms]
        lines.append(Text("Frames (histograma)", style="bold"))
        if frame_all:
            lines.append(theme.histogram(theme.frame_buckets(frame_all), width=width))
        else:
            lines.append(theme.histogram(_pctl_buckets(last), width=width, fmt=lambda v: f"{v:.0f} ms"))
    tbl = Table(box=box.SIMPLE_HEAD, title="Hilos por CPU" + (f" (filtro: {thread_pat})" if thread_pat else ""), title_justify="left", expand=False)
    for col in ("TID", "Hilo", "%CPU", "", "Estado"):
        tbl.add_column(col, justify="right" if col in ("TID", "%CPU") else "left")
    if last and last.alive:
        ths = last.threads
        if thread_pat:
            import fnmatch
            pat = thread_pat.lower()
            ths = [th for th in ths if fnmatch.fnmatchcase(th.name.lower(), pat) or pat in th.name.lower()]
        for th in ths[:top]:
            bar = theme.hbar(th.cpu, 100, 24)
            tbl.add_row(str(th.tid), theme.paint(escape(th.name), theme.thread_color(th.name), bold=(th.name == "main")),
                        theme.paint(f"{th.cpu:.1f}", theme.cpu_color(th.cpu)), theme.paint(bar, theme.cpu_color(th.cpu)),
                        {"R": "[bold]R corriendo[/]", "S": "[dim]S[/]", "D": "[yellow]D io[/]", "Z": "[red]Z[/]", "T": "[red]T parado[/]"}.get(th.state, th.state))
    ex = Table(box=box.SIMPLE_HEAD, title=f"Salidas de la app ({len(ses.exits)})", title_justify="left")
    for col in ("Fecha", "Razón", "PID", "Detalle"):
        ex.add_column(col)
    for e in ses.exits[:5]:
        color = theme.ERR if e.reason in (4, 5, 6) else (theme.WARN if e.reason in (3, 2) else theme.MUTED)
        ex.add_row(e.timestamp, theme.paint(e.reason_name, color, bold=True), str(e.pid), escape((e.anr or e.description or "")[:80]))
    n = len(ses.samples)
    foot = Text(f"{n} muestras · cada {ses.interval:.1f}s" + (f" · guardando en {ses.path}" if ses.path else "") + " · Ctrl+C para terminar", style="dim")
    return Group(Panel(Group(*lines), border_style=theme.hx(dev)), tbl, ex, foot)


def cmd_inspect(args) -> int:
    from . import inspector as insp
    if args.list:
        sessions = insp.list_sessions()
        if not sessions:
            ui.info(f"No hay sesiones de inspección en {insp.INSPECT_DIR}")
            return 0
        t = Table(box=box.SIMPLE_HEAD, title=f"Sesiones de inspección · {insp.INSPECT_DIR}", title_justify="left")
        for col in ("#", "Dispositivo", "Archivo", "Tamaño"):
            t.add_column(col, justify="right" if col in ("#", "Tamaño") else "left")
        for i, p in enumerate(sessions, 1):
            t.add_row(str(i), p.parent.name, p.name, ui.human_size(p.stat().st_size))
        ui.console.print(t)
        ui.console.print("[dim]resumen: droid inspect --replay <archivo|#>[/]")
        return 0
    if args.replay:
        sessions = insp.list_sessions()
        path = Path(args.replay)
        if args.replay.isdigit() and 1 <= int(args.replay) <= len(sessions):
            path = sessions[int(args.replay) - 1]
        if not path.exists():
            raise ui.UserError(f"No existe {path}")
        data = insp.load_session(path)
        if getattr(args, "chart", False):
            ui.console.print(_replay_chart(data))
            return 0
        samples = [x for x in data["samples"] if x.get("alive")]
        meta = data["meta"] or {}
        ui.console.print(f"[bold]{meta.get('package')}[/] · {meta.get('device', {}).get('model')} · {meta.get('started')} → {(data['end'] or {}).get('ended', '?')} · {len(samples)} muestras")
        if not samples:
            return 0
        cpu = [x["cpu"] for x in samples]; rss = [x["rss_kb"] for x in samples]; fps = [x["fps"] for x in samples]
        frames = sum(x["frames"] for x in samples); janky = sum(x["janky"] for x in samples)
        ui.console.print(f"CPU media {sum(cpu)/len(cpu):.1f}% máx {max(cpu):.1f}%  {theme.paint(theme.spark(cpu, 60, lo=0), theme.ERR)}")
        ui.console.print(f"RSS media {theme.kb(sum(rss)/len(rss))} máx {theme.kb(max(rss))}  {theme.paint(theme.spark(rss, 60), theme.WARN)}")
        ui.console.print(f"Frames {frames} · jank {janky} ({(janky/frames*100 if frames else 0):.1f}%) · fps medio {sum(fps)/len(fps):.1f}  {theme.paint(theme.spark(fps, 60, lo=0), theme.OK)}")
        top = {}
        for x in samples:
            for th in x.get("threads", []):
                top[th["name"]] = top.get(th["name"], 0.0) + th["cpu"]
        t = Table(box=box.SIMPLE_HEAD, title="Hilos con más CPU (media)", title_justify="left")
        t.add_column("Hilo"); t.add_column("%CPU medio", justify="right")
        for name, v in sorted(top.items(), key=lambda kv: -kv[1])[:10]:
            t.add_row(theme.paint(escape(name), theme.thread_color(name)), theme.cpu_markup(v / len(samples)))
        ui.console.print(t)
        if data["meminfo"]:
            m = data["meminfo"][-1]
            ui.console.print(f"[dim]último meminfo: PSS {theme.kb(m.get('pss_total_kb'))} · Java {theme.kb(m.get('java_heap_kb'))} · Native {theme.kb(m.get('native_heap_kb'))} · Views {m.get('views')} · Activities {m.get('activities')}[/]")
        if data.get("gc") or data.get("leak"):
            gc_n = len(data.get("gc", []))
            leak_n = len(data.get("leak", []))
            pause_total = sum(g.get("pause_ms", 0.0) for g in data.get("gc", []))
            ui.console.print(f"[dim]GC: {gc_n} colecciones · pausa total {pause_total:.0f} ms · fugas posibles: {leak_n}[/]")
        return 0
    devices = adbmod.list_devices()
    dev = ui.select_device(devices, args.device, prompt="¿Qué dispositivo inspeccionar?")
    if not args.package:
        ui.console.print("[bold]droid inspect[/] — elige una app con [bold]-p <paquete>[/]. Apps de tus proyectos en este dispositivo:")
        _apps_table(dev.serial, Path(config.get("projects_dir")).expanduser(), show_all=False)
        ui.console.print("[dim]ej.: droid inspect " + (dev.alias or dev.serial) + " -p com.mi.app --interval 1 --duration 60s[/]")
        return 0
    duration = _parse_duration(args.duration)
    if args.json:
        ses = insp.InspectSession(dev, args.package, interval=args.interval, meminfo_every=args.meminfo_every, record=not args.no_record,
                                  on_sample=lambda smp: print(json.dumps({"type": "sample", **smp.to_dict()}), flush=True),
                                  on_meminfo=lambda m: print(json.dumps({"type": "meminfo", **m.to_dict()}), flush=True),
                                  on_status=lambda k, t: print(json.dumps({"type": "status", "kind": k, "text": t}), flush=True))
    else:
        ses = insp.InspectSession(dev, args.package, interval=args.interval, meminfo_every=args.meminfo_every, record=not args.no_record,
                                  on_status=lambda k, t: ui.errc.print(f"[dim]{t}[/]"))
    ses.start()
    import time as _time
    end = _time.time() + duration if duration else None
    try:
        if args.json:
            while ses.running and (end is None or _time.time() < end):
                _time.sleep(0.2)
        else:
            from rich.live import Live
            with Live(_inspect_view(ses, args.top, args.thread), console=ui.console, refresh_per_second=4, screen=False) as live:
                while ses.running and (end is None or _time.time() < end):
                    _time.sleep(0.25)
                    live.update(_inspect_view(ses, args.top, args.thread))
    except KeyboardInterrupt:
        pass
    finally:
        ses.stop()
    summ = ses.summary()
    if args.json:
        print(json.dumps({"type": "summary", **summ}, ensure_ascii=False))
    else:
        ui.console.print(f"[bold]Resumen[/] · {summ.get('alive', 0)} muestras útiles en {summ.get('duration_s', 0)}s · CPU media {summ.get('cpu_avg')}% máx {summ.get('cpu_max')}% · RSS media {theme.kb(summ.get('rss_kb_avg'))} · frames {summ.get('frames')} jank {summ.get('jank_pct')}%")
        if summ.get("top_threads"):
            ui.console.print("[dim]hilos: " + ", ".join(f"{n} {v}%" for n, v in summ["top_threads"][:5]) + "[/]")
        if ses.path:
            ui.console.print(f"[dim]sesión guardada: {ses.path} · resumen: droid inspect --replay 1[/]")
    return 0


def cmd_db(args) -> int:
    from . import db as dbmod
    devices = adbmod.list_devices()
    dev = ui.select_device(devices, args.device, prompt="¿Qué dispositivo?")
    if not args.package:
        ui.console.print("[bold]droid db[/] — elige una app debuggable con [bold]-p <paquete>[/]:")
        _apps_table(dev.serial, Path(config.get("projects_dir")).expanduser(), show_all=False)
        return 0
    ok, msg = dbmod.check_run_as(dev.serial, args.package)
    if not ok:
        raise ui.UserError(f"run-as falla para {args.package}: {msg}. Solo funciona con builds debuggables instaladas.")
    if args.prefs:
        for name, content in dbmod.shared_prefs(dev.serial, args.package).items():
            ui.console.rule(name)
            ui.console.print(escape(content.strip()))
        return 0
    dbs = dbmod.list_databases(dev.serial, args.package)
    if not dbs:
        ui.info(f"{args.package} no tiene bases de datos en {', '.join(dbmod.SEARCH_DIRS)}.")
        return 0
    if args.list or not (args.db or len(dbs) == 1):
        t = Table(box=box.SIMPLE_HEAD, title=f"Bases de datos de {args.package} · {theme.device_markup(dev.key, escape(dev.display))}", title_justify="left")
        for col in ("#", "Archivo", "Tamaño", "Modificado", "WAL"):
            t.add_column(col, justify="right" if col in ("#", "Tamaño") else "left")
        for i, d in enumerate(dbs, 1):
            t.add_row(str(i), d.path, theme.paint(ui.human_size(d.size), theme.size_color(d.size)), d.mtime, theme.paint("✔", theme.OK) if d.wal else "")
        ui.console.print(t)
        if not args.db and len(dbs) > 1:
            ui.console.print("[dim]elige una con --db <#|nombre> y añade --tables, --table T, --query SQL[/]")
            return 0
    chosen = dbs[0]
    if args.db:
        if args.db.isdigit() and 1 <= int(args.db) <= len(dbs):
            chosen = dbs[int(args.db) - 1]
        else:
            hits = [d for d in dbs if d.name == args.db or d.path == args.db or args.db.lower() in d.name.lower()]
            if len(hits) != 1:
                raise ui.UserError(f"No identifico la DB '{args.db}'. Opciones: " + ", ".join(d.path for d in dbs))
            chosen = hits[0]
    dest = Path(args.export).expanduser() if args.export else dbmod.snapshot_dir(dev, args.package)
    dest.mkdir(parents=True, exist_ok=True)
    local = dbmod.pull_database(dev.serial, args.package, chosen.path, dest)
    ui.errc.print(f"[dim]snapshot: {local}[/]")
    conn = dbmod.open_db(local)
    if args.query:
        cols, rws, trunc = dbmod.query(conn, args.query, args.limit)
        _print_rows(cols, rws, trunc, args.csv, title=args.query)
        return 0
    if args.table:
        if args.schema:
            ui.console.print(escape(dbmod.create_sql(conn, args.table)))
        cols, rws, _ = dbmod.rows(conn, args.table, limit=args.limit, offset=args.offset)
        _print_rows(cols, rws, False, args.csv, title=f"{args.table} (LIMIT {args.limit} OFFSET {args.offset})")
        return 0
    t = Table(box=box.SIMPLE_HEAD, title=f"{chosen.path} · tablas", title_justify="left")
    for col in ("Tabla", "Filas", "Tipo", "Columnas"):
        t.add_column(col, justify="right" if col == "Filas" else "left", overflow="fold")
    for name, n, typ in dbmod.tables(conn):
        cols = dbmod.schema(conn, name)
        t.add_row(theme.paint(escape(name), theme.color_for(name), bold=True), str(n), typ,
                  ", ".join((("[bold]" + c + "[/]") if pk else c) + f"[dim]:{ty.lower()}[/]" for c, ty, _, pk in cols[:12]) + (" …" if len(cols) > 12 else ""))
    ui.console.print(t)
    ui.console.print(f"[dim]filas: droid db {dev.alias or ''} -p {args.package} --table <tabla> · consulta: --query \"SELECT …\" · CSV: --csv · prefs: --prefs[/]")
    return 0


def _files_entry_kind(filesmod, serial, root, path, package, pid):
    if "/" in path:
        parent, name = path.rsplit("/", 1)
    else:
        parent, name = "", path
    if not name:
        return None
    entries = filesmod.list_dir(serial, root, parent, package, pid)
    for e in entries:
        if e.name == name:
            return e.kind
    return None


def cmd_files(args) -> int:
    from . import files as filesmod
    devices = adbmod.list_devices()
    dev = ui.select_device(devices, args.device, prompt="¿Qué dispositivo?")
    root = args.root
    package = args.package
    pid = args.pid
    if root == "sandbox" and not package:
        raise ui.UserError("Indica el paquete con -p PKG (root=sandbox)")
    if root == "proc" and not pid:
        raise ui.UserError("Indica --pid PID (root=proc)")
    ok, msg = filesmod.check_access(dev.serial, root, package, pid)
    if not ok:
        raise ui.UserError(f"Sin acceso a {root}: {msg}")
    path = (args.path or "").strip("/")

    if args.cat:
        text, truncated = filesmod.preview(dev.serial, root, path, package)
        sys.stdout.write(text)
        if truncated:
            ui.errc.print("[dim](truncado)[/]")
        return 0

    if args.pull:
        dest = Path(args.pull).expanduser()
        pulled = filesmod.pull_path(dev.serial, root, path, package, dest)
        for p in pulled:
            ui.errc.print(f"[dim]{p}[/]")
        return 0

    if args.rm:
        kind = _files_entry_kind(filesmod, dev.serial, root, path, package, pid)
        is_dir = kind == "dir"
        if not args.yes:
            label = "directorio (recursivo)" if is_dir else "archivo"
            if not ui.confirm(f"¿Borrar el {label} {path or root}?"):
                ui.info("Cancelado.")
                return 0
        ok2, out = filesmod.delete_path(dev.serial, root, path, package, is_dir, confirmed=True)
        if not ok2:
            raise ui.UserError(f"No se pudo borrar: {out}")
        ui.info(f"Borrado {path or root}")
        return 0

    entries = filesmod.list_dir(dev.serial, root, path, package, pid)
    entries = [e for e in entries if e.kind != "error"]
    entries.sort(key=lambda e: (e.kind != "dir", e.name.lower()))

    if args.json:
        print(json.dumps([e.to_dict() for e in entries], ensure_ascii=False, indent=2))
        return 0

    t = Table(box=box.SIMPLE_HEAD, title=f"{root}:/{path} · {theme.device_markup(dev.key, escape(dev.display))}", title_justify="left")
    for col in ("", "Nombre", "Tamaño", "Modificado", "Permisos"):
        t.add_column(col)
    glyphs = {"dir": "📁", "link": "🔗", "file": ""}
    for e in entries:
        t.add_row(glyphs.get(e.kind, "⛔"), e.name, ui.human_size(e.size) if e.kind == "file" else "", e.mtime, e.perm)
    ui.console.print(t)
    return 0


def _print_rows(cols, rws, truncated: bool, as_csv: bool, title: str = "") -> None:
    from . import db as dbmod
    if as_csv:
        sys.stdout.write(dbmod.to_csv(cols, rws))
        return
    t = Table(box=box.SIMPLE_HEAD, title=escape(title), title_justify="left")
    for c in cols:
        t.add_column(escape(str(c)), overflow="fold")
    for r in rws:
        t.add_row(*[escape(str(v)[:200]) if v is not None else "[dim]NULL[/]" for v in r])
    ui.console.print(t)
    ui.console.print(f"[dim]{len(rws)} filas" + (" (truncado, sube --limit)" if truncated else "") + "[/]")


def cmd_net(args) -> int:
    from . import net as netmod
    devices = adbmod.list_devices()
    dev = ui.select_device(devices, args.device, prompt="¿Qué dispositivo?")
    if args.sockets:
        uid = netmod.uid_of(dev.serial, args.package) if args.package else None
        socks = netmod.sockets_for_uid(dev.serial, uid)
        t = Table(box=box.SIMPLE_HEAD, title=f"Sockets · {dev.display}" + (f" · {args.package} (uid {uid})" if args.package else " · todos"), title_justify="left")
        for col in ("Proto", "Estado", "Local", "Remoto", "UID"):
            t.add_column(col)
        for s_ in socks[:200]:
            t.add_row(s_.proto, theme.paint(s_.state, theme.OK if s_.state == "ESTAB" else theme.MUTED), s_.local, s_.remote, str(s_.uid))
        ui.console.print(t)
        return 0
    duration = _parse_duration(args.duration)
    mon = netmod.NetMonitor(dev, args.package, interval=args.interval,
                            on_status=lambda k, t: ui.errc.print(f"[dim]{t}[/]"))
    mon.start()
    import time as _time
    end = _time.time() + duration if duration else None
    try:
        if args.json:
            while mon.running and (end is None or _time.time() < end):
                _time.sleep(args.interval)
                if mon.samples:
                    smp = mon.samples[-1]
                    print(json.dumps({"t": smp.t, "rates": smp.rates, "totals": mon.totals,
                                       "hosts": mon.hosts, "events": list(mon.events)}), flush=True)
        else:
            from rich.live import Live
            with Live(_net_view(mon), console=ui.console, refresh_per_second=4) as live:
                while mon.running and (end is None or _time.time() < end):
                    _time.sleep(0.25)
                    live.update(_net_view(mon))
    except KeyboardInterrupt:
        pass
    finally:
        mon.stop()
    return 0


def _net_view(mon):
    from rich.console import Group
    from rich.panel import Panel
    lines = []
    head = Text()
    head.append(mon.dev.display, style=f"bold {theme.hx(theme.color_for(mon.dev.key))}")
    head.append(f" · red" + (f" · {mon.package} (uid {mon.uid})" if mon.package else " · todo el dispositivo"), style="dim")
    cadence = getattr(mon, "cadence_label", None)
    if cadence:
        head.append(f" · {cadence}", style="dim")
    lines.append(head)
    samples = list(mon.samples)
    last = samples[-1] if samples else None
    if last:
        for iface, (rx, tx) in sorted(last.rates.items()):
            if rx == 0 and tx == 0 and not any(s.rates.get(iface, (0, 0)) != (0, 0) for s in samples[-30:]):
                continue
            rx_hist = [s.rates.get(iface, (0, 0))[0] for s in samples]
            tx_hist = [s.rates.get(iface, (0, 0))[1] for s in samples]
            rx_spark, tx_spark = theme.dual_spark(rx_hist, tx_hist, 30)
            t = Text()
            t.append(f"{iface:<10}", style="bold")
            t.append(f"↓ {theme.rate(rx):>10} ", style=theme.hx(theme.OK)); t.append(rx_spark, style=theme.hx(theme.OK))
            t.append(f"  ↑ {theme.rate(tx):>10} ", style=theme.hx(theme.INFO)); t.append(tx_spark, style=theme.hx(theme.INFO))
            lines.append(t)
    if mon.totals:
        t = Text()
        t.append("Totales de la app ", style="bold")
        t.append(f"↓ {ui.human_size(mon.totals['rx'])} ↑ {ui.human_size(mon.totals['tx'])}", style=theme.hx(theme.WARN))
        if mon.totals_start:
            t.append(f" · en esta sesión ↓ {ui.human_size(mon.totals['rx'] - mon.totals_start['rx'])} ↑ {ui.human_size(mon.totals['tx'] - mon.totals_start['tx'])}", style="dim")
        t.append(" · " + " ".join(f"{k}: ↓{ui.human_size(v[0])} ↑{ui.human_size(v[1])}" for k, v in mon.totals["by_type"].items()), style="dim")
        lines.append(t)
        lines.append(Text("(los totales por app los consolida Android periódicamente; la tasa por segundo es por interfaz)", style="dim"))
        by_type = mon.totals.get("by_type") or {}
        if by_type:
            max_total = max((v[0] + v[1] for v in by_type.values()), default=0)
            tipo = Text("Por tipo ", style="bold")
            for k, v in by_type.items():
                tipo.append(f"{k} ", style="dim")
                tipo.append(theme.hbar(v[0] + v[1], max_total, 16), style=theme.hx(theme.WARN))
                tipo.append("  ")
            lines.append(tipo)
    extras = []
    hosts = getattr(mon, "hosts", None) or []
    if hosts:
        htbl = Table(box=box.SIMPLE_HEAD, title="Hosts", title_justify="left")
        for col in ("Host", "IP", "Conexiones", "Estados", "Primera", "Última"):
            htbl.add_column(col)
        for h_ in hosts[:10]:
            host = h_.get("host") or ""
            states = " ".join(f"{k}:{v}" for k, v in sorted((h_.get("states") or {}).items()))
            htbl.add_row(theme.paint(host, theme.color_for(host)) if host else Text("(sin PTR)", style="dim"),
                        h_.get("ip", ""), str(h_.get("connections", 0)), states,
                        _fmt_net_t(h_.get("first_seen")), _fmt_net_t(h_.get("last_seen")))
        extras.append(htbl)
    events = list(getattr(mon, "events", None) or [])
    if events:
        etbl = Table(box=box.SIMPLE_HEAD, title="Eventos", title_justify="left")
        for col in ("Hora", "Evento", "Proto", "Remoto", "Estado"):
            etbl.add_column(col)
        for ev in list(reversed(events))[:8]:
            kind = ev.get("kind", "")
            etbl.add_row(_fmt_net_t(ev.get("t")), theme.paint("nuevo" if kind == "new" else "cerrado", theme.OK if kind == "new" else theme.MUTED),
                        ev.get("proto", ""), ev.get("remote", ""), ev.get("state", ""))
        extras.append(etbl)
    tbl = Table(box=box.SIMPLE_HEAD, title=f"Sockets ({len(mon.sockets)})", title_justify="left")
    for col in ("Proto", "Estado", "Local", "Remoto"):
        tbl.add_column(col)
    for s_ in mon.sockets[:15]:
        tbl.add_row(s_.proto, theme.paint(s_.state, theme.OK if s_.state == "ESTAB" else theme.MUTED), s_.local, s_.remote)
    return Group(Panel(Group(*lines), border_style=theme.hx(theme.color_for(mon.dev.key))), *extras, tbl, Text("Ctrl+C para terminar", style="dim"))


def _fmt_net_t(ts) -> str:
    if not ts:
        return ""
    import time as _t
    return _t.strftime("%H:%M:%S", _t.localtime(ts))


def cmd_shell(args) -> int:
    devices = adbmod.list_devices()
    dev = ui.select_device(devices, args.device, prompt="¿Shell de qué dispositivo?")
    cmd = [adbmod.adb_path(), "-s", dev.serial, "shell"] + (args.cmd or [])
    os.execv(cmd[0], cmd)
    return 0


def cmd_run(args) -> int:
    """Ejecuta una línea de shell local con `adb` apuntando al dispositivo (-s serial)."""
    from . import runner
    cmd = " ".join(args.cmd).strip()
    if not cmd:
        raise ui.UserError("Indica el comando, p.ej.: droid run -d pix 'adb shell getprop | grep -iE \"ro.product\"'")
    devices = adbmod.list_devices(details=False)
    if args.all:
        targets = adbmod.dedupe([d for d in devices if d.online])
        if not targets:
            raise ui.UserError("No hay dispositivos online.")
    else:
        targets = [ui.select_device(devices, args.device, prompt="¿En qué dispositivo?")]
    final, mode = runner.normalize(cmd)
    rc_total = 0
    for dev in targets:
        if len(targets) > 1 or not args.quiet:
            ui.errc.print(f"[{theme.hx(theme.color_for(dev.key))} bold]$ {escape(final)}[/] [dim]({escape(dev.display)} · adb -s {escape(dev.serial)})[/]")
        rc, secs, _ = runner.run(dev.serial, cmd, on_line=lambda line: print(line, flush=True), timeout=args.timeout)
        if not args.quiet:
            ui.errc.print(f"[dim]{'✔' if rc == 0 else '✖'} código {rc} · {secs:.2f}s[/]")
        rc_total = rc_total or rc
    return rc_total


def cmd_adb(args) -> int:
    devices = adbmod.list_devices(details=False)
    dev = ui.select_device(devices, args.device, prompt="¿Dispositivo?")
    cmd = [adbmod.adb_path(), "-s", dev.serial] + (args.args or [])
    os.execv(cmd[0], cmd)
    return 0


def cmd_config(args) -> int:
    cfg = config.load()
    if not args.key:
        ui.console.print(f"[dim]{config.CONFIG_FILE}[/]")
        for k, v in cfg.items():
            ui.console.print(f"  {k} = {v}")
        try:
            ui.console.print(f"[dim]adb detectado: {adbmod.adb_path()}[/]")
        except adbmod.AdbError as e:
            ui.warn(str(e))
        return 0
    if args.key not in config.DEFAULTS:
        raise ui.UserError(f"Clave desconocida '{args.key}'. Válidas: {', '.join(config.DEFAULTS)}")
    if args.value is None:
        print(cfg.get(args.key))
        return 0
    v: object = args.value
    default = config.DEFAULTS[args.key]
    if isinstance(default, bool):
        v = args.value.lower() in ("1", "true", "yes", "si", "sí", "on")
    elif isinstance(default, int):
        v = int(args.value)
    cfg[args.key] = v
    config.save(cfg)
    ui.ok(f"{args.key} = {v}")
    return 0


# ---------------------------------------------------------------- parser

def _add_log_filters(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("filtros")
    g.add_argument("-p", "--package", action="append", metavar="PKG", help="solo logs de este paquete/proceso (repetible)")
    g.add_argument("-t", "--tag", action="append", metavar="TAG", help="solo estos tags; admite comodines: 'Retrofit*' (repetible)")
    g.add_argument("-l", "--level", metavar="NIVEL", help="nivel mínimo: V D I W E F")
    g.add_argument("-g", "--grep", metavar="REGEX", help="solo líneas cuyo mensaje/tag coincide (se resalta)")
    g.add_argument("-x", "--exclude", metavar="REGEX", help="oculta líneas que coinciden")
    g.add_argument("--pid", action="append", type=int, metavar="PID", help="solo este proceso por PID (repetible; ver `droid ps`)")
    g.add_argument("--tid", action="append", type=int, metavar="TID", help="solo este hilo por TID (repetible; ver `droid ps --threads`)")
    g.add_argument("--thread", action="append", metavar="NOMBRE", help="solo hilos con este nombre; substring o comodín: 'OkHttp*', main, 'pool-*' (requiere -p/--pid)")
    v = p.add_argument_group("presentación")
    v.add_argument("--hl", dest="highlight", metavar="REGEX", help="resalta sin filtrar")
    v.add_argument("--raw", action="store_true", help="líneas crudas de logcat, sin formatear")
    v.add_argument("--show-tid", action="store_true", help="muestra el TID numérico")
    v.add_argument("--show-thread", action="store_true", help="muestra el nombre del hilo (requiere -p/--pid para resolverlo)")
    v.add_argument("--show-proc", action="store_true", help="muestra el nombre del proceso junto al PID (mismo color que el PID)")
    v.add_argument("--no-smart", action="store_true", help="sin resaltado automático de excepciones/crash/ANR/URLs")
    v.add_argument("--no-pid", action="store_true", help="oculta el PID")
    v.add_argument("--no-time", action="store_true", help="oculta la hora")
    v.add_argument("--date", action="store_true", help="muestra la fecha")
    v.add_argument("--no-wrap", action="store_true", help="no partir mensajes largos")
    v.add_argument("--tag-width", type=int, metavar="N", help="ancho de la columna TAG")
    v.add_argument("--color", choices=("auto", "always", "never"), default="auto")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="droid", description="Gestión de dispositivos ADB: USB/WiFi, logs con color y caché post-mortem.")
    p.add_argument("-V", "--version", action="version", version=f"droid {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="comando")
    p.set_defaults(func=cmd_ls, all=False, json=False)

    ls = sub.add_parser("ls", aliases=["devices"], help="lista dispositivos conectados (USB y WiFi)")
    ls.add_argument("-a", "--all", action="store_true", help="incluye WiFi conocidos que no están conectados")
    ls.add_argument("-w", "--watch", action="store_true", help="tabla en vivo: se refresca sola al conectar/desconectar")
    ls.add_argument("-i", "--interval", type=float, default=5.0, metavar="SEG", help="con --watch: refresco periódico de batería/IP (defecto 5)")
    ls.add_argument("-r", "--reconnect", action="store_true", help="antes de listar, `adb reconnect offline`")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=cmd_ls)

    rf = sub.add_parser("refresh", aliases=["reconnect"], help="re-detecta dispositivos (adb reconnect offline; --hard reinicia el servidor adb) y lista")
    rf.add_argument("--hard", action="store_true", help="kill-server + start-server")
    rf.add_argument("-a", "--all", action="store_true")
    rf.set_defaults(func=cmd_refresh, json=False, watch=False, reconnect=False)

    w = sub.add_parser("wifi", help="configurar/conectar dispositivos por WiFi")
    ws = w.add_subparsers(dest="wcmd", metavar="acción")
    w.set_defaults(func=cmd_wifi_setup, device=None, port=None)
    s = ws.add_parser("setup", help="pasa un dispositivo USB a modo WiFi (adb tcpip + connect)")
    s.add_argument("device", nargs="?", help="índice/serial/alias/modelo (por defecto pregunta)")
    s.add_argument("--port", type=int, help=f"puerto TCP (defecto {config.DEFAULTS['wifi_port']})")
    s.set_defaults(func=cmd_wifi_setup)
    c = ws.add_parser("connect", help="reconecta un dispositivo WiFi conocido (o ip[:puerto])")
    c.add_argument("target", nargs="?", help="alias, modelo, serial o ip[:puerto]")
    c.add_argument("--all", action="store_true", help="todos los conocidos")
    c.add_argument("--port", type=int)
    c.set_defaults(func=cmd_wifi_connect)
    d = ws.add_parser("disconnect", help="desconecta un dispositivo WiFi")
    d.add_argument("target", nargs="?")
    d.add_argument("--all", action="store_true")
    d.set_defaults(func=cmd_wifi_disconnect)
    pr = ws.add_parser("pair", help="Android 11+: vincula con código (Depuración inalámbrica)")
    pr.add_argument("hostport", help="IP:PUERTO de vinculación que muestra el móvil")
    pr.add_argument("code", nargs="?", help="código de 6 dígitos (si no, se pregunta)")
    pr.set_defaults(func=cmd_wifi_pair)
    u = ws.add_parser("usb", help="devuelve adb del dispositivo a modo USB")
    u.add_argument("device", nargs="?")
    u.set_defaults(func=cmd_wifi_usb)
    f = ws.add_parser("forget", help="olvida un dispositivo WiFi guardado")
    f.add_argument("target")
    f.set_defaults(func=cmd_wifi_forget)

    lg = sub.add_parser("logs", aliases=["log", "logcat"], help="logcat en vivo con colores (y caché automática)")
    lg.add_argument("device", nargs="?", help="índice/serial/alias/modelo/'usb'/'wifi' (por defecto pregunta)")
    _add_log_filters(lg)
    o = lg.add_argument_group("origen")
    o.add_argument("-n", "--tail", type=int, metavar="N", help="empieza por las últimas N líneas del buffer (defecto: todo el buffer)")
    o.add_argument("-c", "--clear", action="store_true", help="vacía el buffer de logcat antes de empezar")
    o.add_argument("-b", "--buffer", action="append", metavar="BUF", help="buffers: main system crash events radio all (repetible)")
    o.add_argument("--no-cache", action="store_true", help="no guardar esta sesión en caché")
    o.add_argument("--no-reconnect", action="store_true", help="salir si el dispositivo se desconecta")
    lg.set_defaults(func=cmd_logs)

    ca = sub.add_parser("cache", help="logs guardados por dispositivo (post-mortem)")
    cs = ca.add_subparsers(dest="ccmd", metavar="acción")
    ca.set_defaults(func=cmd_cache_ls, device=None)
    cl = cs.add_parser("ls", help="lista dispositivos con caché, o sesiones de uno")
    cl.add_argument("device", nargs="?")
    cl.set_defaults(func=cmd_cache_ls)
    csh = cs.add_parser("show", help="muestra una sesión guardada con los mismos filtros/colores que `logs`")
    csh.add_argument("device", nargs="?", help="dispositivo (defecto: el más reciente)")
    csh.add_argument("-s", "--session", help="# de sesión (1 = más reciente) o id; defecto: la última")
    csh.add_argument("-n", "--tail", type=int, metavar="N", help="solo las últimas N líneas (tras filtrar)")
    csh.add_argument("-f", "--follow", action="store_true", help="sigue la sesión si está en curso")
    _add_log_filters(csh)
    csh.set_defaults(func=cmd_cache_show)
    cp = cs.add_parser("path", help="imprime la ruta de la caché")
    cp.add_argument("device", nargs="?")
    cp.set_defaults(func=cmd_cache_path)
    co = cs.add_parser("open", help="abre la carpeta en Finder")
    co.add_argument("device", nargs="?")
    co.set_defaults(func=cmd_cache_open)
    cc = cs.add_parser("clean", help="borra sesiones antiguas")
    cc.add_argument("--device")
    cc.add_argument("--older-than", metavar="EDAD", help="p.ej. 30d, 12h, 2w")
    cc.add_argument("--keep", type=int, metavar="N", help="conserva las N más recientes por dispositivo")
    cc.add_argument("--all", action="store_true", help="borra todo (menos sesiones en curso)")
    cc.add_argument("--dry-run", action="store_true")
    cc.add_argument("-y", "--yes", action="store_true")
    cc.set_defaults(func=cmd_cache_clean)

    rc = sub.add_parser("record", help="graba logcat en background a la caché (sobrevive a cerrar la terminal)")
    rs = rc.add_subparsers(dest="rcmd", metavar="acción")
    rc.set_defaults(func=cmd_record_status)
    r1 = rs.add_parser("start", help="inicia grabación")
    r1.add_argument("device", nargs="?")
    r1.add_argument("--all", action="store_true", help="todos los dispositivos online")
    r1.add_argument("-b", "--buffer", action="append", metavar="BUF")
    r1.set_defaults(func=cmd_record_start)
    r2 = rs.add_parser("stop", help="detiene grabación")
    r2.add_argument("device", nargs="?")
    r2.add_argument("--all", action="store_true")
    r2.set_defaults(func=cmd_record_stop)
    r3 = rs.add_parser("status", help="grabaciones activas")
    r3.set_defaults(func=cmd_record_status)

    al = sub.add_parser("alias", help="pon un nombre corto a un dispositivo (droid logs pixel)")
    al.add_argument("device")
    al.add_argument("name", nargs="?")
    al.add_argument("--remove", action="store_true")
    al.set_defaults(func=cmd_alias)

    ap = sub.add_parser("apps", help="apps de tus proyectos instaladas en el dispositivo (debuggables)")
    ap.add_argument("device", nargs="?")
    ap.add_argument("--all", action="store_true", help="también las no instaladas")
    ap.add_argument("--projects", metavar="DIR", help="carpeta de proyectos (defecto: config projects_dir)")
    ap.set_defaults(func=cmd_apps)

    ps = sub.add_parser("ps", help="procesos e hilos del dispositivo (para elegir qué filtrar en logs)")
    ps.add_argument("device", nargs="?")
    ps.add_argument("-p", "--package", action="append", metavar="PKG", help="solo procesos de este paquete (repetible)")
    ps.add_argument("--pid", action="append", type=int, metavar="PID")
    ps.add_argument("-T", "--threads", action="store_true", help="lista los hilos del proceso (-p o --pid)")
    ps.add_argument("-a", "--all", action="store_true", help="todos los procesos, no solo apps de usuario")
    ps.add_argument("-s", "--sort", choices=("cpu", "mem", "name"), default="cpu")
    ps.add_argument("-n", "--limit", type=int, metavar="N", help="máximo de filas")
    ps.set_defaults(func=cmd_ps)

    ins = sub.add_parser("inspect", help="inspector en vivo: CPU por hilo, RAM (RSS/PSS/heaps), frames/jank, salidas de la app")
    ins.add_argument("device", nargs="?")
    ins.add_argument("-p", "--package", metavar="PKG", help="app a inspeccionar (sin -p lista tus apps)")
    ins.add_argument("-i", "--interval", type=float, default=1.0, metavar="SEG", help="segundos entre muestras (defecto 1)")
    ins.add_argument("-d", "--duration", metavar="DUR", help="p.ej. 30s, 5m (defecto: hasta Ctrl+C)")
    ins.add_argument("--meminfo-every", type=float, default=10.0, metavar="SEG", help="cada cuánto pedir dumpsys meminfo (defecto 10)")
    ins.add_argument("--top", type=int, default=15, help="hilos a mostrar")
    ins.add_argument("--thread", metavar="PAT", help="filtra hilos por nombre (substring o comodín)")
    ins.add_argument("--json", action="store_true", help="una línea JSON por muestra (para scripts)")
    ins.add_argument("--no-record", action="store_true", help="no guardar la sesión en ~/.droid/inspect")
    ins.add_argument("--list", action="store_true", help="lista sesiones guardadas")
    ins.add_argument("--replay", metavar="ARCHIVO|#", help="resumen de una sesión guardada")
    ins.add_argument("--chart", action="store_true", help="con --replay: gráficos (sparks, histogramas) en vez de resumen de texto")
    ins.set_defaults(func=cmd_inspect)

    dbp = sub.add_parser("db", help="bases de datos SQLite de una app debuggable (run-as): tablas, filas, SQL, CSV, prefs")
    dbp.add_argument("device", nargs="?")
    dbp.add_argument("-p", "--package", metavar="PKG")
    dbp.add_argument("--list", action="store_true", help="solo lista las DBs")
    dbp.add_argument("--db", metavar="#|NOMBRE", help="qué DB usar si hay varias")
    dbp.add_argument("--table", metavar="TABLA", help="muestra filas de una tabla")
    dbp.add_argument("--schema", action="store_true", help="con --table: muestra el CREATE TABLE")
    dbp.add_argument("--query", metavar="SQL", help="ejecuta una consulta sobre el snapshot local")
    dbp.add_argument("--limit", type=int, default=50)
    dbp.add_argument("--offset", type=int, default=0)
    dbp.add_argument("--csv", action="store_true", help="salida CSV (con --table o --query)")
    dbp.add_argument("--export", metavar="DIR", help="copia la DB (y -wal/-shm) a este directorio")
    dbp.add_argument("--prefs", action="store_true", help="muestra shared_prefs/*.xml")
    dbp.set_defaults(func=cmd_db)

    filesp = sub.add_parser("files", help="explorador de archivos: sandbox de apps (run-as) y /sdcard, /data/local/tmp, /proc/<pid>")
    filesp.add_argument("device", nargs="?")
    filesp.add_argument("-p", "--package", metavar="PKG")
    filesp.add_argument("--root", choices=["sandbox", "sdcard", "tmp", "proc"], default="sandbox")
    filesp.add_argument("--pid", metavar="PID")
    filesp.add_argument("path", nargs="?", default="", metavar="PATH")
    filesp.add_argument("--pull", metavar="DEST", help="descarga PATH a este directorio local")
    filesp.add_argument("--cat", action="store_true", help="imprime el contenido de PATH (como preview)")
    filesp.add_argument("--rm", action="store_true", help="borra PATH (pide confirmación salvo -y)")
    filesp.add_argument("-y", "--yes", action="store_true", help="con --rm: no pedir confirmación")
    filesp.add_argument("--json", action="store_true", help="lista el contenido de PATH como JSON")
    filesp.set_defaults(func=cmd_files)

    netp = sub.add_parser("net", help="red: tasa por interfaz en vivo, totales por app y sockets abiertos")
    netp.add_argument("device", nargs="?")
    netp.add_argument("-p", "--package", metavar="PKG")
    netp.add_argument("-i", "--interval", type=float, default=1.0)
    netp.add_argument("-d", "--duration", metavar="DUR")
    netp.add_argument("--sockets", action="store_true", help="solo lista sockets y sale")
    netp.add_argument("--json", action="store_true")
    netp.set_defaults(func=cmd_net)

    sh = sub.add_parser("shell", help="adb shell en el dispositivo elegido")
    sh.add_argument("device", nargs="?")
    sh.add_argument("cmd", nargs=argparse.REMAINDER)
    sh.set_defaults(func=cmd_shell)

    rn = sub.add_parser("run", aliases=["x"], help="línea de shell con `adb` ya apuntando al dispositivo: droid run -d pix 'adb shell getprop | grep ro.product'")
    rn.add_argument("-d", "--device", help="índice/serial/alias/modelo (si hay uno solo, no hace falta)")
    rn.add_argument("--all", action="store_true", help="en todos los dispositivos online")
    rn.add_argument("-q", "--quiet", action="store_true", help="solo la salida del comando")
    rn.add_argument("--timeout", type=float, default=120.0)
    rn.add_argument("cmd", nargs=argparse.REMAINDER, help="comando (entre comillas si lleva | o \"); sin `adb` delante se antepone `adb shell`; `!` = local")
    rn.set_defaults(func=cmd_run)

    ad = sub.add_parser("adb", help="ejecuta adb -s <dispositivo> <args…>")
    ad.add_argument("device")
    ad.add_argument("args", nargs=argparse.REMAINDER)
    ad.set_defaults(func=cmd_adb)

    uiapp = sub.add_parser("ui", aliases=["app", "tui", "menu"], help="abre la app interactiva de terminal (= `droid` sin argumentos)")
    uiapp.add_argument("device", nargs="?", help="dispositivo inicial")
    uiapp.set_defaults(func=cmd_ui)

    cf = sub.add_parser("config", help="ver/cambiar configuración (~/.droid/config.json)")
    cf.add_argument("key", nargs="?")
    cf.add_argument("value", nargs="?")
    cf.set_defaults(func=cmd_config)

    wk = sub.add_parser("_worker")
    wk.add_argument("--device-json", required=True)
    wk.add_argument("-b", "--buffer", action="append")
    wk.set_defaults(func=cmd_worker)
    return p


def cmd_ui(args) -> int:
    from .tui import run_app
    return run_app(device=getattr(args, "device", None))


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    if argv is None:
        argv = sys.argv[1:]
    if not argv and sys.stdin.isatty() and sys.stdout.isatty() and bool(config.get("ui_on_empty")):
        return cmd_ui(argparse.Namespace(device=None))
    args, extra = parser.parse_known_args(argv)
    if extra:
        if argv and argv[0] == "files" and not getattr(args, "path", "") and len(extra) == 1 and not extra[0].startswith("-"):
            args.path = extra[0]
        else:
            parser.error("argumentos no reconocidos: " + " ".join(extra))
    try:
        return int(args.func(args) or 0)
    except ui.UserError as e:
        ui.fail(str(e))
        return 1
    except adbmod.AdbError as e:
        ui.fail(str(e))
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
