"""App interactiva de terminal (Textual): dispositivos, logs en vivo, caché post-mortem, procesos/hilos, apps y grabación."""
import os
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

from rich.markup import escape
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, OptionList, RichLog, Static, TabbedContent, TabPane, TextArea
from textual.widgets.option_list import Option

from . import adb as adbmod
from . import cache, clip, config, record, registry, theme, wifi
from .adb import Device
from .apps import find_project_apps, scan_projects
from .logs import SEP_RE, Filter, LogcatStream, PidWatcher, Renderer, list_pids, list_processes, list_threads, parse, thread_cpu
from .ui import UserError, human_size
from .tui_panes import ConsolePane, DbPane, InspectPane, NetPane
from .suggest import DroidAutoComplete, Suggest


# ----------------------------------------------------------------------------- helpers

def T(text, color: Optional[int] = None, bold: bool = False, dim: bool = False) -> Text:
    style = []
    if color is not None:
        style.append(f"color({color})")
    if bold:
        style.append("bold")
    if dim:
        style.append("dim")
    return Text(str(text), style=" ".join(style))


def dev_text(dev: Device, with_serial: bool = False) -> Text:
    t = T("● " if dev.online else "○ ", theme.color_for(dev.key))
    t.append_text(T(dev.display, theme.color_for(dev.key), bold=True))
    if dev.alias:
        t.append_text(T(f" {dev.name}", dim=True))
    if with_serial:
        t.append_text(T(f" · {dev.serial}", dim=True))
    return t


def transport_text(transport: str) -> Text:
    label, n = theme.TRANSPORT.get(transport, theme.TRANSPORT["?"])
    return T(label, n, bold=(transport == "usb"))


def state_text(state: str) -> Text:
    label, n = theme.STATE.get(state, (state, theme.MUTED))
    return T(label, n)


# ----------------------------------------------------------------------------- modales

class TextPrompt(ModalScreen[Optional[str]]):
    """Pide un texto. Enter acepta, Escape cancela."""

    DEFAULT_CSS = """
    TextPrompt { align: center middle; }
    TextPrompt > Vertical { width: 70; height: auto; border: round $accent; background: $surface; padding: 1 2; }
    TextPrompt Label { margin-bottom: 1; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancelar")]

    def __init__(self, title: str, placeholder: str = "", value: str = "", password: bool = False, candidates=None):
        super().__init__()
        self._title, self._placeholder, self._value, self._password = title, placeholder, value, password
        self._candidates = candidates

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._title)
            inp = Input(value=self._value, placeholder=self._placeholder, password=self._password, id="prompt")
            yield inp
            if self._candidates is not None:
                yield DroidAutoComplete(inp, provider=self._candidates)
            yield Label("[dim]Enter acepta · Esc cancela · ↑↓ elige sugerencia · Tab completa[/]")

    def on_mount(self) -> None:
        self.query_one("#prompt", Input).focus()

    @on(Input.Submitted)
    def _submit(self, ev: Input.Submitted) -> None:
        self.dismiss(ev.value.strip())

    def action_cancel(self) -> None:
        self.dismiss(None)


class Confirm(ModalScreen[bool]):
    DEFAULT_CSS = """
    Confirm { align: center middle; }
    Confirm > Vertical { width: 70; height: auto; border: round $warning; background: $surface; padding: 1 2; }
    Confirm Horizontal { height: auto; align: center middle; }
    Confirm Button { margin: 1 2 0 2; }
    """
    BINDINGS = [Binding("escape", "no", "No"), Binding("n", "no", "No"), Binding("s", "yes", "Sí"), Binding("y", "yes", "Sí")]

    def __init__(self, text: str):
        super().__init__()
        self._text = text

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._text)
            with Horizontal():
                yield Button("Sí (s)", variant="error", id="yes")
                yield Button("No (n)", id="no")

    @on(Button.Pressed)
    def _pressed(self, ev: Button.Pressed) -> None:
        self.dismiss(ev.button.id == "yes")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class Choose(ModalScreen[Optional[int]]):
    """Lista de opciones; devuelve el índice elegido o None."""

    DEFAULT_CSS = """
    Choose { align: center middle; }
    Choose > Vertical { width: 80; height: auto; max-height: 80%; border: round $accent; background: $surface; padding: 1 2; }
    Choose OptionList { height: auto; max-height: 20; }
    """
    BINDINGS = [Binding("escape", "cancel", "Cancelar")]

    def __init__(self, title: str, options: List[Text]):
        super().__init__()
        self._title = title
        self._options = options

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._title)
            yield OptionList(*[Option(o, id=str(i)) for i, o in enumerate(self._options)], id="opts")
            yield Label("[dim]Enter elige · Esc cancela[/]")

    def on_mount(self) -> None:
        self.query_one("#opts", OptionList).focus()

    @on(OptionList.OptionSelected)
    def _selected(self, ev: OptionList.OptionSelected) -> None:
        self.dismiss(int(ev.option.id))

    def action_cancel(self) -> None:
        self.dismiss(None)


class ReviewScreen(ModalScreen[None]):
    """Visor para leer, seleccionar y copiar una sección de texto (logs, tablas, salida de comandos).

    El texto está congelado: puedes moverte, seleccionar con shift+flechas o con el ratón, y copiarlo fuera."""

    DEFAULT_CSS = """
    ReviewScreen { align: center middle; }
    ReviewScreen > Vertical { width: 92%; height: 90%; border: round $accent; background: $surface; padding: 0 1; }
    ReviewScreen #rv_title { height: 1; color: $text-muted; }
    ReviewScreen #rv_text { height: 1fr; border: none; }
    ReviewScreen #rv_hint { height: 1; color: $text-muted; }
    """
    BINDINGS = [
        Binding("escape", "close", "Cerrar"),
        Binding("q", "close", "Cerrar", show=False),
        Binding("c", "copy", "Copiar"),
        Binding("a", "select_all", "Seleccionar todo"),
        Binding("g", "save", "Guardar"),
        Binding("e", "editor", "Abrir en editor"),
        Binding("w", "wrap", "Ajuste de línea"),
    ]

    def __init__(self, text: str, title: str = "", prefix: str = "clip"):
        super().__init__()
        self._text = text
        self._title = title
        self._prefix = prefix
        self._saved: Optional[Path] = None

    def compose(self) -> ComposeResult:
        n = len(self._text.splitlines())
        with Vertical():
            yield Static(f"[bold]{escape(self._title)}[/] [dim]· {n} líneas · {len(self._text)} caracteres[/]", id="rv_title")
            yield TextArea(self._text, id="rv_text", read_only=True, soft_wrap=True, show_line_numbers=True)
            yield Static("[dim]ratón o shift+flechas selecciona · c copiar (selección o todo) · a todo · g guardar archivo · e abrir en editor · w ajuste · Esc cerrar[/]", id="rv_hint")

    def on_mount(self) -> None:
        ta = self.query_one("#rv_text", TextArea)
        ta.focus()
        ta.move_cursor(ta.document.end)

    def _payload(self) -> str:
        ta = self.query_one("#rv_text", TextArea)
        sel = getattr(ta, "selected_text", "") or ""
        return sel if sel.strip() else self._text

    def action_copy(self) -> None:
        self.app.copy_to_clipboard(self._payload())

    def action_select_all(self) -> None:
        self.query_one("#rv_text", TextArea).select_all()

    def action_save(self) -> None:
        self._saved = clip.save_clip(self._payload(), self._prefix)
        self.app.notify(f"Guardado en {self._saved}", timeout=6)

    def action_editor(self) -> None:
        path = self._saved or clip.save_clip(self._payload(), self._prefix)
        self._saved = path
        if clip.open_file(path):
            self.app.notify(f"Abierto {path.name} en el editor", timeout=4)
        else:
            self.app.notify(f"No pude abrirlo; está en {path}", severity="warning", timeout=6)

    def action_wrap(self) -> None:
        ta = self.query_one("#rv_text", TextArea)
        ta.soft_wrap = not ta.soft_wrap

    def action_close(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    HelpScreen > Vertical { width: 90; height: auto; max-height: 90%; border: round $accent; background: $surface; padding: 1 2; }
    """
    BINDINGS = [Binding("escape", "close", "Cerrar"), Binding("q", "close", "Cerrar"), Binding("question_mark", "close", "Cerrar")]
    TEXT = """[bold]droid[/] — atajos

[bold]Global[/]   1-9,0 pestañas · r/F5 refrescar · R reconectar adb (offline/sin autorizar) · la lista se refresca sola al conectar/desconectar · ctrl+d siguiente dispositivo · esc salir de un campo · q salir
[bold]Dispositivos[/]   Enter ver logs · espacio elegir como actual · w USB→WiFi · c conectar conocido · d desconectar · u volver a USB · a alias
[bold]Copiar[/]   arrastra con el ratón sobre el log para seleccionar y ctrl+c (o cmd+c) copia al portapapeles del Mac.
          [bold]y[/] copia la selección, o lo que se ve en pantalla (en tablas, la fila del cursor) · [bold]Y[/] copia todo el panel/tabla
          [bold]v[/] abre el visor: texto congelado para leer, seleccionar con shift+flechas, c copiar, g guardar archivo, e abrir en editor
          En logs en vivo, v pausa el stream para que puedas recortar tranquilo (p lo reanuda).

[bold]Campos[/]   al entrar en cualquier campo aparece una lista de sugerencias (procesos vivos, apps de tus proyectos, hilos, niveles, tags, tablas…): ↑↓ elige, Tab o Enter completa, Esc oculta. Escribe para filtrar.
[bold]Logs[/]   s iniciar/parar · p pausar · x limpiar · f editar filtros · e solo avisos+errores · t columna de hilo · n columna de proceso
       Filtros: paquete (com.mi.app), nivel (V D I W E F), grep (regex), hilo (main, OkHttp*), pid. Enter en un campo aplica.
[bold]Caché[/]   Enter abrir sesión · f seguir en vivo · x borrar sesión · o abrir carpeta en Finder
[bold]Procesos[/]   t hilos del proceso · l logs de ese proceso · espacio auto-refresco
[bold]Apps[/]   Enter/l logs de esa app · a mostrar también no instaladas
[bold]Grabar[/]   s grabar dispositivo actual · S grabar todos · x parar seleccionada · X parar todas
[bold]Inspector[/] (7)  s iniciar/parar · l logs del hilo · m meminfo ahora · e salidas de la app · f cambiar app   (desde Apps: i)
[bold]DB[/] (8)  Enter en DB: snapshot · Enter en tabla: filas · / SQL · x CSV · p shared_prefs   (desde Apps: d)
[bold]Red[/] (9)  s iniciar/parar · r sockets · f cambiar app
[bold]Consola[/] (0)  área de bloque: pega uno o varios comandos y ctrl+r (o ▶). Si no empiezan por `adb` van enteros a `adb shell` del
          dispositivo actual; si empiezan por `adb` corren en el Mac con adb ya apuntando al dispositivo (`adb shell getprop | grep …`).
          Línea rápida arriba (f): Enter ejecuta, con sugerencias · ctrl+g corta · ctrl+l limpia salida · b vuelve al bloque · ctrl+d cambia dispositivo

[bold]Colores[/]   cada dispositivo, proceso (PID), hilo y tag tiene su color fijo en toda la app.
          Niveles: {lv} · [{exc} bold]Excepciones[/] · [{cfg} on {cbg} bold] FATAL / ANR / died [/] · [{url} underline]URLs[/]
""".format(lv=" ".join(theme.level_markup(l) for l in "VDIWEF"), exc=theme.hx(theme.EXC_FG), cfg=theme.hx(theme.CRASH_FG), cbg=theme.hx(theme.CRASH_BG), url=theme.hx(theme.URL_FG))

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self.TEXT + "\n[dim]────────────────────────────────[/]\n" + INSPECT_TEXT)

    def action_close(self) -> None:
        self.dismiss(None)


