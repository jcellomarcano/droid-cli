"""Paneles de la TUI para el inspector (Fase 1), base de datos y red."""
import fnmatch
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from rich.markup import escape
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Input, RichLog, Select, Sparkline, Static, TabbedContent, TabPane, TextArea
from collections import deque
import threading

from . import config, theme
from .suggest import DroidAutoComplete
from .ui import human_size


def T(text, color: Optional[int] = None, bold: bool = False, dim: bool = False) -> Text:
    style = []
    if color is not None:
        style.append(f"color({color})")
    if bold:
        style.append("bold")
    if dim:
        style.append("dim")
    return Text(str(text), style=" ".join(style))


# ============================================================================ Inspector

class InspectPane(Vertical):
    DEFAULT_CSS = """
    InspectPane { height: 1fr; }
    InspectPane .bar { height: 3; }
    InspectPane .bar Input { width: 1fr; }
    InspectPane .bar Input.narrow { width: 10; }
    InspectPane #i_head { height: auto; padding: 0 1; }
    InspectPane #i_sparks { height: 4; }
    InspectPane .spark { width: 1fr; height: 3; margin: 0 1; }
    InspectPane #sp_cpu > .sparkline--max-color { color: #ff5f5f; }
    InspectPane #sp_cpu > .sparkline--min-color { color: #5f0000; }
    InspectPane #sp_mem > .sparkline--max-color { color: #ffaf00; }
    InspectPane #sp_mem > .sparkline--min-color { color: #5f3700; }
    InspectPane #sp_fps > .sparkline--max-color { color: #5fd75f; }
    InspectPane #sp_fps > .sparkline--min-color { color: #005f00; }
    InspectPane .sparklabel { width: 1fr; height: 1; padding: 0 1; color: $text-muted; }
    InspectPane #i_threads { height: 1fr; }
    InspectPane #i_exits { height: 8; }
    InspectPane .hint { color: $text-muted; height: 1; padding: 0 1; }
    InspectPane #i_switch { height: 1fr; }
    """
    BINDINGS = [
        Binding("s", "toggle", "Iniciar/Parar"),
        Binding("l", "logs_thread", "Logs del hilo"),
        Binding("m", "meminfo", "meminfo ya"),
        Binding("e", "exits", "Salidas"),
        Binding("f", "focus_pkg", "App"),
        Binding("o", "view_overview", "Resumen"),
        Binding("u", "view_memoria", "Memoria"),
    ]

    def __init__(self, **kw):
        super().__init__(**kw)
        self.session = None
        self.cpu_hist: List[float] = []
        self.mem_hist: List[float] = []
        self.fps_hist: List[float] = []

    def compose(self) -> ComposeResult:
        with Horizontal(classes="bar"):
            i_pkg = Input(placeholder="paquete a inspeccionar (Enter en Apps lo rellena)", id="i_pkg"); yield i_pkg
            yield DroidAutoComplete(i_pkg, provider=lambda: self.app.suggest.packages("pkg"))
            i_int = Input(placeholder="seg 1", id="i_int", classes="narrow"); yield i_int
            yield DroidAutoComplete(i_int, provider=self.app.suggest.intervals)
            i_thread = Input(placeholder="filtro de hilo (main, OkHttp*)", id="i_thread"); yield i_thread
            yield DroidAutoComplete(i_thread, provider=lambda: self.app.suggest.threads(i_pkg.value, str(self.session.pid) if self.session and self.session.pid else ""))
        with TabbedContent(id="i_switch", initial="i_view_overview"):
            with TabPane("Resumen", id="i_view_overview"):
                yield Static("[dim]s: iniciar · la app debe estar corriendo (o arrancará el muestreo cuando aparezca)[/]", id="i_head")
                with Horizontal(id="i_sparks"):
                    with Vertical(classes="spark"):
                        yield Static("CPU", classes="sparklabel", id="lbl_cpu")
                        yield Sparkline([0.0], id="sp_cpu")
                    with Vertical(classes="spark"):
                        yield Static("RSS", classes="sparklabel", id="lbl_mem")
                        yield Sparkline([0.0], id="sp_mem")
                    with Vertical(classes="spark"):
                        yield Static("FPS", classes="sparklabel", id="lbl_fps")
                        yield Sparkline([0.0], id="sp_fps")
                yield DataTable(id="i_threads", cursor_type="row", zebra_stripes=True)
                yield DataTable(id="i_exits", cursor_type="row", zebra_stripes=True)
                yield Static("", id="i_hint", classes="hint")
            with TabPane("Memoria", id="i_view_memoria"):
                yield MemoriaView(id="memoria_view")

    def on_mount(self) -> None:
        self.query_one("#i_threads", DataTable).add_columns("TID", "Hilo", "%CPU", "", "Estado")
        self.query_one("#i_exits", DataTable).add_columns("Fecha", "Razón", "PID", "Detalle")
        self.query_one("#i_hint", Static).update("[dim]s: iniciar/parar · l: logs del hilo seleccionado · m: meminfo ahora · e: salidas · f: cambiar app · o: resumen · u: memoria · se guarda en ~/.droid/inspect[/]")
        self.set_interval(0.5, self._refresh)

    def action_view_overview(self) -> None:
        self.query_one("#i_switch", TabbedContent).active = "i_view_overview"
        self.query_one("#i_threads", DataTable).focus()

    def action_view_memoria(self) -> None:
        self.query_one("#i_switch", TabbedContent).active = "i_view_memoria"
        self.query_one("#me_breakdown", DataTable).focus()

    # --- control ---
    def set_package(self, pkg: str) -> None:
        self.query_one("#i_pkg", Input).value = pkg

    def action_focus_pkg(self) -> None:
        self.query_one("#i_pkg", Input).focus()

    @on(Input.Submitted)
    def _submitted(self, ev: Input.Submitted) -> None:
        if ev.input.id == "i_pkg":
            self.app.suggest.remember("pkg", ev.value)
        if ev.input.id == "i_thread":
            self.app.suggest.remember("thread", ev.value)
        if ev.input.id in ("i_pkg", "i_int"):
            if self.session and self.session.running:
                self._stop()
            self._start()
            self.query_one("#i_threads", DataTable).focus()
        ev.stop()

    def action_toggle(self) -> None:
        if self.session and self.session.running:
            self._stop()
        else:
            self._start()

    def _start(self) -> None:
        from .inspector import InspectSession
        app = self.app
        dev = app.current
        if not dev:
            app.notify("Elige un dispositivo primero (pestaña 1)", severity="warning")
            return
        pkg = self.query_one("#i_pkg", Input).value.strip()
        if not pkg:
            app.notify("Indica el paquete (o pulsa Enter sobre una app en la pestaña 5)", severity="warning")
            self.query_one("#i_pkg", Input).focus()
            return
        try:
            interval = float(self.query_one("#i_int", Input).value.strip() or "1")
        except ValueError:
            interval = 1.0
        self.cpu_hist, self.mem_hist, self.fps_hist = [], [], []
        self.session = InspectSession(dev, pkg, interval=interval, meminfo_every=8.0, record=True,
                                      on_status=lambda k, t: app.call_from_thread(app.notify, t, severity={"err": "error", "warn": "warning"}.get(k, "information"), timeout=3))
        self.session.start()
        self.query_one(MemoriaView).set_session(self.session)
        app.notify(f"Inspeccionando {pkg} en {dev.display}", timeout=2)

    def _stop(self) -> None:
        if self.session:
            self.session.stop()
            s = self.session.summary()
            self.app.notify(f"Sesión guardada: {self.session.path.name if self.session.path else '-'} · CPU media {s.get('cpu_avg')}% · jank {s.get('jank_pct')}%", timeout=6)
            self.query_one(MemoriaView).set_session(self.session)

    def load_replay(self, path: Path) -> None:
        """Carga una sesion grabada (JSONL) y la deja disponible para Resumen y Memoria como una sesion de solo lectura."""
        from types import SimpleNamespace

        from .inspector import ExitRecord, MemInfo, Sample, load_session
        from .memoria import GcEvent, LeakFlag

        data = load_session(path)
        meta = data.get("meta") or {}
        samples = [Sample.from_dict(d) for d in data.get("samples", [])]
        meminfos = [MemInfo.from_dict(d) for d in data.get("meminfo", [])]
        gc_events = [GcEvent(**{k: v for k, v in d.items() if k != "type"}) for d in data.get("gc", [])]
        leak_flags = [LeakFlag(**{k: v for k, v in d.items() if k != "type"}) for d in data.get("leak", [])]
        exits = [ExitRecord(**{k: v for k, v in d.items() if k != "type"}) for d in data.get("exits", [])]
        dev_dict = meta.get("device") or {}
        pid = samples[-1].pid if samples else None
        replay = SimpleNamespace(
            dev=SimpleNamespace(key=dev_dict.get("key", "?"), display=dev_dict.get("display", dev_dict.get("model", "?"))),
            package=meta.get("package", "?"), debuggable=None, path=path, interval=meta.get("interval", 1.0),
            samples=samples, meminfos=meminfos, exits=exits, gc_events=gc_events, leak_flags=leak_flags,
            running=False, pid=pid, device_tz_offset=meta.get("device_tz_offset"),
        )
        self.session = replay
        self.query_one(MemoriaView).set_session(self.session)

    def action_meminfo(self) -> None:
        if self.session and self.session.running and self.session.pid:
            self.session._mem_last = 0.0
            self.app.notify("Pidiendo dumpsys meminfo…", timeout=2)

    def action_exits(self) -> None:
        if self.session:
            self.run_worker(self.session.refresh_exits, thread=True)

    def action_logs_thread(self) -> None:
        ses = self.session
        if not ses or not ses.pid:
            return
        t = self.query_one("#i_threads", DataTable)
        try:
            key = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        except Exception:
            key = None
        name = ses.thread_names.get(int(key)) if key and key.isdigit() else None
        if key and key.isdigit() and int(key) == ses.pid:
            name = "main"
        self.app.open_logs_for(pids=[ses.pid], threads=[name] if name else [])

    # --- refresco ---
    def _refresh(self) -> None:
        ses = self.session
        if not ses:
            return
        from .inspector import oom_label
        last = ses.samples[-1] if ses.samples else None
        head = self.query_one("#i_head", Static)
        dev_c = theme.hx(theme.color_for(ses.dev.key))
        if last is None:
            head.update(f"[{dev_c} bold]{escape(ses.dev.display)}[/] · {escape(ses.package)} · [dim]esperando primera muestra…[/]")
            return
        if not last.alive:
            head.update(f"[{dev_c} bold]{escape(ses.dev.display)}[/] · {escape(ses.package)} · [{theme.hx(theme.ERR)} bold]proceso no corriendo[/] [dim](se reanuda cuando arranque)[/]"
                        + ("" if ses.running else " · [dim]parado[/]"))
            return
        if ses.running and (not self.cpu_hist or last.t != getattr(self, "_last_t", None)):
            self._last_t = last.t
            self.cpu_hist.append(last.cpu)
            self.mem_hist.append(float(last.pss_kb or last.rss_kb))
            self.fps_hist.append(last.fps)
            for h in (self.cpu_hist, self.mem_hist, self.fps_hist):
                del h[:-120]
            self.query_one("#sp_cpu", Sparkline).data = self.cpu_hist or [0.0]
            self.query_one("#sp_mem", Sparkline).data = self.mem_hist or [0.0]
            self.query_one("#sp_fps", Sparkline).data = self.fps_hist or [0.0]
        pc = theme.hx(theme.pid_color(last.pid))
        mi = ses.meminfos[-1] if ses.meminfos else None
        parts = [
            f"[{dev_c} bold]{escape(ses.dev.display)}[/] · {escape(ses.package)} · [{pc} bold]pid {last.pid}[/] · {oom_label(last.oom_adj)} · {last.nthreads} hilos · "
            + ("debuggable" if ses.debuggable else "release") + ("" if ses.running else f" · [{theme.hx(theme.WARN)}]parado[/]"),
            f"[bold]CPU[/] [{theme.hx(theme.cpu_color(last.cpu))}]{last.cpu:5.1f}%[/] [dim]de 1 core · {last.cpu_total_pct:.1f}% de {last.ncpu} cores[/]   "
            f"[bold]RSS[/] [{theme.hx(theme.WARN)}]{theme.kb(last.rss_kb)}[/] [dim](anon {theme.kb(last.rss_anon_kb)} · swap {theme.kb(last.swap_kb)})[/]"
            + (f"   [bold]PSS[/] [{theme.hx(theme.WARN)}]{theme.kb(last.pss_kb)}[/]" if last.pss_kb is not None else "")
            + f"   [bold]Frames[/] [{theme.hx(theme.OK if last.fps > 0 else theme.MUTED)}]{last.fps:4.1f} fps[/] "
            f"[{theme.hx(theme.ERR if last.jank_pct > 10 else theme.OK)}]jank {last.jank_pct:.0f}%[/] [dim]p50 {last.p50}ms p90 {last.p90}ms p99 {last.p99}ms[/]",
        ]
        if mi:
            parts.append(f"[bold]meminfo[/] [dim]PSS {theme.kb(mi.pss_total_kb)} · Java heap {theme.kb(mi.java_heap_kb)}"
                         + (f" ({theme.kb(mi.dalvik_heap_alloc_kb)}/{theme.kb(mi.dalvik_heap_size_kb)})" if mi.dalvik_heap_size_kb else "")
                         + f" · Native {theme.kb(mi.native_heap_kb)} · Graphics {theme.kb(mi.graphics_kb)} · Code {theme.kb(mi.code_kb)} · Views {mi.views} · Activities {mi.activities}"
                         + (f" · [{theme.hx(theme.WARN)}]{escape(mi.error)}[/]" if mi.error else "") + "[/]")
        head.update("\n".join(parts))
        self.query_one("#lbl_cpu", Static).update(f"CPU {last.cpu:.0f}%  [dim]máx {max(self.cpu_hist or [0]):.0f}%[/]")
        self.query_one("#lbl_mem", Static).update(f"{'PSS' if last.pss_kb is not None else 'RSS'} {theme.kb(last.pss_kb or last.rss_kb)}  [dim]máx {theme.kb(max(self.mem_hist or [0]))}[/]")
        self.query_one("#lbl_fps", Static).update(f"FPS {last.fps:.0f}  [dim]jank {last.jank_pct:.0f}%[/]")
        # hilos
        pat = self.query_one("#i_thread", Input).value.strip().lower()
        t = self.query_one("#i_threads", DataTable)
        ths = last.threads
        if pat:
            ths = [th for th in ths if fnmatch.fnmatchcase(th.name.lower(), pat) or pat in th.name.lower()]
        cur = t.cursor_row
        t.clear()
        for th in ths[:40]:
            bar = "█" * min(25, int(th.cpu / 100 * 25 + 0.5))
            t.add_row(str(th.tid), T(th.name, theme.thread_color(th.name), bold=(th.name == "main")), T(f"{th.cpu:.1f}", theme.cpu_color(th.cpu)),
                      T(bar, theme.cpu_color(th.cpu)), {"R": T("R corriendo", bold=True), "D": T("D io", theme.WARN)}.get(th.state, T(th.state, dim=True)), key=str(th.tid))
        if cur is not None and t.row_count:
            try:
                t.move_cursor(row=min(cur, t.row_count - 1))
            except Exception:
                pass
        # salidas
        ex = self.query_one("#i_exits", DataTable)
        if getattr(self, "_exits_n", -1) != len(ses.exits):
            self._exits_n = len(ses.exits)
            ex.clear()
            for e in ses.exits[:20]:
                color = theme.ERR if e.reason in (4, 5, 6) else (theme.WARN if e.reason in (2, 3) else theme.MUTED)
                ex.add_row(e.timestamp, T(e.reason_name, color, bold=True), str(e.pid), (e.anr or e.description or "")[:100])