# ----------------------------------------------------------------------------- stream de logs en vivo

class LiveLogs:
    """Hilo que lee logcat, aplica filtros y encola líneas ANSI para que la UI las vuelque por lotes."""

    def __init__(self, dev: Device, filt: Filter, renderer: Renderer, tail: Optional[int] = None, use_cache: bool = True,
                 since: Optional[str] = None):
        self.dev = dev
        self.filt = filt
        self.renderer = renderer
        self.tail = tail
        self.since = since
        self.use_cache = use_cache
        self.queue: deque = deque(maxlen=50000)
        self.plain: deque = deque(maxlen=50000)   # mismas líneas sin color ni marco, para copiar/revisar
        self.running = False
        self.paused = False
        self.shown = 0
        self.total = 0
        self.stream: Optional[LogcatStream] = None
        self.session: Optional[cache.Session] = None
        self.watcher: Optional[PidWatcher] = None
        self.thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _banner(self, text: str, kind: str = "info") -> None:
        self.queue.append(self.renderer.banner(text, kind))
        self.plain.append(f"# droid: {text}")

    def _run(self) -> None:
        r = self.renderer
        try:
            if self.use_cache and not record.status_of(self.dev.key):
                self.session = cache.Session(self.dev, "ui", self.filt.describe())
            stream = LogcatStream(self.dev.serial, self.dev.key, tail=self.tail, reconnect=bool(config.get("reconnect")),
                                  status=lambda k, t: self._banner(t, k), since=self.since)
            self.stream = stream

            def _sync() -> None:
                if self.session:
                    self.session.set_pids(self.filt.pid_names)
                    self.session.set_threads(self.filt.tid_names)
                    self.session.set_procs(self.filt.proc_names)

            need_watch = self.filt.packages or self.filt.wants_threads or r.show_thread or r.show_proc
            if need_watch:
                self.watcher = PidWatcher(lambda: stream.serial, self.filt, lambda t: (self._banner(t, "info"), _sync()),
                                          want_thread_names=r.show_thread, want_proc_names=r.show_proc, on_refresh=_sync)
                self.watcher.tick()
                self.watcher.start()
            self._banner(f"{self.dev.label()} · {self._filters_text()}" + (f" · caché → {self.session.log_path.name}" if self.session else " · sin caché"), "ok")
            for kind, payload in stream.stream():
                if not self.running:
                    break
                if kind == "line":
                    self.total += 1
                    if self.session:
                        self.session.write(payload)
                    ll = parse(payload)
                    if ll is None:
                        if not self.filt.packages and SEP_RE.match(payload):
                            self.queue.append(r.separator(payload))
                            self.plain.append(payload)
                        continue
                    ev = self.filt.observe(ll)
                    if ev:
                        self._banner(ev, "warn" if ("muri" in ev or "termin" in ev) else "ok")
                        _sync()
                    if self.filt.matches(ll):
                        self.shown += 1
                        self.queue.append(r.line(ll))
                        self.plain.append(payload)
                else:
                    self._banner(payload, {"lost": "err", "reconnected": "ok", "end": "warn"}.get(kind, "info"))
                    if self.session:
                        self.session.note(payload)
        except Exception as e:  # nunca tumbar la UI por el stream
            self._banner(f"error en el stream: {e}", "err")
        finally:
            if self.watcher:
                self.watcher.stop()
            if self.stream:
                self.stream.close()
            if self.session:
                self.session.close()
                self._banner(f"caché: {self.session.lines} líneas → {self.session.log_path}", "info")
            self.running = False
            self._banner("stream detenido", "warn")

    def _filters_text(self) -> str:
        d = self.filt.describe()
        bits = []
        if d["packages"]:
            bits.append("pkg=" + ",".join(d["packages"]))
        if d.get("pids"):
            bits.append("pid=" + ",".join(map(str, d["pids"])))
        if d["min_level"] != "V":
            bits.append("nivel≥" + d["min_level"])
        if d["grep"]:
            bits.append(f"grep=/{d['grep']}/")
        if d.get("threads"):
            bits.append("hilo=" + ",".join(d["threads"]))
        return " ".join(bits) if bits else "sin filtros"

    def stop(self) -> None:
        self.running = False
        if self.stream:
            self.stream.close()
        if self.watcher:
            self.watcher.stop()


# ----------------------------------------------------------------------------- la app

INSPECT_TEXT = """[bold]droid inspect[/] — App inspector / build analyzer  [dim](en planificación)[/]

Plan completo: [bold]docs/ROADMAP-inspector.md[/] en el repo droid-cli.

  [bold {ok}]Fase 0[/]  Identificación de apps de tus proyectos  [{ok}]✔ hecho[/]  → pestaña [bold]5 Apps[/]
  [bold {warn}]Filtros[/] procesos e hilos con color de identidad  [{ok}]✔ hecho[/]  → pestañas [bold]2 Logs[/] y [bold]4 Procesos[/]
  [bold]Fase 1[/]  CPU por hilo · PSS/heap · frames y jank (dumpsys gfxinfo) · GC · ANR/crash en la línea temporal
  [bold]Fase 2[/]  Red: rx/tx por segundo, sockets abiertos, peticiones HTTP (proxy o agente debug)
  [bold]Fase 3[/]  Base de datos: run-as + sqlite3, snapshots y diff, DataStore/SharedPreferences
  [bold]Fase 4[/]  Build analyzer: apkanalyzer, comparar APK instalado vs compilado, retrace con mapping.txt
  [bold]Fase 5[/]  simpleperf / perfetto integrados con la caché de logs

Solo apps [bold]debuggables[/] cuyo applicationId sale de un módulo Android en tu carpeta de proyectos
([dim]droid config projects_dir[/]).
""".format(ok=theme.hx(theme.OK), warn=theme.hx(theme.WARN))