class MemoriaView(Vertical):
    DEFAULT_CSS = """
    MemoriaView { height: 1fr; }
    MemoriaView #me_head { height: auto; padding: 0 1; }
    MemoriaView #me_breakdown { height: 10; }
    MemoriaView #me_sparks { height: 4; }
    MemoriaView .spark { width: 1fr; height: 3; margin: 0 1; }
    MemoriaView .sparklabel { width: 1fr; height: 1; padding: 0 1; color: $text-muted; }
    MemoriaView #me_gc { height: 1fr; }
    MemoriaView .hint { color: $text-muted; height: 1; padding: 0 1; }
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self.session = None
        self.java_hist: List[float] = []
        self.native_hist: List[float] = []
        self.graphics_hist: List[float] = []
        self.pss_hist: List[float] = []
        self._last_mi_t: Optional[float] = None
        self._gc_n = -1

    def compose(self) -> ComposeResult:
        yield Static("[dim]esperando meminfo…[/]", id="me_head")
        yield DataTable(id="me_breakdown", cursor_type="row", zebra_stripes=True)
        with Horizontal(id="me_sparks"):
            with Vertical(classes="spark"):
                yield Static("Java", classes="sparklabel", id="melbl_java")
                yield Sparkline([0.0], id="sp_java")
            with Vertical(classes="spark"):
                yield Static("Native", classes="sparklabel", id="melbl_native")
                yield Sparkline([0.0], id="sp_native")
            with Vertical(classes="spark"):
                yield Static("Graphics", classes="sparklabel", id="melbl_graphics")
                yield Sparkline([0.0], id="sp_graphics")
            with Vertical(classes="spark"):
                yield Static("PSS", classes="sparklabel", id="melbl_pss")
                yield Sparkline([0.0], id="sp_pss")
        yield DataTable(id="me_gc", cursor_type="row", zebra_stripes=True)
        yield Static("m: meminfo ya · o: resumen · u: memoria", id="me_hint", classes="hint")

    def on_mount(self) -> None:
        self.query_one("#me_breakdown", DataTable).add_columns("Heap", "MB", "")
        self.query_one("#me_gc", DataTable).add_columns("Hora", "Tipo", "Liberado", "Pausa", "Heap tras GC")
        self.set_interval(0.5, self._refresh)

    def set_session(self, session) -> None:
        self.session = session
        self.java_hist, self.native_hist, self.graphics_hist, self.pss_hist = [], [], [], []
        self._last_mi_t = None
        self._gc_n = -1

    def _refresh(self) -> None:
        ses = self.session
        if ses is None:
            return
        meminfos = list(getattr(ses, "meminfos", None) or [])
        gc_events = list(getattr(ses, "gc_events", None) or [])
        leak_flags = list(getattr(ses, "leak_flags", None) or [])
        head = self.query_one("#me_head", Static)
        if leak_flags:
            parts = []
            for f in leak_flags:
                growth_mb = (f.last_kb - f.first_kb) / 1024.0
                span_s = max(0.0, f.until_t - f.since_t)
                parts.append(f"{f.metric} +{growth_mb:.1f} MB en {span_s:.0f} s")
            head.update(f"[{theme.hx(theme.WARN)} bold]posible fuga: " + " · ".join(parts) + " (heurística)[/]")
        elif meminfos:
            mi = meminfos[-1]
            head.update(f"[dim]último meminfo · PSS {theme.kb(mi.pss_total_kb)} · Java {theme.kb(mi.java_heap_kb)}[/]")
        else:
            head.update("[dim]esperando meminfo…[/]")

        bt = self.query_one("#me_breakdown", DataTable)
        if meminfos:
            mi = meminfos[-1]
            buckets = [
                ("Java heap", mi.java_heap_kb), ("Native heap", mi.native_heap_kb), ("Code", mi.code_kb),
                ("Stack", mi.stack_kb), ("Graphics", mi.graphics_kb), ("Private other", mi.private_other_kb),
                ("System", mi.system_kb), ("PSS total", mi.pss_total_kb),
            ]
            max_v = max((v for _, v in buckets), default=0) or 1
            bt.clear()
            for name, v in buckets:
                bt.add_row(name, f"{v / 1024.0:.1f}", theme.hbar(v, max_v, 20))
            if not self.java_hist or mi.t != self._last_mi_t:
                self._last_mi_t = mi.t
                self.java_hist.append(mi.java_heap_kb / 1024.0)
                self.native_hist.append(mi.native_heap_kb / 1024.0)
                self.graphics_hist.append(mi.graphics_kb / 1024.0)
                self.pss_hist.append(mi.pss_total_kb / 1024.0)
                for h in (self.java_hist, self.native_hist, self.graphics_hist, self.pss_hist):
                    del h[:-120]
                self.query_one("#sp_java", Sparkline).data = self.java_hist or [0.0]
                self.query_one("#sp_native", Sparkline).data = self.native_hist or [0.0]
                self.query_one("#sp_graphics", Sparkline).data = self.graphics_hist or [0.0]
                self.query_one("#sp_pss", Sparkline).data = self.pss_hist or [0.0]
            self.query_one("#melbl_java", Static).update(f"Java {mi.java_heap_kb / 1024:.0f} MB")
            self.query_one("#melbl_native", Static).update(f"Native {mi.native_heap_kb / 1024:.0f} MB")
            self.query_one("#melbl_graphics", Static).update(f"Graphics {mi.graphics_kb / 1024:.0f} MB")
            self.query_one("#melbl_pss", Static).update(f"PSS {mi.pss_total_kb / 1024:.0f} MB")

        gt = self.query_one("#me_gc", DataTable)
        if self._gc_n != len(gc_events):
            self._gc_n = len(gc_events)
            gt.clear()
            for ev in gc_events[-50:]:
                hora = datetime.fromtimestamp(ev.t).strftime("%H:%M:%S")
                gt.add_row(hora, ev.kind, theme.kb(ev.freed_kb), f"{ev.pause_ms:.1f} ms", theme.kb(ev.heap_used_kb))


# ============================================================================ Base de datos

class DbPane(Vertical):
    DEFAULT_CSS = """
    DbPane { height: 1fr; }
    DbPane .bar { height: 3; }
    DbPane .bar Input { width: 1fr; }
    DbPane #db_list { height: 8; }
    DbPane #db_tables { height: 10; }
    DbPane #db_rows { height: 1fr; }
    DbPane .hint { color: $text-muted; height: 1; padding: 0 1; }
    """
    BINDINGS = [
        Binding("r", "reload", "Recargar"),
        Binding("b", "back", "Volver a tablas"),
        Binding("x", "export_csv", "CSV"),
        Binding("p", "prefs", "Prefs"),
        Binding("f", "focus_pkg", "App"),
        Binding("slash", "focus_query", "SQL", key_display="/"),
    ]

    def __init__(self, **kw):
        super().__init__(**kw)
        self.dbs = []
        self.conn = None
        self.local: Optional[Path] = None
        self.cur_table: Optional[str] = None
        self.cur_cols: List[str] = []
        self.cur_rows: List[tuple] = []

    def compose(self) -> ComposeResult:
        with Horizontal(classes="bar"):
            d_pkg = Input(placeholder="paquete debuggable (Enter en Apps lo rellena)", id="d_pkg"); yield d_pkg
            yield DroidAutoComplete(d_pkg, provider=lambda: self.app.suggest.packages("pkg"))
            d_query = Input(placeholder="SQL sobre el snapshot (Enter ejecuta)", id="d_query"); yield d_query
            yield DroidAutoComplete(d_query, provider=lambda: self.app.suggest.sql(self.conn), multi=True)
        yield DataTable(id="db_list", cursor_type="row", zebra_stripes=True)
        yield DataTable(id="db_tables", cursor_type="row", zebra_stripes=True)
        yield DataTable(id="db_rows", cursor_type="row", zebra_stripes=True)
        yield Static("", id="db_hint", classes="hint")

    def on_mount(self) -> None:
        self.query_one("#db_list", DataTable).add_columns("Base de datos", "Tamaño", "Modificada", "WAL")
        self.query_one("#db_tables", DataTable).add_columns("Tabla", "Filas", "Tipo", "Columnas")
        self.query_one("#db_hint", Static).update("[dim]Enter en una DB: copia snapshot (run-as) · Enter en tabla: filas · /: SQL · x: exportar CSV · p: shared_prefs · r: recargar[/]")

    def set_package(self, pkg: str) -> None:
        self.query_one("#d_pkg", Input).value = pkg
        self.action_reload()

    def action_focus_pkg(self) -> None:
        self.query_one("#d_pkg", Input).focus()

    def action_focus_query(self) -> None:
        self.query_one("#d_query", Input).focus()

    @on(Input.Submitted)
    def _submitted(self, ev: Input.Submitted) -> None:
        if ev.input.id == "d_pkg":
            self.app.suggest.remember("pkg", ev.value)
            self.action_reload()
            self.query_one("#db_list", DataTable).focus()
        elif ev.input.id == "d_query":
            self.app.suggest.remember("sql", ev.value.strip())
            self._run_query(ev.value.strip())
            self.query_one("#db_rows", DataTable).focus()
        ev.stop()

    def action_reload(self) -> None:
        self._load_list()

    @work(thread=True, exclusive=True, group="db")
    def _load_list(self) -> None:
        from . import db as dbmod
        app = self.app
        dev = app.current
        pkg = self.query_one("#d_pkg", Input).value.strip()
        if not dev or not pkg:
            return
        ok, msg = dbmod.check_run_as(dev.serial, pkg)
        if not ok:
            app.call_from_thread(app.notify, f"run-as falla: {msg[:120]} (solo apps debuggables)", severity="error", timeout=8)
            return
        try:
            dbs = dbmod.list_databases(dev.serial, pkg)
        except Exception as e:
            app.call_from_thread(app.notify, f"db: {e}", severity="error")
            return
        app.call_from_thread(self._apply_list, dbs)

    def _apply_list(self, dbs) -> None:
        self.dbs = dbs
        t = self.query_one("#db_list", DataTable)
        t.clear()
        for d in dbs:
            t.add_row(T(d.path, theme.color_for(d.name), bold=True), T(human_size(d.size), theme.size_color(d.size)), d.mtime, T("✔", theme.OK) if d.wal else "", key=d.path)
        self.query_one("#db_hint", Static).update(f"[dim]{len(dbs)} base(s) de datos · Enter: snapshot y tablas · p: prefs[/]" if dbs else "[dim]sin bases de datos en databases/, files/, no_backup/, cache/[/]")

    @on(DataTable.RowSelected, "#db_list")
    def _db_selected(self, ev: DataTable.RowSelected) -> None:
        key = ev.row_key.value if ev.row_key else None
        if key:
            self._pull(key)
        ev.stop()

    @work(thread=True, exclusive=True, group="db")
    def _pull(self, remote: str) -> None:
        from . import db as dbmod
        app = self.app
        dev = app.current
        pkg = self.query_one("#d_pkg", Input).value.strip()
        try:
            dest = dbmod.snapshot_dir(dev, pkg)
            local = dbmod.pull_database(dev.serial, pkg, remote, dest)
            conn = dbmod.open_db(local)
            tabs = [(n, c, ty, dbmod.schema(conn, n)) for n, c, ty in dbmod.tables(conn)]
        except Exception as e:
            app.call_from_thread(app.notify, f"snapshot: {e}", severity="error", timeout=8)
            return
        app.call_from_thread(self._apply_tables, local, conn, tabs)

    def _apply_tables(self, local, conn, tabs) -> None:
        self.local, self.conn = local, conn
        t = self.query_one("#db_tables", DataTable)
        t.clear()
        for name, n, typ, cols in tabs:
            coltxt = Text()
            for i, (c, ty, _, pk) in enumerate(cols[:10]):
                coltxt.append_text(T(("" if i == 0 else ", ") + c, bold=pk))
                coltxt.append_text(T(f":{ty.lower()}", dim=True))
            if len(cols) > 10:
                coltxt.append_text(T(" …", dim=True))
            t.add_row(T(name, theme.color_for(name), bold=True), str(n), typ, coltxt, key=name)
        self.query_one("#db_hint", Static).update(f"[dim]snapshot {local} · Enter en tabla: filas · /: SQL · x: CSV[/]")
        t.focus()

    @on(DataTable.RowSelected, "#db_tables")
    def _table_selected(self, ev: DataTable.RowSelected) -> None:
        key = ev.row_key.value if ev.row_key else None
        if key and self.conn:
            from . import db as dbmod
            try:
                cols, rws, _ = dbmod.rows(self.conn, key, limit=500)
            except Exception as e:
                self.app.notify(f"{e}", severity="error")
                return
            self.cur_table = key
            self._show_rows(cols, rws, f'SELECT * FROM "{key}" LIMIT 500')
        ev.stop()

    def _run_query(self, sql: str) -> None:
        if not sql or not self.conn:
            self.app.notify("Primero abre una DB (Enter en la lista)", severity="warning")
            return
        from . import db as dbmod
        try:
            cols, rws, trunc = dbmod.query(self.conn, sql, 1000)
        except Exception as e:
            self.app.notify(f"SQL: {e}", severity="error", timeout=8)
            return
        self._show_rows(cols, rws, sql + (" (truncado a 1000)" if trunc else ""))

    def _show_rows(self, cols: List[str], rws: List[tuple], title: str) -> None:
        self.cur_cols, self.cur_rows = cols, rws
        t = self.query_one("#db_rows", DataTable)
        t.clear(columns=True)
        t.add_columns(*[str(c) for c in cols] or ["(sin columnas)"])
        for r in rws:
            t.add_row(*[T("NULL", dim=True) if v is None else str(v)[:120] for v in r])
        self.query_one("#db_hint", Static).update(f"[dim]{escape(title)} · {len(rws)} filas · x: exportar CSV · b: tablas[/]")
        t.focus()

    def action_back(self) -> None:
        self.query_one("#db_tables", DataTable).focus()

    def action_export_csv(self) -> None:
        if not self.cur_rows or not self.local:
            self.app.notify("No hay filas que exportar", severity="warning")
            return
        from . import db as dbmod
        out = self.local.parent / f"{self.cur_table or 'query'}-{time.strftime('%H%M%S')}.csv"
        out.write_text(dbmod.to_csv(self.cur_cols, self.cur_rows))
        self.app.notify(f"CSV: {out}", timeout=6)

    @work(thread=True)
    def action_prefs(self) -> None:
        from . import db as dbmod
        app = self.app
        dev = app.current
        pkg = self.query_one("#d_pkg", Input).value.strip()
        if not dev or not pkg:
            return
        try:
            prefs = dbmod.shared_prefs(dev.serial, pkg)
        except Exception as e:
            app.call_from_thread(app.notify, f"prefs: {e}", severity="error")
            return
        rows = []
        for name, content in prefs.items():
            for line in content.splitlines():
                if line.strip():
                    rows.append((name, line.strip()[:160]))
        app.call_from_thread(self._show_rows, ["archivo", "contenido"], rows, "shared_prefs / datastore")


# ============================================================================ Red

def _fmt_t(ts) -> str:
    if not ts:
        return ""
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _fmt_states(states: dict) -> str:
    return " ".join(f"{k}:{v}" for k, v in sorted(states.items())) if states else ""


def _remote_with_host(remote: str, dns) -> str:
    if not dns:
        return remote
    ip, _, _port = remote.rpartition(":")
    ip = ip.strip("[]") or remote
    cache = getattr(dns, "_cache", {})
    host = cache.get(ip)
    return f"{host} ({remote})" if host else remote


class NetPane(Vertical):
    DEFAULT_CSS = """
    NetPane { height: 1fr; }
    NetPane .bar { height: 3; }
    NetPane .bar Input { width: 1fr; }
    NetPane #n_head { height: auto; padding: 0 1; }
    NetPane #n_sparks { height: 4; }
    NetPane .spark { width: 1fr; height: 3; margin: 0 1; }
    NetPane #sp_rx > .sparkline--max-color { color: #5fd75f; }
    NetPane #sp_rx > .sparkline--min-color { color: #005f00; }
    NetPane #sp_tx > .sparkline--max-color { color: #5fafff; }
    NetPane #sp_tx > .sparkline--min-color { color: #00305f; }
    NetPane .sparklabel { width: 1fr; height: 1; padding: 0 1; color: $text-muted; }
    NetPane #n_hosts { height: 1fr; min-height: 4; }
    NetPane #n_events { height: 1fr; min-height: 4; }
    NetPane #n_sockets { height: 1fr; }
    NetPane .hint { color: $text-muted; height: 1; padding: 0 1; }
    """
    BINDINGS = [
        Binding("s", "toggle", "Iniciar/Parar"),
        Binding("r", "refresh_sockets", "Sockets"),
        Binding("f", "focus_pkg", "App"),
        Binding("h", "focus_hosts", "Hosts"),
        Binding("e", "focus_events", "Eventos"),
        Binding("k", "focus_sockets", "Sockets"),
    ]

    def __init__(self, **kw):
        super().__init__(**kw)
        self.mon = None
        self.rx_hist: List[float] = []
        self.tx_hist: List[float] = []

    def compose(self) -> ComposeResult:
        with Horizontal(classes="bar"):
            n_pkg = Input(placeholder="paquete (vacío = todo el dispositivo)", id="n_pkg"); yield n_pkg
            yield DroidAutoComplete(n_pkg, provider=lambda: self.app.suggest.packages("pkg"))
        yield Static("[dim]s: iniciar · tasa por interfaz en vivo (Android no da bytes por app en tiempo real sin root; los totales por app se consolidan periódicamente)[/]", id="n_head")
        with Horizontal(id="n_sparks"):
            with Vertical(classes="spark"):
                yield Static("↓ descarga", classes="sparklabel", id="lbl_rx")
                yield Sparkline([0.0], id="sp_rx")
            with Vertical(classes="spark"):
                yield Static("↑ subida", classes="sparklabel", id="lbl_tx")
                yield Sparkline([0.0], id="sp_tx")
        yield DataTable(id="n_hosts", cursor_type="row", zebra_stripes=True)
        yield DataTable(id="n_events", cursor_type="row", zebra_stripes=True)
        yield DataTable(id="n_sockets", cursor_type="row", zebra_stripes=True)
        yield Static("", id="n_hint", classes="hint")

    def on_mount(self) -> None:
        self.query_one("#n_hosts", DataTable).add_columns("Host", "IP", "Conexiones", "Estados", "Primera", "Última")
        self.query_one("#n_events", DataTable).add_columns("Hora", "Evento", "Proto", "Remoto", "Estado")
        self.query_one("#n_sockets", DataTable).add_columns("Proto", "Estado", "Local", "Remoto", "UID")
        self.query_one("#n_hint", Static).update("[dim]s: iniciar/parar · r: refrescar sockets · f: cambiar app · h: hosts · e: eventos · k: sockets[/]")
        self.set_interval(0.5, self._refresh)

    def set_package(self, pkg: str) -> None:
        self.query_one("#n_pkg", Input).value = pkg

    def action_focus_pkg(self) -> None:
        self.query_one("#n_pkg", Input).focus()

    def action_focus_hosts(self) -> None:
        self.query_one("#n_hosts", DataTable).focus()

    def action_focus_events(self) -> None:
        self.query_one("#n_events", DataTable).focus()

    def action_focus_sockets(self) -> None:
        self.query_one("#n_sockets", DataTable).focus()

    @on(Input.Submitted)
    def _submitted(self, ev: Input.Submitted) -> None:
        self.app.suggest.remember("pkg", ev.value)
        if self.mon and self.mon.running:
            self.mon.stop()
        self._start()
        self.query_one("#n_sockets", DataTable).focus()
        ev.stop()

    def action_toggle(self) -> None:
        if self.mon and self.mon.running:
            self.mon.stop()
            self.app.notify("Monitor de red parado", timeout=2)
        else:
            self._start()

    def _start(self) -> None:
        from .net import NetMonitor
        app = self.app
        dev = app.current
        if not dev:
            app.notify("Elige un dispositivo primero", severity="warning")
            return
        pkg = self.query_one("#n_pkg", Input).value.strip() or None
        self.rx_hist, self.tx_hist = [], []
        self.mon = NetMonitor(dev, pkg, interval=1.0, totals_every=10.0,
                              on_status=lambda k, t: app.call_from_thread(app.notify, t, severity={"err": "error", "warn": "warning"}.get(k, "information"), timeout=3))
        self.mon.start()

    def action_refresh_sockets(self) -> None:
        if self.mon:
            self.mon._tot_last = 0.0

    def _refresh(self) -> None:
        mon = self.mon
        if not mon or not mon.samples:
            return
        last = mon.samples[-1]
        if last.t != getattr(self, "_last_t", None) and mon.running:
            self._last_t = last.t
            self.rx_hist.append(last.rx_rate)
            self.tx_hist.append(last.tx_rate)
            del self.rx_hist[:-120]
            del self.tx_hist[:-120]
            self.query_one("#sp_rx", Sparkline).data = self.rx_hist or [0.0]
            self.query_one("#sp_tx", Sparkline).data = self.tx_hist or [0.0]
        dev_c = theme.hx(theme.color_for(mon.dev.key))
        active = [(i, r) for i, r in sorted(last.rates.items()) if r != (0.0, 0.0) or any(s.rates.get(i, (0, 0)) != (0, 0) for s in list(mon.samples)[-30:])]
        parts = [f"[{dev_c} bold]{escape(mon.dev.display)}[/] · red" + (f" · {escape(mon.package)} (uid {mon.uid})" if mon.package else " · todo el dispositivo") + ("" if mon.running else f" · [{theme.hx(theme.WARN)}]parado[/]"),
                 f"[dim]{escape(getattr(mon, 'cadence_label', ''))}[/]"]
        if last.app_rx_rate is not None:
            parts.append(f"[bold]app ↓/↑[/] [{theme.hx(theme.OK)}]↓ {theme.rate(last.app_rx_rate)}[/] [{theme.hx(theme.INFO)}]↑ {theme.rate(last.app_tx_rate or 0.0)}[/]")
        else:
            parts.append("  ".join(f"[bold]{i}[/] [{theme.hx(theme.OK)}]↓ {theme.rate(r[0])}[/] [{theme.hx(theme.INFO)}]↑ {theme.rate(r[1])}[/]" for i, r in active) or "[dim]sin tráfico[/]")
        if mon.totals:
            t = mon.totals
            sess = ""
            if mon.totals_start:
                sess = f" · en esta sesión ↓ {human_size(t['rx'] - mon.totals_start['rx'])} ↑ {human_size(t['tx'] - mon.totals_start['tx'])}"
            parts.append(f"[bold]Totales de la app[/] [{theme.hx(theme.WARN)}]↓ {human_size(t['rx'])} ↑ {human_size(t['tx'])}[/][dim]{sess} · "
                         + " ".join(f"{k}: ↓{human_size(v[0])} ↑{human_size(v[1])}" for k, v in t["by_type"].items()) + "[/]")
        self.query_one("#n_head", Static).update("\n".join(parts))
        self.query_one("#lbl_rx", Static).update(f"↓ {theme.rate(last.rx_rate)}  [dim]máx {theme.rate(max(self.rx_hist or [0]))}[/]")
        self.query_one("#lbl_tx", Static).update(f"↑ {theme.rate(last.tx_rate)}  [dim]máx {theme.rate(max(self.tx_hist or [0]))}[/]")
        hosts = getattr(mon, "hosts", []) or []
        if getattr(self, "_hosts_n", None) != len(hosts):
            self._hosts_n = len(hosts)
            ht = self.query_one("#n_hosts", DataTable)
            ht.clear()
            for h_ in hosts:
                host = h_.get("host") or ""
                ht.add_row(T(host, theme.color_for(host)) if host else T("(sin PTR)", theme.MUTED),
                           h_.get("ip", ""), str(h_.get("connections", 0)),
                           _fmt_states(h_.get("states", {})),
                           _fmt_t(h_.get("first_seen")), _fmt_t(h_.get("last_seen")))
        events = list(getattr(mon, "events", []) or [])
        if getattr(self, "_events_n", None) != len(events):
            self._events_n = len(events)
            et = self.query_one("#n_events", DataTable)
            et.clear()
            for ev in list(reversed(events))[:100]:
                kind = ev.get("kind", "")
                et.add_row(_fmt_t(ev.get("t")), T("nuevo" if kind == "new" else "cerrado", theme.OK if kind == "new" else theme.MUTED),
                           ev.get("proto", ""), ev.get("remote", ""), ev.get("state", ""))
        if getattr(self, "_socks_n", None) != (len(mon.sockets), mon._tot_last):
            self._socks_n = (len(mon.sockets), mon._tot_last)
            t = self.query_one("#n_sockets", DataTable)
            t.clear()
            dns = getattr(mon, "dns", None)
            for s_ in mon.sockets[:200]:
                t.add_row(s_.proto, T(s_.state, theme.OK if s_.state == "ESTAB" else theme.MUTED), s_.local, _remote_with_host(s_.remote, dns), str(s_.uid))
            self.query_one("#n_hint", Static).update(f"[dim]{len(mon.sockets)} sockets" + (f" del uid {mon.uid}" if mon.uid else "") + " · r: refrescar · s: parar[/]")


# ============================================================================ Consola

class ConsolePane(Vertical):
    """Comandos dirigidos al dispositivo actual. Arriba una línea rápida con sugerencias; debajo un área para pegar
    un bloque (script) y ejecutarlo con ctrl+r o el botón ▶. `adb` lleva ya el -s <serial>."""

    DEFAULT_CSS = """
    ConsolePane { height: 1fr; }
    ConsolePane .bar { height: 3; }
    ConsolePane .bar Input { width: 1fr; }
    ConsolePane #sh_block { height: 9; }
    ConsolePane #sh_code { width: 1fr; height: 100%; }
    ConsolePane #sh_btns { width: 16; height: 100%; }
    ConsolePane #sh_btns Button { width: 100%; min-width: 10; height: 3; margin: 0; }
    ConsolePane #sh_out { height: 1fr; border: round $primary; }
    ConsolePane .hint { color: $text-muted; height: 1; padding: 0 1; }
    """
    BINDINGS = [
        Binding("ctrl+r", "run_block", "Ejecutar bloque"),
        Binding("ctrl+g", "kill", "Cortar"),
        Binding("ctrl+l", "clear", "Limpiar salida"),
        Binding("k", "kill", "Cortar", show=False),
        Binding("f", "focus_cmd", "Línea"),
        Binding("b", "focus_block", "Bloque"),
    ]

    def __init__(self, **kw):
        super().__init__(**kw)
        self.queue: deque = deque(maxlen=20000)
        self.plain: deque = deque(maxlen=20000)   # salida sin color, para copiar/revisar
        self.holder: dict = {}
        self.running = False

    def compose(self) -> ComposeResult:
        with Horizontal(classes="bar"):
            cmd = Input(placeholder="línea rápida: adb shell getprop | grep -iE \"…\"   ·   getprop ro.product.model   ·   !comando local   (Enter ejecuta)", id="sh_cmd")
            yield cmd
            yield DroidAutoComplete(cmd, provider=lambda: self.app.suggest.commands("cmd"), show_on_empty=False, prevent_default_enter=False)
        with Horizontal(id="sh_block"):
            yield TextArea("", id="sh_code", soft_wrap=True, show_line_numbers=True, tab_behavior="indent")
            with Vertical(id="sh_btns"):
                yield Button("▶ Ejecutar\nctrl+r", id="sh_run", variant="success")
                yield Button("✕ Vaciar", id="sh_clear_code")
                yield Button("■ Cortar\nctrl+g", id="sh_kill", variant="error")
        yield RichLog(id="sh_out", highlight=False, markup=False, wrap=True, max_lines=20000)
        yield Static("", id="sh_hint", classes="hint")

    def on_mount(self) -> None:
        self.set_interval(0.1, self._drain)
        self._update_hint()

    def _update_hint(self) -> None:
        dev = self.app.current
        target = f"→ {theme.paint(escape(dev.display), theme.color_for(dev.key), bold=True)} [dim]({escape(dev.serial)})[/]" if dev else "[dim]sin dispositivo[/]"
        state = theme.paint(" · ejecutando… (ctrl+g corta)", theme.WARN) if self.running else ""
        self.query_one("#sh_hint", Static).update(
            f"{target}{state} [dim]· pega un bloque en el área y ctrl+r (o ▶) · líneas sin `adb` van enteras a adb shell; si empieza por `adb` corre en el Mac · ctrl+l limpia · ctrl+d cambia dispositivo[/]")

    # --- acciones ---
    def action_focus_cmd(self) -> None:
        self.query_one("#sh_cmd", Input).focus()

    def action_focus_block(self) -> None:
        self.query_one("#sh_code", TextArea).focus()

    def action_clear(self) -> None:
        self.query_one("#sh_out", RichLog).clear()

    def action_kill(self) -> None:
        from .runner import kill
        if kill(self.holder):
            self.app.notify("Comando cortado", severity="warning", timeout=2)

    def action_run_block(self) -> None:
        text = self.query_one("#sh_code", TextArea).text
        if not text.strip():
            self.app.notify("El área de bloque está vacía: pega ahí los comandos", severity="warning")
            self.action_focus_block()
            return
        self.app.suggest.remember("cmd", text.strip().splitlines()[0])
        self.run_block(text)

    @on(Button.Pressed, "#sh_run")
    def _btn_run(self, ev: Button.Pressed) -> None:
        ev.stop()
        self.action_run_block()

    @on(Button.Pressed, "#sh_clear_code")
    def _btn_clear(self, ev: Button.Pressed) -> None:
        ev.stop()
        self.query_one("#sh_code", TextArea).text = ""
        self.action_focus_block()

    @on(Button.Pressed, "#sh_kill")
    def _btn_kill(self, ev: Button.Pressed) -> None:
        ev.stop()
        self.action_kill()

    @on(Input.Submitted)
    def _submitted(self, ev: Input.Submitted) -> None:
        cmd = ev.value.strip()
        ev.stop()
        if not cmd:
            return
        self.app.suggest.remember("cmd", cmd)
        self.run_command(cmd)
        ev.input.value = ""

    # --- ejecución ---
    def _start(self, dev, header: Text, target) -> bool:
        if self.running:
            self.app.notify("Hay un comando en marcha (ctrl+g para cortarlo)", severity="warning")
            return False
        self.query_one("#sh_out", RichLog).write(header)
        self.plain.append(header.plain)
        self.running = True
        self._update_hint()
        threading.Thread(target=target, daemon=True).start()
        return True

    def run_command(self, cmd: str) -> None:
        from .runner import normalize, run
        dev = self.app.current
        if not dev:
            self.app.notify("Elige un dispositivo primero (pestaña 1)", severity="warning")
            return
        final, _ = normalize(cmd)
        head = Text()
        head.append("$ ", style=f"bold {theme.hx(theme.color_for(dev.key))}")
        head.append(final, style="bold")
        head.append(f"   [{dev.display} · adb -s {dev.serial}]", style="dim")

        def worker() -> None:
            try:
                rc, secs, _ = run(dev.serial, cmd, on_line=self.queue.append, proc_holder=self.holder)
                self._finish(rc, secs)
            except Exception as e:
                self.queue.append(Text(f"✖ {e}", style=theme.hx(theme.ERR)))
            finally:
                self.running = False
                self.app.call_from_thread(self._update_hint)
        self._start(dev, head, worker)

    def run_block(self, text: str) -> None:
        from .runner import block_mode, run_block
        dev = self.app.current
        if not dev:
            self.app.notify("Elige un dispositivo primero (pestaña 1)", severity="warning")
            return
        mode = block_mode(text)
        lines = [l for l in text.strip().splitlines() if l.strip()]
        head = Text()
        head.append("▶ ", style=f"bold {theme.hx(theme.color_for(dev.key))}")
        head.append(f"bloque de {len(lines)} línea(s) ", style="bold")
        head.append("en el dispositivo (adb shell)" if mode == "device" else "en el Mac (adb → " + dev.serial + ")", style=theme.hx(theme.INFO if mode == "device" else theme.WARN))
        head.append(f"   [{dev.display}]", style="dim")
        for l in lines[:12]:
            head.append("\n  │ " + l, style="dim")
        if len(lines) > 12:
            head.append(f"\n  │ … {len(lines) - 12} más", style="dim")

        def worker() -> None:
            try:
                rc, secs, _, _ = run_block(dev.serial, text, on_line=self.queue.append, proc_holder=self.holder)
                self._finish(rc, secs)
            except Exception as e:
                self.queue.append(Text(f"✖ {e}", style=theme.hx(theme.ERR)))
            finally:
                self.running = False
                self.app.call_from_thread(self._update_hint)
        self._start(dev, head, worker)

    def _finish(self, rc: int, secs: float) -> None:
        tail = Text()
        if rc == 0:
            tail.append(f"✔ ok · {secs:.2f}s", style=theme.hx(theme.OK))
        else:
            tail.append(f"✖ código {rc} · {secs:.2f}s", style=theme.hx(theme.ERR))
        self.queue.append(tail)

    def _drain(self) -> None:
        if not self.queue:
            return
        out = self.query_one("#sh_out", RichLog)
        batch = []
        for _ in range(min(len(self.queue), 500)):
            item = self.queue.popleft()
            self.plain.append(item.plain if isinstance(item, Text) else str(item))
            if isinstance(item, Text):
                if batch:
                    out.write(Text.from_ansi("\n".join(batch)))
                    batch = []
                out.write(item)
            else:
                batch.append(item)
        if batch:
            out.write(Text.from_ansi("\n".join(batch)))


# ============================================================================ Archivos

class FilesPane(Vertical):
    DEFAULT_CSS = """
    FilesPane { height: 1fr; }
    FilesPane .bar { height: 3; }
    FilesPane .bar Input { width: 1fr; }
    FilesPane .bar Input.narrow { width: 12; }
    FilesPane .bar Select { width: 28; }
    FilesPane #f_list { height: 1fr; }
    FilesPane .hint { color: $text-muted; height: 1; padding: 0 1; }
    """
    ROOT_OPTIONS = [
        ("Sandbox de la app", "sandbox"),
        ("/sdcard", "sdcard"),
        ("/data/local/tmp", "tmp"),
        ("/proc/<pid>", "proc"),
    ]
    GLYPH = {"dir": "📁", "link": "🔗", "file": ""}

    BINDINGS = [
        Binding("backspace", "up", "Subir"),
        Binding("r", "reload", "Recargar"),
        Binding("p", "pull", "Pull"),
        Binding("v", "preview", "Ver"),
        Binding("b", "open_db", "Abrir DB"),
        Binding("x", "delete", "Borrar"),
        Binding("f", "focus_pkg", "App"),
        Binding("o", "open_pulled", "Abrir pull", show=False),
    ]

    def __init__(self, **kw):
        super().__init__(**kw)
        self.root = "sandbox"
        self.rel_segments: List[str] = []
        self.entries: dict = {}
        self.last_pulled: Optional[Path] = None

    def compose(self) -> ComposeResult:
        with Horizontal(classes="bar"):
            f_pkg = Input(placeholder="paquete (sandbox)", id="f_pkg"); yield f_pkg
            yield DroidAutoComplete(f_pkg, provider=lambda: self.app.suggest.packages("pkg"))
            yield Select(self.ROOT_OPTIONS, value="sandbox", allow_blank=False, id="f_root")
            f_pid = Input(placeholder="pid", id="f_pid", classes="narrow"); yield f_pid
        yield Static("", id="f_breadcrumb", classes="hint")
        yield DataTable(id="f_list", cursor_type="row", zebra_stripes=True)
        yield Static("", id="f_hint", classes="hint")

    def on_mount(self) -> None:
        self.query_one("#f_list", DataTable).add_columns("", "Nombre", "Tamaño", "Modificado", "Permisos")
        self.query_one("#f_hint", Static).update("[dim]Enter: abrir/ver · backspace: subir · p: pull · v: ver · x: borrar · b: abrir como DB · r: recargar · f: paquete[/]")
        self._update_breadcrumb()

    def _rel(self) -> str:
        return "/".join(self.rel_segments)

    def _update_breadcrumb(self) -> None:
        pid = self.query_one("#f_pid", Input).value.strip()
        base = f"/proc/{pid or '<pid>'}" if self.root == "proc" else {"sandbox": "sandbox", "sdcard": "/sdcard", "tmp": "/data/local/tmp"}.get(self.root, self.root)
        rel = self._rel()
        self.query_one("#f_breadcrumb", Static).update(f"[dim]{base}{'/' + rel if rel else ''}[/]")

    def set_package(self, pkg: str) -> None:
        self.query_one("#f_pkg", Input).value = pkg
        if self.root == "sandbox":
            self.rel_segments = []
            self._load()

    def action_focus_pkg(self) -> None:
        self.query_one("#f_pkg", Input).focus()

    @on(Input.Submitted)
    def _submitted(self, ev: Input.Submitted) -> None:
        if ev.input.id in ("f_pkg", "f_pid"):
            if ev.input.id == "f_pkg":
                self.app.suggest.remember("pkg", ev.value)
            self.rel_segments = []
            self._load()
            self.query_one("#f_list", DataTable).focus()
        ev.stop()

    @on(Select.Changed, "#f_root")
    def _root_changed(self, ev: "Select.Changed") -> None:
        self.root = str(ev.value)
        self.rel_segments = []
        self._load()

    def action_reload(self) -> None:
        self._load()

    def action_up(self) -> None:
        if self.rel_segments:
            self.rel_segments.pop()
            self._load()

    @work(thread=True, exclusive=True, group="files")
    def _load(self) -> None:
        from . import files as filesmod
        app = self.app
        dev = app.current
        if not dev:
            return
        root = self.root
        pkg = self.query_one("#f_pkg", Input).value.strip() or None
        pid = self.query_one("#f_pid", Input).value.strip() or None
        if root == "sandbox" and not pkg:
            app.call_from_thread(app.notify, "indica un paquete", severity="warning")
            return
        if root == "proc" and not pid:
            app.call_from_thread(app.notify, "indica un pid", severity="warning")
            return
        ok, msg = filesmod.check_access(dev.serial, root, pkg, pid)
        if not ok:
            app.call_from_thread(app.notify, f"acceso denegado: {msg[:120]}", severity="error", timeout=8)
            app.call_from_thread(self._apply_entries, [], False)
            return
        rel = self._rel()
        try:
            entries = filesmod.list_dir(dev.serial, root, rel, pkg, pid)
        except Exception as e:
            app.call_from_thread(app.notify, f"files: {e}", severity="error")
            return
        truncated = len(entries) > 2000
        if truncated:
            entries = entries[:2000]
        app.call_from_thread(self._apply_entries, entries, truncated)

    def _apply_entries(self, entries: List, truncated: bool) -> None:
        self.entries = {}
        t = self.query_one("#f_list", DataTable)
        t.clear()
        if self.rel_segments:
            t.add_row("", "..", "", "", "", key="..")
        errors = []
        for e in entries:
            if e.kind == "error":
                errors.append(e.message or "error")
                continue
            glyph = self.GLYPH.get(e.kind, "⛔")
            key = e.path or e.name
            self.entries[key] = e
            t.add_row(glyph, e.name, human_size(e.size) if e.kind == "file" else "", e.mtime, e.perm, key=key)
        self._update_breadcrumb()
        hint = f"[dim]{len(self.entries)} elemento(s)"
        if truncated:
            hint += " (truncado a 2000)"
        hint += " · Enter: abrir/ver · backspace: subir · p: pull · v: ver · x: borrar · b: abrir como DB · r: recargar[/]"
        self.query_one("#f_hint", Static).update(hint)
        if errors:
            self.app.notify(errors[0][:160], severity="error")

    @on(DataTable.RowSelected, "#f_list")
    def _row_selected(self, ev: DataTable.RowSelected) -> None:
        key = ev.row_key.value if ev.row_key else None
        if key:
            self._enter(key)
        ev.stop()

    def _enter(self, key: str) -> None:
        if key == "..":
            self.action_up()
            return
        entry = self.entries.get(key)
        if entry is None:
            return
        if entry.kind in ("dir", "link"):
            self.rel_segments.append(entry.name)
            self._load()
        elif entry.kind == "file":
            self._preview_entry(entry)

    def _selected_entry(self):
        t = self.query_one("#f_list", DataTable)
        if t.row_count == 0 or t.cursor_row is None:
            return None
        try:
            key = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        except Exception:
            return None
        if key is None or key == "..":
            return None
        return self.entries.get(key)

    def action_preview(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            self.app.notify("Selecciona un archivo", severity="warning")
            return
        if entry.kind != "file":
            self.app.notify("Solo se puede ver un archivo", severity="warning")
            return
        self._preview_entry(entry)

    @work(thread=True, exclusive=True, group="files_preview")
    def _preview_entry(self, entry) -> None:
        from . import files as filesmod
        app = self.app
        dev = app.current
        if not dev:
            return
        pkg = self.query_one("#f_pkg", Input).value.strip() or None
        pid = self.query_one("#f_pid", Input).value.strip() or None
        rel = f"{self._rel()}/{entry.name}" if self._rel() else entry.name
        try:
            text, truncated = filesmod.preview(dev.serial, self.root, rel, pkg)
        except Exception as e:
            app.call_from_thread(app.notify, f"preview: {e}", severity="error")
            return
        if filesmod.is_text_name(entry.name):
            title = entry.path or entry.name
            if truncated:
                title += " (truncado)"
            app.call_from_thread(self._show_preview, text, title)
        else:
            hexdump = " ".join(f"{ord(c) & 0xff:02x}" for c in text[:512])
            note = f"archivo binario, primeros 512 bytes en hex:\n\n{hexdump}"
            app.call_from_thread(self._show_preview, note, f"{entry.name} (binario)")

    def _show_preview(self, text: str, title: str) -> None:
        from .tui import ReviewScreen
        self.app.push_screen(ReviewScreen(text, title=title, prefix="files"))

    def action_pull(self) -> None:
        app = self.app
        dev = app.current
        if not dev:
            app.notify("No hay dispositivo actual", severity="warning")
            return
        file_entries = [e for e in self.entries.values() if e.kind == "file"]
        if not file_entries:
            app.notify("No hay archivos que descargar en este listado", severity="warning")
            return
        self._do_pull(dev, file_entries)

    @work(thread=True, exclusive=True, group="files_pull")
    def _do_pull(self, dev, file_entries) -> None:
        from . import files as filesmod
        app = self.app
        pkg = self.query_one("#f_pkg", Input).value.strip() or None
        rel = self._rel()
        dest_dir = config.DROID_HOME / "pull" / dev.key / self.root / rel
        try:
            pulled = filesmod.pull_path(dev.serial, self.root, rel, pkg, dest_dir, entries=file_entries)
        except Exception as e:
            app.call_from_thread(app.notify, f"pull: {e}", severity="error", timeout=8)
            return
        self.last_pulled = dest_dir
        app.call_from_thread(app.notify, f"{len(pulled)} archivo(s) en {dest_dir} · o: abrir", timeout=8)

    def action_open_pulled(self) -> None:
        from . import clip
        if not self.last_pulled:
            self.app.notify("Todavía no has hecho pull de nada", severity="warning")
            return
        clip.open_file(self.last_pulled)

    def action_open_db(self) -> None:
        entry = self._selected_entry()
        if entry is None or not entry.name.lower().endswith(".db"):
            self.app.notify("Selecciona un archivo .db", severity="warning")
            return
        pkg = self.query_one("#f_pkg", Input).value.strip()
        if not pkg:
            self.app.notify("Falta el paquete", severity="warning")
            return
        remote = f"{self._rel()}/{entry.name}" if self._rel() else entry.name
        app = self.app
        app.set_inspect_package(pkg)
        app.action_tab("db")
        from .tui import DbPane
        db_pane = app.query_one("#db_pane", DbPane)
        db_pane.query_one("#d_pkg", Input).value = pkg
        db_pane._pull(remote)

    def action_delete(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            self.app.notify("Selecciona un archivo o directorio", severity="warning")
            return
        from .tui import Confirm
        is_dir = entry.kind == "dir"
        path = entry.path or entry.name

        def _after_first(yes: bool) -> None:
            if not yes:
                return
            if is_dir:
                self.app.push_screen(Confirm("Es un directorio: se borra todo lo que contiene. ¿Seguro?"), _after_second)
            else:
                self._do_delete(entry, is_dir)

        def _after_second(yes: bool) -> None:
            if yes:
                self._do_delete(entry, is_dir)

        self.app.push_screen(Confirm(f"¿Borrar {path}?"), _after_first)

    @work(thread=True, exclusive=True, group="files_delete")
    def _do_delete(self, entry, is_dir: bool) -> None:
        from . import files as filesmod
        app = self.app
        dev = app.current
        if not dev:
            return
        pkg = self.query_one("#f_pkg", Input).value.strip() or None
        rel = f"{self._rel()}/{entry.name}" if self._rel() else entry.name
        try:
            ok, msg = filesmod.delete_path(dev.serial, self.root, rel, pkg, is_dir, confirmed=True)
        except Exception as e:
            app.call_from_thread(app.notify, f"borrar: {e}", severity="error", timeout=8)
            return
        if ok:
            app.call_from_thread(app.notify, f"Borrado {rel}", timeout=5)
            app.call_from_thread(self._load)
        else:
            app.call_from_thread(app.notify, f"No se pudo borrar: {msg[:160]}", severity="error", timeout=8)