class DroidApp(App):
    TITLE = "droid"
    CSS = """
    Screen { layout: vertical; }
    #devbar { height: 1; padding: 0 1; background: $panel; }
    TabbedContent { height: 1fr; }
    TabPane { padding: 0; }
    DataTable { height: 1fr; }
    .bar { height: 3; }
    .bar Input { width: 1fr; }
    .bar Input.narrow { width: 12; }
    .bar Input.mid { width: 24; }
    RichLog { height: 1fr; border: round $primary; }
    .hint { color: $text-muted; height: 1; padding: 0 1; }
    #inspect { padding: 1 2; }
    #cache_table { height: 2fr; min-height: 5; }
    #cache_view { height: 3fr; min-height: 8; }
    """
    BINDINGS = [
        Binding("q", "quit", "Salir"),
        Binding("question_mark", "help", "Ayuda", key_display="?"),
        Binding("1", "tab('devices')", "Dispositivos", show=False),
        Binding("2", "tab('logs')", "Logs", show=False),
        Binding("3", "tab('cache')", "Caché", show=False),
        Binding("4", "tab('procs')", "Procesos", show=False),
        Binding("5", "tab('apps')", "Apps", show=False),
        Binding("6", "tab('record')", "Grabar", show=False),
        Binding("7", "tab('inspect')", "Inspector", show=False),
        Binding("8", "tab('db')", "DB", show=False),
        Binding("9", "tab('net')", "Red", show=False),
        Binding("0", "tab('console')", "Consola", show=False),
        Binding("y", "copy", "Copiar"),
        Binding("Y", "copy_all", "Copiar todo", show=False),
        Binding("v", "review", "Revisar/copiar"),
        Binding("r", "refresh", "Refrescar"),
        Binding("f5", "refresh", "Refrescar", show=False),
        Binding("R", "hard_refresh", "Reconectar adb"),
        Binding("ctrl+d", "next_device", "Disp. →"),
        Binding("escape", "unfocus", "Salir del campo", show=False),
        # dispositivos
        Binding("space", "dev_select", "Elegir", show=True),
        Binding("w", "dev_wifi", "USB→WiFi"),
        Binding("c", "dev_connect", "Conectar"),
        Binding("d", "dev_disconnect", "Desconectar"),
        Binding("u", "dev_usb", "→USB"),
        Binding("a", "dev_alias", "Alias"),
        # logs
        Binding("s", "logs_toggle", "Iniciar/Parar"),
        Binding("p", "logs_pause", "Pausar"),
        Binding("x", "logs_clear", "Limpiar"),
        Binding("f", "logs_filters", "Filtros"),
        Binding("e", "logs_errors", "Solo W+"),
        Binding("t", "logs_thread", "Hilo"),
        Binding("n", "logs_proc", "Proceso"),
        # caché
        Binding("f", "cache_follow", "Seguir"),
        Binding("x", "cache_delete", "Borrar"),
        Binding("o", "cache_open", "Finder"),
        # procesos
        Binding("t", "procs_threads", "Hilos"),
        Binding("l", "procs_logs", "Logs del proceso"),
        Binding("space", "procs_auto", "Auto"),
        # apps
        Binding("l", "apps_logs", "Logs de la app"),
        Binding("i", "apps_inspect", "Inspeccionar"),
        Binding("d", "apps_db", "DB"),
        Binding("a", "apps_all", "Todas"),
        # grabación
        Binding("s", "rec_start", "Grabar actual"),
        Binding("S", "rec_start_all", "Grabar todos"),
        Binding("x", "rec_stop", "Parar"),
        Binding("X", "rec_stop_all", "Parar todas"),
    ]
    TAB_ACTIONS = {
        "devices": {"dev_select", "dev_wifi", "dev_connect", "dev_disconnect", "dev_usb", "dev_alias"},
        "logs": {"logs_toggle", "logs_pause", "logs_clear", "logs_filters", "logs_errors", "logs_thread", "logs_proc"},
        "cache": {"cache_follow", "cache_delete", "cache_open"},
        "procs": {"procs_threads", "procs_logs", "procs_auto"},
        "apps": {"apps_logs", "apps_all", "apps_inspect", "apps_db"},
        "record": {"rec_start", "rec_start_all", "rec_stop", "rec_stop_all"},
        "inspect": set(), "db": set(), "net": set(), "console": set(),
    }
    ALL_TAB_ACTIONS = set().union(*TAB_ACTIONS.values())

    def __init__(self, initial_device: Optional[str] = None):
        super().__init__()
        self.devices: List[Device] = []
        self.current: Optional[Device] = None
        self.initial_device = initial_device
        self.live: Optional[LiveLogs] = None
        self.cache_sessions: List[cache.SessionInfo] = []
        self.cache_current: Optional[cache.SessionInfo] = None
        self.cache_follow = False
        self.cache_fh = None
        self.cache_filter: Optional[Filter] = None
        self.cache_renderer: Optional[Renderer] = None
        self.cache_plain: deque = deque(maxlen=60000)
        self.procs_mode = "procs"          # procs | threads
        self.procs_pid: Optional[int] = None
        self.procs_auto = False
        self.apps_all = False
        self._projects = None
        self._rec_rows: List[dict] = []
        self.suggest = Suggest(self)

    # ---------------------------------------------------------------- layout
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("", id="devbar")
        with TabbedContent(initial="devices"):
            with TabPane("1 Dispositivos", id="devices"):
                yield DataTable(id="dev_table", cursor_type="row", zebra_stripes=True)
                yield Static("", id="dev_hint", classes="hint")
            with TabPane("2 Logs", id="logs"):
                with Horizontal(classes="bar"):
                    f_pkg = Input(placeholder="paquete (com.mi.app)", id="f_pkg"); yield f_pkg
                    yield DroidAutoComplete(f_pkg, provider=lambda: self.suggest.packages("pkg"), multi=True)
                    f_level = Input(placeholder="nivel V", id="f_level", classes="narrow"); yield f_level
                    yield DroidAutoComplete(f_level, provider=self.suggest.levels)
                    f_grep = Input(placeholder="grep (regex)", id="f_grep"); yield f_grep
                    yield DroidAutoComplete(f_grep, provider=lambda: self.suggest.greps("grep"))
                    f_thread = Input(placeholder="hilo (main, OkHttp*)", id="f_thread", classes="mid"); yield f_thread
                    yield DroidAutoComplete(f_thread, provider=lambda: self.suggest.threads(f_pkg.value, f_pid.value), multi=True)
                    f_pid = Input(placeholder="pid", id="f_pid", classes="narrow"); yield f_pid
                    yield DroidAutoComplete(f_pid, provider=self.suggest.pids, multi=True)
                yield RichLog(id="log", highlight=False, markup=False, wrap=False, max_lines=8000)
                yield Static("", id="log_status", classes="hint")
            with TabPane("3 Caché", id="cache"):
                yield DataTable(id="cache_table", cursor_type="row", zebra_stripes=True)
                with Horizontal(classes="bar"):
                    c_pkg = Input(placeholder="paquete", id="c_pkg"); yield c_pkg
                    yield DroidAutoComplete(c_pkg, provider=lambda: self.suggest.packages("pkg"), multi=True)
                    c_level = Input(placeholder="nivel V", id="c_level", classes="narrow"); yield c_level
                    yield DroidAutoComplete(c_level, provider=self.suggest.levels)
                    c_grep = Input(placeholder="grep (regex)", id="c_grep"); yield c_grep
                    yield DroidAutoComplete(c_grep, provider=lambda: self.suggest.greps("grep"))
                    c_tail = Input(placeholder="últimas N líneas (3000)", id="c_tail", classes="mid"); yield c_tail
                    yield DroidAutoComplete(c_tail, provider=self.suggest.tails)
                yield RichLog(id="cache_view", highlight=False, markup=False, wrap=False, max_lines=20000)
                yield Static("", id="cache_hint", classes="hint")
            with TabPane("4 Procesos", id="procs"):
                yield DataTable(id="procs_table", cursor_type="row", zebra_stripes=True)
                yield Static("", id="procs_hint", classes="hint")
            with TabPane("5 Apps", id="apps"):
                yield DataTable(id="apps_table", cursor_type="row", zebra_stripes=True)
                yield Static("", id="apps_hint", classes="hint")
            with TabPane("6 Grabar", id="record"):
                yield DataTable(id="rec_table", cursor_type="row", zebra_stripes=True)
                yield Static("", id="rec_hint", classes="hint")
            with TabPane("7 Inspector", id="inspect"):
                yield InspectPane(id="inspect_pane")
            with TabPane("8 DB", id="db"):
                yield DbPane(id="db_pane")
            with TabPane("9 Red", id="net"):
                yield NetPane(id="net_pane")
            with TabPane("0 Consola", id="console"):
                yield ConsolePane(id="console_pane")
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#dev_table", DataTable)
        t.add_columns("", "Dispositivo", "Serial", "Vía", "Estado", "Android", "IP", "Bat")
        c = self.query_one("#cache_table", DataTable)
        c.add_columns("Dispositivo", "Sesión", "Inicio", "Duración", "Líneas", "Tamaño", "Origen", "Filtros", "Estado")
        self.query_one("#procs_table", DataTable).add_columns("PID", "Usuario", "%CPU", "RSS", "Proceso")
        self.query_one("#apps_table", DataTable).add_columns("Proyecto", "Módulo", "Package", "Versión", "Debug", "Proceso", "Actualizada")
        self.query_one("#rec_table", DataTable).add_columns("Dispositivo", "Serial", "PID", "Desde", "Líneas", "Tamaño", "Sesión")
        self.query_one("#dev_hint", Static).update("[dim]Enter: logs · espacio: elegir actual · w: USB→WiFi · c: conectar · d: desconectar · a: alias · r: refrescar · ?: ayuda[/]")
        self.query_one("#log_status", Static).update("[dim]s: iniciar · f: filtros · e: solo W+ · t: hilo · n: proceso · p: pausa · x: limpiar[/]")
        self.query_one("#cache_hint", Static).update("[dim]Enter: abrir sesión · f: seguir · x: borrar · o: Finder[/]")
        self.query_one("#procs_hint", Static).update("[dim]t: hilos · l: logs del proceso · espacio: auto-refresco[/]")
        self.query_one("#apps_hint", Static).update("[dim]Enter/l: logs de la app · i: inspeccionar (CPU/RAM/frames) · d: base de datos · a: incluir no instaladas[/]")
        self.query_one("#rec_hint", Static).update("[dim]s: grabar actual · S: grabar todos · x: parar · X: parar todas[/]")
        self.set_interval(0.1, self._drain_logs)
        self.set_interval(2.0, self._tick_periodic)
        self.refresh_devices()
        self._track_state = None
        self._tracker = adbmod.DeviceTracker(lambda devs: self.call_from_thread(self._on_track_change, devs))
        self._tracker.start()

    # ---------------------------------------------------------------- utilidades
    @property
    def active_tab(self) -> str:
        return self.query_one(TabbedContent).active

    def check_action(self, action: str, parameters) -> Optional[bool]:
        if action in self.ALL_TAB_ACTIONS:
            return True if action in self.TAB_ACTIONS.get(self.active_tab, set()) else None
        return True

    def action_tab(self, name: str) -> None:
        self.query_one(TabbedContent).active = name
        self._on_tab_shown(name)

    @on(TabbedContent.TabActivated)
    def _tab_changed(self, ev: TabbedContent.TabActivated) -> None:
        self._on_tab_shown(ev.pane.id)

    def _on_tab_shown(self, name: str) -> None:
        if name == "cache":
            self.refresh_cache()
            self.query_one("#cache_table", DataTable).focus()
        elif name == "procs":
            self.refresh_procs()
            self.query_one("#procs_table", DataTable).focus()
        elif name == "apps":
            self.refresh_apps()
            self.query_one("#apps_table", DataTable).focus()
        elif name == "record":
            self.refresh_record()
            self.query_one("#rec_table", DataTable).focus()
        elif name == "devices":
            self.query_one("#dev_table", DataTable).focus()
        elif name == "logs":
            self.query_one("#log", RichLog).focus()
            self._fit_renderer()
        elif name == "inspect":
            self.query_one("#i_threads", DataTable).focus()
        elif name == "db":
            self.query_one("#db_list", DataTable).focus()
        elif name == "net":
            self.query_one("#n_sockets", DataTable).focus()
        elif name == "console":
            self.query_one("#sh_code").focus()
            self.query_one("#console_pane", ConsolePane)._update_hint()

    def action_unfocus(self) -> None:
        tab = self.active_tab
        target = {"devices": "#dev_table", "logs": "#log", "cache": "#cache_table", "procs": "#procs_table", "apps": "#apps_table", "record": "#rec_table",
                  "inspect": "#i_threads", "db": "#db_list", "net": "#n_sockets", "console": "#sh_out"}.get(tab)
        if target:
            self.query_one(target).focus()

    def copy_to_clipboard(self, text: str) -> None:
        """Copia al portapapeles real del Mac (pbcopy) y además por OSC 52, para que funcione también por SSH."""
        if not text:
            self.notify("No hay nada que copiar", severity="warning", timeout=2)
            return
        super().copy_to_clipboard(text)
        ok, backend = clip.copy(text)
        n = len(text.splitlines())
        if ok:
            self.notify(f"Copiado: {n} línea(s), {len(text)} caracteres", timeout=3)
        else:
            self.notify(f"Sin portapapeles del sistema ({backend}); probé la vía del terminal (OSC 52). Usa v → g para guardar a archivo.",
                        severity="warning", timeout=6)

    # --- seleccionar / copiar / revisar ---
    TEXT_WIDGETS = {"logs": ["#log"], "cache": ["#cache_view"], "console": ["#sh_out"], "devices": ["#dev_table"],
                    "procs": ["#procs_table"], "apps": ["#apps_table"], "record": ["#rec_table"],
                    "inspect": ["#i_threads"], "db": ["#db_rows", "#db_tables", "#db_list"], "net": ["#n_sockets"]}

    def _text_widget(self):
        """Widget de la pestaña activa del que se copia (el primero con contenido)."""
        first = None
        for sel in self.TEXT_WIDGETS.get(self.active_tab, []):
            try:
                w = self.query_one(sel)
            except Exception:
                continue
            first = first or w
            if isinstance(w, DataTable) and w.row_count:
                return w
            if isinstance(w, RichLog) and w.lines:
                return w
        return first

    def _source_name(self) -> str:
        dev = self.current.display if self.current else "droid"
        return {"logs": f"logs-{dev}", "cache": "cache", "console": "consola", "devices": "dispositivos", "procs": "procesos",
                "apps": "apps", "record": "grabaciones", "inspect": "hilos", "db": "db", "net": "sockets"}.get(self.active_tab, "droid")

    def _plain_buffer(self) -> Optional[List[str]]:
        """Texto limpio (líneas crudas de logcat / salida sin formato) del panel activo, si lo hay."""
        tab = self.active_tab
        if tab == "logs" and self.live is not None and self.live.plain:
            return list(self.live.plain)
        if tab == "cache" and self.cache_plain:
            return list(self.cache_plain)
        if tab == "console":
            try:
                pane = self.query_one("#console_pane", ConsolePane)
                if pane.plain:
                    return list(pane.plain)
            except Exception:
                pass
        return None

    def _grab(self, whole: bool) -> str:
        plain = self._plain_buffer()
        if plain is not None:
            if whole:
                return "\n".join(plain)
            try:
                n = max(10, int(self.query_one(self.TEXT_WIDGETS[self.active_tab][0]).size.height))
            except Exception:
                n = 40
            return "\n".join(plain[-n:])
        w = self._text_widget()
        if w is None:
            return ""
        if isinstance(w, DataTable):
            return "\n".join(clip.table_lines(w, only_row=not whole))
        if isinstance(w, RichLog):
            return "\n".join(clip.richlog_lines(w, visible_only=not whole))
        return ""

    def action_copy(self) -> None:
        """Copia la selección; si no hay, lo que se ve en pantalla (o la fila del cursor en una tabla)."""
        sel = self.screen.get_selected_text()
        if sel and sel.strip():
            self.copy_to_clipboard(clip.unwrap_gutter(sel))
            return
        text = self._grab(whole=False)
        if not text.strip():
            self.notify("Nada que copiar en esta pestaña", severity="warning", timeout=3)
            return
        self.copy_to_clipboard(text)

    def action_copy_all(self) -> None:
        text = self._grab(whole=True)
        if not text.strip():
            self.notify("Nada que copiar en esta pestaña", severity="warning", timeout=3)
            return
        self.copy_to_clipboard(text)

    def action_review(self) -> None:
        """Abre el visor con la selección (o todo el contenido) para leerlo, recortarlo y copiarlo fuera."""
        sel = self.screen.get_selected_text()
        text = clip.unwrap_gutter(sel) if (sel and sel.strip()) else self._grab(whole=True)
        if not text.strip():
            self.notify("Nada que revisar en esta pestaña", severity="warning", timeout=3)
            return
        lines = text.splitlines()
        extra = ""
        if len(lines) > 8000:
            lines = lines[-8000:]
            extra = " (últimas 8000 líneas)"
            text = "\n".join(lines)
        name = self._source_name()
        if self.live and self.live.paused is False and self.active_tab == "logs":
            self.live.paused = True
            extra += " · stream en pausa (p reanuda)"
        self.push_screen(ReviewScreen(text, title=f"{name}{extra}", prefix=adbmod.sanitize(name)))

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_refresh(self) -> None:
        self.refresh_devices()
        self._on_tab_shown(self.active_tab)
        self.notify("Refrescando…", timeout=1.5)

    @work(thread=True, exclusive=True, group="hard_refresh")
    def action_hard_refresh(self) -> None:
        """adb reconnect offline + relistar (para dispositivos atascados en offline/sin autorizar)."""
        self.call_from_thread(self.notify, "adb reconnect offline…", timeout=2)
        try:
            adbmod.reconnect("offline")
        except Exception as e:
            self.call_from_thread(self.notify, f"reconnect: {e}", severity="error")
        time.sleep(1.5)
        self.call_from_thread(self.refresh_devices)

    def _on_track_change(self, devs: List[Device]) -> None:
        """adb track-devices avisó de un cambio: notifica qué entró/salió y refresca."""
        now = {d.serial: d.state for d in devs}
        prev = self._track_state
        self._track_state = now
        if prev is None:
            return
        for serial, state in now.items():
            if serial not in prev:
                self.notify(f"Conectado: {serial} ({state})", timeout=4)
            elif prev[serial] != state:
                self.notify(f"{serial}: {prev[serial]} → {state}", timeout=4, severity="warning" if state != "device" else "information")
        for serial in prev:
            if serial not in now:
                self.notify(f"Desconectado: {serial}", timeout=4, severity="warning")
        self.set_timer(0.7, self.refresh_devices)

    def action_next_device(self) -> None:
        phys = adbmod.dedupe([d for d in self.devices if d.online])
        if not phys:
            return
        keys = [d.key for d in phys]
        idx = (keys.index(self.current.key) + 1) % len(keys) if self.current and self.current.key in keys else 0
        self.set_current(phys[idx])

    def set_current(self, dev: Device) -> None:
        self.current = dev
        self.suggest.invalidate()
        self.suggest.prewarm()
        try:
            self.query_one("#console_pane", ConsolePane)._update_hint()
        except Exception:
            pass
        self.sub_title = f"{dev.display} · {dev.serial} · {theme.TRANSPORT.get(dev.transport, ('?', 0))[0]}"
        self._render_devbar()
        self.notify(f"Dispositivo actual: {dev.display}", timeout=2)

    def _render_devbar(self) -> None:
        phys = adbmod.dedupe([d for d in self.devices if d.online])
        parts = []
        for d in phys:
            vias = sorted({x.transport for x in self.devices if x.online and x.key == d.key})
            via = "+".join(theme.TRANSPORT.get(v, ("?", 0))[0] for v in vias)
            mark = "▶ " if self.current and self.current.key == d.key else "  "
            parts.append(f"{mark}{theme.paint('●', theme.color_for(d.key))} {theme.device_markup(d.key, escape(d.display))} [dim]{via}[/] {theme.battery_markup(d.battery)}")
        others = [d for d in self.devices if not d.online]
        if others:
            parts.append(f"[dim]{len(others)} sin conexión útil[/]")
        rec = record.all_status()
        if rec:
            parts.append(theme.paint(f"● grabando {len(rec)}", theme.OK))
        if self.live and self.live.running:
            parts.append(theme.paint("● logs en vivo", theme.INFO))
        parts.append("[dim]⟳ auto · r/F5 refrescar · R reconectar[/]")
        self.query_one("#devbar", Static).update("   ".join(parts) if parts else "[dim]sin dispositivos · conecta uno por USB o pulsa c para conectar por WiFi[/]")

    def _dev_or_warn(self) -> Optional[Device]:
        if not self.current:
            self.notify("Elige primero un dispositivo (pestaña 1, espacio)", severity="warning")
            return None
        return self.current

    def _selected_row_key(self, table_id: str) -> Optional[str]:
        t = self.query_one(table_id, DataTable)
        if t.row_count == 0 or t.cursor_row is None:
            return None
        try:
            return t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        except Exception:
            return None

    # ---------------------------------------------------------------- dispositivos
    @work(thread=True, exclusive=True, group="devices")
    def refresh_devices(self) -> None:
        try:
            devices = adbmod.list_devices()
            registry.touch_all(devices)
        except Exception as e:
            self.call_from_thread(self.notify, f"adb: {e}", severity="error")
            return
        self.call_from_thread(self._apply_devices, devices)

    def _apply_devices(self, devices: List[Device]) -> None:
        self.devices = devices
        t = self.query_one("#dev_table", DataTable)
        t.clear()
        for d in devices:
            android = Text(d.android) if d.android else Text("")
            if d.sdk:
                android.append_text(T(f" (API {d.sdk})", dim=True))
            t.add_row(T("●" if d.online else "○", theme.color_for(d.key) if d.online else theme.MUTED), dev_text(d), d.serial,
                      transport_text(d.transport), state_text(d.state), android,
                      T(d.ip, theme.TRANSPORT["wifi"][1]) if d.ip else "", T(f"{d.battery}%", theme.battery_color(d.battery)) if d.battery else "",
                      key=d.serial)
        known = {k: e for k, e in registry.load().items() if e.get("ip") and k not in {d.key for d in devices if d.online}}
        for k, e in known.items():
            t.add_row(T("○", theme.MUTED), T((e.get("alias") or e.get("model") or k), theme.color_for(k), dim=True), T(e.get("serial", k), dim=True),
                      transport_text("wifi"), T("desconocido / WiFi guardado", dim=True), T(e.get("android", ""), dim=True),
                      T(f"{e.get('ip')}:{e.get('port', '')}", dim=True), "", key=f"known:{k}")
        online = [d for d in devices if d.online]
        if self.current:
            same = [d for d in online if d.key == self.current.key]
            if same:
                pref = adbmod.dedupe(same)[0]
                if pref.serial != self.current.serial:
                    self.current = pref
            else:
                self.current = None
        if self.current is None and online:
            pick = None
            if self.initial_device:
                for d in adbmod.dedupe(online):
                    if self.initial_device.lower() in {d.serial.lower(), d.key.lower(), d.alias.lower(), d.name.lower()} or self.initial_device.lower() in d.name.lower():
                        pick = d
                        break
                self.initial_device = None
            self.current = pick or adbmod.dedupe(online)[0]
            self.sub_title = f"{self.current.display} · {self.current.serial}"
        elif self.current:
            self.sub_title = f"{self.current.display} · {self.current.serial}"
        if self.current:
            self.suggest.prewarm()
        self._render_devbar()

    def _device_from_row(self, key: Optional[str]) -> Optional[Device]:
        if not key or key.startswith("known:"):
            return None
        for d in self.devices:
            if d.serial == key:
                return d
        return None

    @on(DataTable.RowSelected, "#dev_table")
    def _dev_enter(self, ev: DataTable.RowSelected) -> None:
        key = ev.row_key.value if ev.row_key else None
        if key and key.startswith("known:"):
            self._wifi_connect_key(key[6:])
            return
        d = self._device_from_row(key)
        if d and d.online:
            self.set_current(d)
            self.action_tab("logs")
            if not (self.live and self.live.running):
                self.action_logs_toggle()

    def action_dev_select(self) -> None:
        key = self._selected_row_key("#dev_table")
        d = self._device_from_row(key)
        if d and d.online:
            self.set_current(d)
        elif key and key.startswith("known:"):
            self._wifi_connect_key(key[6:])

    def action_dev_wifi(self) -> None:
        key = self._selected_row_key("#dev_table")
        d = self._device_from_row(key) or self.current
        if not d:
            return
        if d.transport != "usb":
            same = [x for x in self.devices if x.key == d.key and x.transport == "usb" and x.online]
            if not same:
                self.notify("Hace falta el dispositivo por USB para activar el modo WiFi", severity="warning")
                return
            d = same[0]
        self._run_bg(lambda: wifi.setup(d), f"WiFi activado en {d.display}")

    def action_dev_connect(self) -> None:
        known = [(k, e) for k, e in registry.load().items() if e.get("ip")]
        if not known:
            self.push_screen(TextPrompt("Conectar a IP[:puerto]", "192.168.1.20:5555", candidates=self.suggest.ips), lambda v: v and self._run_bg(lambda: wifi.connect(v), f"Conectado a {v}"))
            return
        opts = [Text.assemble(T(e.get("alias") or e.get("model") or k, theme.color_for(k), bold=True), T(f"  {registry.hostport(e)}  visto {e.get('last_seen', '?')}", dim=True)) for k, e in known]
        opts.append(Text("Otra IP…", style="italic"))
        opts.append(Text("Vincular con código (Android 11+)…", style="italic"))

        def _cb(i: Optional[int]) -> None:
            if i is None:
                return
            if i < len(known):
                self._wifi_connect_key(known[i][0])
            elif i == len(known):
                self.push_screen(TextPrompt("Conectar a IP[:puerto]", "192.168.1.20:5555", candidates=self.suggest.ips), lambda v: v and self._run_bg(lambda: wifi.connect(v), f"Conectado a {v}"))
            else:
                self.push_screen(TextPrompt("IP:PUERTO de vinculación (Depuración inalámbrica → Vincular con código)", "192.168.1.20:37123", candidates=self.suggest.ips),
                                 lambda hp: hp and self.push_screen(TextPrompt("Código de vinculación", "123456"),
                                                                    lambda code: code and self._run_bg(lambda: wifi.pair(hp, code), "Vinculado")))
        self.push_screen(Choose("Conectar por WiFi", opts), _cb)

    def _wifi_connect_key(self, key: str) -> None:
        e = registry.load().get(key)
        if not e or not e.get("ip"):
            self.notify("Ese dispositivo no tiene IP guardada", severity="warning")
            return
        hp = registry.hostport(e)
        self._run_bg(lambda: wifi.connect(hp), f"Conectado a {hp}")

    def action_dev_disconnect(self) -> None:
        key = self._selected_row_key("#dev_table")
        d = self._device_from_row(key)
        if not d or d.transport != "wifi":
            self.notify("Selecciona una fila WiFi para desconectar", severity="warning")
            return
        self._run_bg(lambda: adbmod.disconnect(d.serial), f"Desconectado {d.serial}")

    def action_dev_usb(self) -> None:
        key = self._selected_row_key("#dev_table")
        d = self._device_from_row(key) or self.current
        if not d:
            return

        def _do():
            wifi.to_usb(d)
            for x in self.devices:
                if x.key == d.key and adbmod.is_hostport(x.serial):
                    adbmod.disconnect(x.serial)
        self._run_bg(_do, f"{d.display} de vuelta en modo USB")

    def action_dev_alias(self) -> None:
        key = self._selected_row_key("#dev_table")
        d = self._device_from_row(key) or self.current
        if not d:
            return

        def _cb(v: Optional[str]) -> None:
            if v is None:
                return
            if d.online:
                registry.upsert(d)
            registry.set_alias(d.key, v)
            self.notify(f"Alias de {d.name}: {v or '(ninguno)'}")
            self.refresh_devices()
        self.push_screen(TextPrompt(f"Alias para {d.name} ({d.serial})", "pixel", d.alias), _cb)

    @work(thread=True)
    def _run_bg(self, fn: Callable, ok_msg: str) -> None:
        try:
            fn()
        except UserError as e:
            self.call_from_thread(self.notify, str(e), severity="error", timeout=8)
        except Exception as e:
            self.call_from_thread(self.notify, f"{type(e).__name__}: {e}", severity="error", timeout=8)
        else:
            self.call_from_thread(self.notify, ok_msg, timeout=4)
        self.call_from_thread(self.refresh_devices)

    # ---------------------------------------------------------------- logs en vivo
    def _fit_renderer(self) -> None:
        if self.live:
            log = self.query_one("#log", RichLog)
            w = log.size.width - 4
            if w > 40:
                self.live.renderer.set_width(w)

    def on_resize(self) -> None:
        self._fit_renderer()
        if self.cache_renderer:
            w = self.query_one("#cache_view", RichLog).size.width - 4
            if w > 40:
                self.cache_renderer.set_width(w)

    def _read_filters(self, prefix: str) -> Filter:
        v = lambda i: self.query_one(f"#{prefix}{i}", Input).value.strip()
        pkgs = [p for p in v("pkg").replace(",", " ").split() if p]
        level = v("level") or "V"
        grep = v("grep") or None
        threads = [t for t in v("thread").replace(",", " ").split()] if prefix == "f_" else []
        pids = [int(p) for p in v("pid").replace(",", " ").split() if p.isdigit()] if prefix == "f_" else []
        return Filter(packages=pkgs, min_level=level, grep=grep, threads=threads, pids=pids)

    def action_logs_toggle(self) -> None:
        if self.live and self.live.running:
            self.live.stop()
            self.notify("Logs parados", timeout=2)
            return
        dev = self._dev_or_warn()
        if not dev:
            return
        try:
            filt = self._read_filters("f_")
        except ValueError as e:
            self.notify(str(e), severity="error")
            return
        if filt.thread_patterns and not (filt.packages or filt.explicit_pids):
            self.notify("Para filtrar por hilo indica paquete o pid", severity="warning")
            return
        show_thread = bool(filt.thread_patterns) or getattr(self, "_show_thread", False)
        show_proc = getattr(self, "_show_proc", False)
        renderer = Renderer(color=True, tag_width=int(config.get("tag_width")), wrap=True, highlight=(filt.grep.pattern if filt.grep else None),
                            show_thread=show_thread, thread_name=filt.thread_name, show_proc=show_proc, proc_name=filt.proc_name)
        since = getattr(self, "_resume_since", None)
        self._resume_since = None
        self.live = LiveLogs(dev, filt, renderer, tail=None, since=since)
        self._fit_renderer()
        self.live.start()
        self.query_one("#log", RichLog).focus()
        self._render_devbar()

    def _drain_logs(self) -> None:
        live = self.live
        if not live or live.paused or not live.queue:
            return
        log = self.query_one("#log", RichLog)
        batch = []
        q = live.queue
        for _ in range(min(len(q), 400)):
            batch.append(q.popleft())
        if batch:
            log.write(Text.from_ansi("\n".join(batch)))
        self.query_one("#log_status", Static).update(
            f"[dim]{live.dev.display} · {live.shown} mostradas / {live.total} leídas · "
            + (theme.paint("PAUSADO", theme.WARN, bold=True) if live.paused else (theme.paint("en vivo", theme.OK) if live.running else theme.paint("parado", theme.ERR)))
            + (f" · caché {live.session.log_path.name}" if live.session else "") + "[/]")

    def action_logs_pause(self) -> None:
        if self.live:
            self.live.paused = not self.live.paused
            self.notify("Pausado (p para seguir)" if self.live.paused else "Reanudado", timeout=2)

    def action_logs_clear(self) -> None:
        self.query_one("#log", RichLog).clear()

    def action_logs_filters(self) -> None:
        self.query_one("#f_pkg", Input).focus()

    def action_logs_errors(self) -> None:
        lvl = self.query_one("#f_level", Input)
        lvl.value = "V" if lvl.value.strip().upper() == "W" else "W"
        self._restart_logs()

    def action_logs_thread(self) -> None:
        self._show_thread = not getattr(self, "_show_thread", False)
        self._restart_logs()

    def action_logs_proc(self) -> None:
        self._show_proc = not getattr(self, "_show_proc", False)
        self._restart_logs()

    def _restart_logs(self) -> None:
        """Reinicia el stream con los filtros actuales continuando desde la última línea (sin repetir el buffer)."""
        if self.live and self.live.running:
            same_dev = self.current and self.live.dev.key == self.current.key
            self._resume_since = self.live.stream.last_ts if (same_dev and self.live.stream) else None
            self.live.stop()
            self.query_one("#log", RichLog).write(Text.from_ansi(self.live.renderer.banner("filtros cambiados · continúa desde la última línea", "info")))
            self.set_timer(0.4, self.action_logs_toggle)
        else:
            self.action_logs_toggle()

    @on(Input.Submitted)
    def _input_submitted(self, ev: Input.Submitted) -> None:
        iid = ev.input.id or ""
        field = {"f_pkg": "pkg", "c_pkg": "pkg", "f_grep": "grep", "c_grep": "grep", "f_thread": "thread"}.get(iid)
        if field:
            for tok in (ev.value.split() if field != "grep" else [ev.value]):
                self.suggest.remember(field, tok)
        if iid.startswith("f_"):
            self._restart_logs()
            self.query_one("#log", RichLog).focus()
        elif iid.startswith("c_"):
            self._load_cache_session()
            self.query_one("#cache_view", RichLog).focus()

    def open_logs_for(self, packages: List[str] = (), pids: List[int] = (), threads: List[str] = ()) -> None:
        self.query_one("#f_pkg", Input).value = " ".join(packages)
        self.query_one("#f_pid", Input).value = " ".join(map(str, pids))
        self.query_one("#f_thread", Input).value = " ".join(threads)
        self.action_tab("logs")
        self._restart_logs()

    def set_inspect_package(self, pkg: str) -> None:
        """Rellena el paquete en Inspector, DB y Red (desde la pestaña Apps)."""
        self.query_one("#inspect_pane", InspectPane).set_package(pkg)
        self.query_one("#db_pane", DbPane).query_one("#d_pkg", Input).value = pkg
        self.query_one("#net_pane", NetPane).set_package(pkg)

    # ---------------------------------------------------------------- caché
    def refresh_cache(self) -> None:
        t = self.query_one("#cache_table", DataTable)
        t.clear()
        self.cache_sessions = []
        for c in cache.list_cached():
            for s in c.sessions:
                self.cache_sessions.append(s)
                f = s.meta.get("filters") or {}
                fbits = []
                if f.get("packages"):
                    fbits.append(",".join(f["packages"]))
                if f.get("grep"):
                    fbits.append("/" + f["grep"] + "/")
                if f.get("threads"):
                    fbits.append("hilo:" + ",".join(f["threads"]))
                if f.get("min_level", "V") != "V":
                    fbits.append("≥" + f["min_level"])
                state = T("en curso", theme.OK) if s.live else (T("sin cerrar", theme.WARN) if not s.ended else T(""))
                if s.meta.get("reconnects"):
                    state.append_text(T(f" {s.meta['reconnects']} reconex.", dim=True))
                src = s.meta.get("source", "?")
                t.add_row(T(c.name, theme.color_for(c.key), bold=True), s.id, s.started.strftime("%Y-%m-%d %H:%M:%S") if s.started else "",
                          s.duration(), str(s.meta.get("lines", "?")), T(human_size(s.size), theme.size_color(s.size)),
                          T(src, theme.SOURCE.get(src, theme.MUTED)), " ".join(fbits), state, key=str(s.log_path))
        self.query_one("#cache_hint", Static).update(f"[dim]{len(self.cache_sessions)} sesiones en {config.LOGS_DIR} · Enter: abrir · f: seguir · x: borrar · o: Finder[/]")

    def _session_from_key(self, key: Optional[str]) -> Optional[cache.SessionInfo]:
        for s in self.cache_sessions:
            if str(s.log_path) == key:
                return s
        return None

    @on(DataTable.RowSelected, "#cache_table")
    def _cache_enter(self, ev: DataTable.RowSelected) -> None:
        s = self._session_from_key(ev.row_key.value if ev.row_key else None)
        if s:
            self.cache_current = s
            self._load_cache_session()

    @work(thread=True, exclusive=True, group="cache")
    def _load_cache_session(self) -> None:
        s = self.cache_current
        if not s:
            return
        try:
            filt = self.call_from_thread(self._read_filters, "c_")
        except ValueError as e:
            self.call_from_thread(self.notify, str(e), severity="error")
            return
        tail_txt = self.call_from_thread(lambda: self.query_one("#c_tail", Input).value.strip())
        tail = int(tail_txt) if tail_txt.isdigit() else 3000
        f = s.meta.get("filters") or {}
        if filt.packages and f.get("pids_seen"):
            filt.set_pids({int(p): n for p, n in f["pids_seen"].items() if any(n == pk or n.startswith(pk + ":") for pk in filt.packages)})
        if f.get("threads_seen"):
            filt.set_threads({int(t): n for t, n in f["threads_seen"].items()})
        if f.get("procs_seen"):
            filt.set_proc_names({int(p): n for p, n in f["procs_seen"].items()})
        view = self.query_one("#cache_view", RichLog)
        renderer = Renderer(color=True, tag_width=int(config.get("tag_width")), wrap=True, highlight=(filt.grep.pattern if filt.grep else None),
                            show_thread=bool(filt.thread_patterns), thread_name=filt.thread_name, show_proc=bool(f.get("procs_seen")), proc_name=filt.proc_name)
        w = view.size.width - 4
        renderer.set_width(w if w > 40 else 120)
        self.cache_filter, self.cache_renderer = filt, renderer
        buf: deque = deque(maxlen=tail)
        plain: deque = deque(maxlen=tail)
        total = 0
        self.cache_plain.clear()
        with open(s.log_path, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.rstrip("\n")
                total += 1
                r = self._render_cached(raw, keep=False)
                if r is not None:
                    buf.append(r)
                    plain.append(raw)
            pos = fh.tell()
        self.cache_plain.extend(plain)
        self.call_from_thread(self._show_cached, list(buf), total, pos)

    def _render_cached(self, raw: str, keep: bool = True) -> Optional[str]:
        """Devuelve la línea pintada (o None si no pasa el filtro) y guarda la cruda para copiar/revisar."""
        filt, renderer = self.cache_filter, self.cache_renderer
        out = None
        if raw.startswith("#droid "):
            out = renderer.banner(raw[7:], "warn")
        else:
            ll = parse(raw)
            if ll is None:
                if SEP_RE.match(raw) and not filt.packages:
                    out = renderer.separator(raw)
            else:
                filt.observe(ll)
                if filt.matches(ll):
                    out = renderer.line(ll)
        if out is not None and keep:
            self.cache_plain.append(raw)
        return out

    def _show_cached(self, lines: List[str], total: int, pos: int) -> None:
        view = self.query_one("#cache_view", RichLog)
        view.clear()
        s = self.cache_current
        view.write(Text.from_ansi(self.cache_renderer.banner(f"{s.meta.get('device', {}).get('model', '')} · sesión {s.id} · {total} líneas en disco · mostrando {len(lines)}", "ok")))
        if lines:
            view.write(Text.from_ansi("\n".join(lines)))
        if self.cache_fh:
            try:
                self.cache_fh.close()
            except Exception:
                pass
        self.cache_fh = open(s.log_path, "r", encoding="utf-8", errors="replace")
        self.cache_fh.seek(pos)
        self.query_one("#cache_hint", Static).update(f"[dim]{s.log_path} · {total} líneas · " + (theme.paint("siguiendo", theme.OK) if self.cache_follow else "f: seguir") + " · x: borrar · o: Finder[/]")
        view.focus()

    def action_cache_follow(self) -> None:
        self.cache_follow = not self.cache_follow
        self.notify("Siguiendo la sesión" if self.cache_follow else "Dejo de seguir", timeout=2)

    def _tick_periodic(self) -> None:
        # seguimiento de sesión de caché
        if self.cache_follow and self.cache_fh and self.cache_renderer:
            lines = []
            while True:
                line = self.cache_fh.readline()
                if not line:
                    break
                r = self._render_cached(line.rstrip("\n"))
                if r is not None:
                    lines.append(r)
            if lines:
                self.query_one("#cache_view", RichLog).write(Text.from_ansi("\n".join(lines)))
        if self.active_tab == "record":
            self.refresh_record()
        if self.active_tab == "procs" and self.procs_auto:
            self.refresh_procs()
        if self.live and (self.live.running or self.live.queue):
            self._render_devbar()

    def action_cache_delete(self) -> None:
        s = self._session_from_key(self._selected_row_key("#cache_table"))
        if not s:
            return
        if s.live:
            self.notify("Esa sesión está en curso", severity="warning")
            return

        def _cb(yes: bool) -> None:
            if not yes:
                return
            for p in (s.log_path, s.log_path.with_suffix(".json")):
                try:
                    p.unlink()
                except OSError:
                    pass
            self.notify(f"Borrada {s.id}")
            self.refresh_cache()
        self.push_screen(Confirm(f"¿Borrar la sesión {s.id} ({human_size(s.size)})?"), _cb)

    def action_cache_open(self) -> None:
        s = self._session_from_key(self._selected_row_key("#cache_table"))
        subprocess.Popen(["open", str(s.device_dir if s else config.LOGS_DIR)])

    # ---------------------------------------------------------------- procesos
    @work(thread=True, exclusive=True, group="procs")
    def refresh_procs(self) -> None:
        dev = self.current
        if not dev:
            return
        try:
            if self.procs_mode == "threads" and self.procs_pid:
                names = list_threads(dev.serial, [self.procs_pid])
                cpu = thread_cpu(dev.serial, [self.procs_pid])
                rows = sorted(((tid, n, cpu.get(tid, 0.0)) for tid, n in names.items()), key=lambda r: (-r[2], r[0]))
                self.call_from_thread(self._apply_threads, rows)
            else:
                procs = list_processes(dev.serial)
                self.call_from_thread(self._apply_procs, procs)
        except Exception as e:
            self.call_from_thread(self.notify, f"ps: {e}", severity="error")

    def _apply_procs(self, procs: List[dict]) -> None:
        t = self.query_one("#procs_table", DataTable)
        t.clear(columns=True)
        t.add_columns("PID", "Usuario", "%CPU", "RSS", "Proceso")
        apps = [p for p in procs if p["user"].startswith("u0_a") or p["user"].startswith("u10_a")]
        apps.sort(key=lambda p: (-p["cpu"], -p["rss"]))
        for p in apps:
            rss = p["rss"] * 1024
            t.add_row(T(p["pid"], theme.pid_color(p["pid"]), bold=True), p["user"], T(f"{p['cpu']:.1f}", theme.cpu_color(p["cpu"])),
                      T(human_size(rss), theme.size_color(rss)), T(p["name"], theme.pid_color(p["pid"])), key=str(p["pid"]))
        self.query_one("#procs_hint", Static).update(f"[dim]{self.current.display if self.current else ''} · {len(apps)} procesos de apps · t: hilos · l: logs del proceso · espacio: auto " + (theme.paint("ON", theme.OK) if self.procs_auto else "off") + "[/]")

    def _apply_threads(self, rows) -> None:
        t = self.query_one("#procs_table", DataTable)
        t.clear(columns=True)
        t.add_columns("TID", "Hilo", "%CPU", "PID")
        pid = self.procs_pid
        for tid, name, c in rows:
            shown = "main" if tid == pid else name
            label = T(shown, theme.thread_color(shown), bold=(tid == pid))
            if tid == pid:
                label.append_text(T(f" {name}", dim=True))
            t.add_row(str(tid), label, T(f"{c:.1f}", theme.cpu_color(c)), T(pid, theme.pid_color(pid), bold=True), key=f"tid:{tid}")
        self.query_one("#procs_hint", Static).update(f"[dim]hilos de pid {pid} · {len(rows)} hilos · t: volver a procesos · l: logs del proceso[/]")

    def action_procs_threads(self) -> None:
        if self.procs_mode == "threads":
            self.procs_mode = "procs"
        else:
            key = self._selected_row_key("#procs_table")
            if not key or not key.isdigit():
                return
            self.procs_pid = int(key)
            self.procs_mode = "threads"
        self.refresh_procs()

    def action_procs_logs(self) -> None:
        key = self._selected_row_key("#procs_table")
        pid = self.procs_pid if self.procs_mode == "threads" else (int(key) if key and key.isdigit() else None)
        if pid:
            self.open_logs_for(pids=[pid])

    def action_procs_auto(self) -> None:
        self.procs_auto = not self.procs_auto
        self.refresh_procs()

    # ---------------------------------------------------------------- apps
    @work(thread=True, exclusive=True, group="apps")
    def refresh_apps(self) -> None:
        dev = self.current
        if not dev:
            return
        try:
            if self._projects is None:
                self._projects = scan_projects(Path(config.get("projects_dir")).expanduser())
            found = find_project_apps(dev.serial, self._projects)
        except Exception as e:
            self.call_from_thread(self.notify, f"apps: {e}", severity="error")
            return
        self.call_from_thread(self._apply_apps, found)

    def _apply_apps(self, found) -> None:
        t = self.query_one("#apps_table", DataTable)
        t.clear()
        for a in found:
            proc = Text()
            if a.pids:
                for i, p in enumerate(a.pids):
                    proc.append_text(T(("" if i == 0 else ", ") + f"pid {p}", theme.pid_color(p), bold=True))
            else:
                proc = T("parada", dim=True)
            t.add_row(T(a.project.project, theme.color_for(a.project.project), bold=True), a.project.module, a.package,
                      f"{a.version_name} ({a.version_code})" if a.version_name else "",
                      T("✔ debug", theme.OK) if a.debuggable else T("✖ release", theme.ERR), proc, a.last_update, key=a.package)
        matched = {a.project.app_id for a in found}
        rest = [p for p in (self._projects or []) if p.app_id not in matched]
        if self.apps_all:
            for p in rest:
                t.add_row(T(p.project, theme.color_for(p.project), dim=True), T(p.module, dim=True), T(p.app_id, dim=True), "", T("no instalada", dim=True), "", "", key=f"missing:{p.app_id}")
        self.query_one("#apps_hint", Static).update(f"[dim]{len(found)} apps de tus proyectos instaladas en {self.current.display if self.current else '?'} · {len(rest)} no instaladas ({'mostradas' if self.apps_all else 'a: mostrar'}) · Enter/l: logs de la app[/]")

    @on(DataTable.RowSelected, "#apps_table")
    def _apps_enter(self, ev: DataTable.RowSelected) -> None:
        key = ev.row_key.value if ev.row_key else None
        if key and not key.startswith("missing:"):
            self.open_logs_for(packages=[key])

    def action_apps_logs(self) -> None:
        key = self._selected_row_key("#apps_table")
        if key and not key.startswith("missing:"):
            self.open_logs_for(packages=[key])

    def action_apps_inspect(self) -> None:
        key = self._selected_row_key("#apps_table")
        if key and not key.startswith("missing:"):
            self.set_inspect_package(key)
            self.action_tab("inspect")
            self.query_one("#inspect_pane", InspectPane).action_toggle()

    def action_apps_db(self) -> None:
        key = self._selected_row_key("#apps_table")
        if key and not key.startswith("missing:"):
            self.set_inspect_package(key)
            self.action_tab("db")
            self.query_one("#db_pane", DbPane).action_reload()

    def action_apps_all(self) -> None:
        self.apps_all = not self.apps_all
        self.refresh_apps()

    # ---------------------------------------------------------------- grabación
    def refresh_record(self) -> None:
        t = self.query_one("#rec_table", DataTable)
        rows = record.all_status()
        self._rec_rows = rows
        t.clear()
        for r in rows:
            lines, size = "?", 0
            sp = r.get("session")
            if sp:
                try:
                    p = Path(sp)
                    size = p.stat().st_size
                    import json
                    meta = json.loads(p.with_suffix(".json").read_text())
                    lines = str(meta.get("lines", "?"))
                except Exception:
                    pass
            t.add_row(Text.assemble(T("● ", theme.OK), T(r.get("alias") or r.get("model") or r["key"], theme.color_for(r["key"]), bold=True)),
                      r.get("serial", ""), str(r["pid"]), r.get("started", ""), lines, T(human_size(size), theme.size_color(size)), sp or "", key=r["key"])
        self.query_one("#rec_hint", Static).update(f"[dim]{len(rows)} grabaciones en background (sobreviven a cerrar la app) · s: grabar actual · S: todos · x: parar · X: todas[/]")

    def action_rec_start(self) -> None:
        dev = self._dev_or_warn()
        if not dev:
            return
        self._run_bg(lambda: record.start(dev), f"Grabando {dev.display} en background")

    def action_rec_start_all(self) -> None:
        devs = adbmod.dedupe([d for d in self.devices if d.online])

        def _do():
            for d in devs:
                try:
                    record.start(d)
                except RuntimeError:
                    pass
        self._run_bg(_do, f"Grabando {len(devs)} dispositivo(s)")

    def action_rec_stop(self) -> None:
        key = self._selected_row_key("#rec_table")
        if key:
            self._run_bg(lambda: record.stop(key), "Grabación detenida")

    def action_rec_stop_all(self) -> None:
        rows = list(self._rec_rows)
        self._run_bg(lambda: [record.stop(r["key"]) for r in rows], "Grabaciones detenidas")

    # ---------------------------------------------------------------- salida
    def action_quit(self) -> None:
        if getattr(self, "_tracker", None):
            self._tracker.stop()
        if self.live:
            self.live.stop()
        try:
            ip = self.query_one("#inspect_pane", InspectPane)
            if ip.session and ip.session.running:
                ip.session.stop()
            npn = self.query_one("#net_pane", NetPane)
            if npn.mon and npn.mon.running:
                npn.mon.stop()
        except Exception:
            pass
        if self.cache_fh:
            try:
                self.cache_fh.close()
            except Exception:
                pass
        self.exit()


def run_app(device: Optional[str] = None) -> int:
    app = DroidApp(initial_device=device)
    app.run()
    return 0
