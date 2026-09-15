"""Autocompletado de la app: cada campo sugiere lo que droid ya identifica (procesos vivos, apps de tus proyectos,
hilos del proceso, niveles, tags vistos, tablas/columnas de la DB, IPs conocidas) más un historial de valores recientes."""
import json
import re
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set

from rich.markup import escape
from textual.content import Content
from textual_autocomplete import AutoComplete, DropdownItem, TargetState

from . import adb as adbmod
from . import config, registry, theme

HISTORY_FILE = config.DROID_HOME / "history.json"
LEVELS = [("V", "verbose"), ("D", "debug"), ("I", "info"), ("W", "warn"), ("E", "error"), ("F", "fatal")]
THREAD_PRESETS = ["main", "RenderThread", "OkHttp*", "Binder:*", "binder:*", "pool-*", "DefaultDispatch*", "arch_disk_io*",
                  "GLThread*", "Firebase*", "Retrofit*", "AsyncTask*", "Coil*", "Glide*", "Room*", "WorkManager*"]
COMMAND_SNIPPETS = [
    ("shell", 'getprop | grep -iE "_for_attestation|^\\[ro\\.product\\.(brand|model|name|device|manufacturer)\\]"'),
    ("shell", "getprop ro.build.version.release"),
    ("shell", "getprop ro.product.model"),
    ("shell", "dumpsys battery"),
    ("shell", 'dumpsys activity activities | grep -E "mResumedActivity|topResumedActivity"'),
    ("shell", "dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'"),
    ("shell", "pm list packages -3"),
    ("shell", "pm path <paquete>"),
    ("shell", "dumpsys package <paquete> | grep -E 'versionName|versionCode|pkgFlags'"),
    ("shell", "am start -n <paquete>/<actividad>"),
    ("shell", "am force-stop <paquete>"),
    ("shell", "pm clear <paquete>"),
    ("shell", "monkey -p <paquete> 1"),
    ("shell", "input keyevent KEYCODE_HOME"),
    ("shell", "input keyevent KEYCODE_BACK"),
    ("shell", "input text 'hola'"),
    ("shell", "wm size"),
    ("shell", "wm density"),
    ("shell", "settings get global adb_enabled"),
    ("shell", "ps -A | grep <paquete>"),
    ("shell", "top -n 1 | head -25"),
    ("shell", "cat /proc/meminfo | head -5"),
    ("shell", "df -h /data"),
    ("shell", "ip addr show wlan0"),
    ("shell", "logcat -d -t 200 | grep -iE 'error|exception'"),
    ("adb", "adb devices -l"),
    ("adb", "adb exec-out screencap -p > ~/Desktop/pantalla.png"),
    ("adb", "adb pull /sdcard/Download/<archivo> ~/Downloads/"),
    ("adb", "adb push <archivo> /sdcard/Download/"),
    ("adb", "adb install -r <ruta.apk>"),
    ("adb", "adb uninstall <paquete>"),
    ("adb", "adb reboot"),
    ("adb", "adb tcpip 5555"),
    ("adb", "adb logcat -d -b crash"),
    ("local", "!ls ~/Desktop"),
]
SQL_KEYWORDS = ["SELECT", "FROM", "WHERE", "ORDER BY", "GROUP BY", "LIMIT", "COUNT(*)", "DISTINCT", "JOIN", "LEFT JOIN", "ON",
                "AND", "OR", "NOT", "NULL", "IS NULL", "IS NOT NULL", "LIKE", "IN", "BETWEEN", "ASC", "DESC", "AS", "HAVING",
                "PRAGMA table_info()", "SELECT name, sql FROM sqlite_master"]


def _item(main: str, prefix: str = "", color: Optional[int] = None, bold: bool = False, dim: bool = False) -> DropdownItem:
    if prefix:
        style = (theme.hx(color) if color is not None else "") + (" bold" if bold else "") + (" dim" if dim else "")
        pre = Content.from_markup(f"[{style.strip()}]{escape(prefix)}[/] " if style.strip() else escape(prefix) + " ")
    else:
        pre = None
    return DropdownItem(main, prefix=pre)


class Suggest:
    """Fuentes de sugerencias con caché por dispositivo (TTL) cargadas en background."""

    def __init__(self, app):
        self.app = app
        self._cache: Dict[str, tuple] = {}
        self._loading: Set[str] = set()
        self._lock = threading.Lock()
        self.history: Dict[str, List[str]] = self._load_history()

    # --- infraestructura ---
    def _serial(self) -> Optional[str]:
        dev = getattr(self.app, "current", None)
        return dev.serial if dev else None

    def _cached(self, key: str, loader: Callable[[], list], ttl: float) -> list:
        now = time.time()
        ent = self._cache.get(key)
        if ent and now - ent[0] < ttl:
            return ent[1]
        with self._lock:
            if key in self._loading:
                return ent[1] if ent else []
            self._loading.add(key)

        def run() -> None:
            try:
                data = loader()
            except Exception:
                data = ent[1] if ent else []
            self._cache[key] = (time.time(), data)
            with self._lock:
                self._loading.discard(key)
        threading.Thread(target=run, daemon=True).start()
        return ent[1] if ent else []

    def invalidate(self) -> None:
        self._cache = {k: v for k, v in self._cache.items() if k == "projects"}

    def prewarm(self) -> None:
        """Carga en background lo habitual para que la primera lista salga al instante."""
        self._procs(); self._installed(); self._projects()

    # --- cargas ---
    def _procs(self) -> List[dict]:
        serial = self._serial()
        if not serial:
            return []
        from .logs import list_processes
        return self._cached(f"procs:{serial}", lambda: list_processes(serial), ttl=6.0)

    def _installed(self) -> List[str]:
        serial = self._serial()
        if not serial:
            return []
        from .apps import installed_packages
        return self._cached(f"installed:{serial}", lambda: installed_packages(serial), ttl=120.0)

    def _projects(self) -> list:
        from .apps import scan_projects
        return self._cached("projects", lambda: scan_projects(Path(config.get("projects_dir")).expanduser()), ttl=3600.0)

    def _threads_for(self, pids: List[int]) -> Dict[int, str]:
        serial = self._serial()
        if not serial or not pids:
            return {}
        from .logs import list_threads
        key = f"threads:{serial}:{','.join(map(str, sorted(pids)))}"
        return self._cached(key, lambda: list_threads(serial, pids), ttl=5.0) or {}

    # --- items ---
    def packages(self, field: str = "pkg") -> List[DropdownItem]:
        procs = self._procs()
        installed = set(self._installed())
        projects = self._projects()
        items: List[DropdownItem] = []
        seen: Set[str] = set()
        running: Dict[str, int] = {}
        for p in procs:
            base = p["name"].split(":")[0]
            if base and "." in base and base not in running:
                running[base] = p["pid"]
        for v in self.history.get(field, []):
            if v not in seen:
                seen.add(v); items.append(_item(v, "reciente", theme.MUTED))
        for pa in projects:
            for pkg in sorted(pk for pk in installed if pa.matches(pk)):
                if pkg in seen:
                    continue
                seen.add(pkg)
                pre = f"★ {pa.project}" + (f" · pid {running[pkg]}" if pkg in running else "")
                items.append(_item(pkg, pre[:26], theme.color_for(pa.project), bold=True))
        for pkg, pid in sorted(running.items(), key=lambda kv: kv[0]):
            if pkg in seen:
                continue
            seen.add(pkg); items.append(_item(pkg, f"● pid {pid}", theme.pid_color(pid)))
        for pkg in sorted(installed):
            if pkg not in seen:
                seen.add(pkg); items.append(_item(pkg, "instalada", theme.MUTED, dim=True))
        return items

    def pids(self) -> List[DropdownItem]:
        procs = sorted(self._procs(), key=lambda p: (not (p["user"].startswith("u0_a") or p["user"].startswith("u10_a")), -p["cpu"], p["name"]))
        return [_item(str(p["pid"]), p["name"][:28], theme.pid_color(p["pid"])) for p in procs]

    def _resolve_pids(self, pkg_text: str, pid_text: str) -> List[int]:
        pids = [int(x) for x in re.split(r"[ ,]+", pid_text.strip()) if x.isdigit()]
        pkgs = [x for x in re.split(r"[ ,]+", pkg_text.strip()) if x]
        if pkgs:
            for p in self._procs():
                if any(p["name"] == k or p["name"].startswith(k + ":") for k in pkgs):
                    pids.append(p["pid"])
        return sorted(set(pids))

    def threads(self, pkg_text: str = "", pid_text: str = "", field: str = "thread") -> List[DropdownItem]:
        items: List[DropdownItem] = []
        seen: Set[str] = set()
        for v in self.history.get(field, []):
            if v not in seen:
                seen.add(v); items.append(_item(v, "reciente", theme.MUTED))
        pids = self._resolve_pids(pkg_text, pid_text)
        names = self._threads_for(pids)
        for tid, name in sorted(names.items(), key=lambda kv: (kv[0] not in pids, kv[1].lower())):
            shown = "main" if tid in pids else name
            if shown in seen:
                continue
            seen.add(shown); items.append(_item(shown, f"tid {tid}", theme.thread_color(shown), bold=(shown == "main")))
        for p in THREAD_PRESETS:
            if p not in seen:
                seen.add(p); items.append(_item(p, "patrón", theme.MUTED, dim=True))
        return items

    def levels(self) -> List[DropdownItem]:
        return [_item(l, name, theme.LEVEL_FG[l], bold=True) for l, name in LEVELS]

    def greps(self, field: str = "grep") -> List[DropdownItem]:
        items = [_item(v, "reciente", theme.MUTED) for v in self.history.get(field, [])]
        live = getattr(self.app, "live", None)
        tags = list(getattr(getattr(live, "renderer", None), "_tag_cache", {}).keys()) if live else []
        seen = {i.value for i in items}
        for t in sorted(tags, key=str.lower)[:60]:
            if t not in seen:
                items.append(_item(t, "tag", theme.color_for(t, theme.TAGS)))
        for pat in ("Exception", "FATAL|ANR|died", "http", "SQLite|Room", "Retrofit|OkHttp"):
            if pat not in seen:
                items.append(_item(pat, "patrón", theme.MUTED, dim=True))
        return items

    def intervals(self) -> List[DropdownItem]:
        return [_item(v, "seg", theme.MUTED) for v in ("0.5", "1", "2", "5", "10")]

    def tails(self) -> List[DropdownItem]:
        return [_item(v, "líneas", theme.MUTED) for v in ("200", "500", "1000", "3000", "10000")]

    def sql(self, conn, field: str = "sql") -> List[DropdownItem]:
        items = [_item(v, "reciente", theme.MUTED) for v in self.history.get(field, [])]
        if conn is not None:
            try:
                from . import db as dbmod
                for name, n, typ in dbmod.tables(conn):
                    items.append(_item(name, f"{typ} · {n} filas", theme.color_for(name), bold=True))
                    for col, ty, _, pk in dbmod.schema(conn, name):
                        items.append(_item(col, f"col · {name}"[:24], theme.color_for(name), dim=not pk))
                    items.append(_item(f'SELECT * FROM "{name}" LIMIT 100', "consulta", theme.MUTED))
            except Exception:
                pass
        items += [_item(k, "SQL", theme.MUTED, dim=True) for k in SQL_KEYWORDS]
        return items

    def commands(self, field: str = "cmd") -> List[DropdownItem]:
        items = [_item(v, "reciente", theme.MUTED) for v in self.history.get(field, [])]
        seen = {i.value for i in items}
        for kind, cmd in COMMAND_SNIPPETS:
            if cmd in seen:
                continue
            seen.add(cmd)
            color = {"shell": theme.INFO, "adb": theme.WARN, "local": theme.MUTED}[kind]
            items.append(_item(cmd, {"shell": "adb shell", "adb": "adb", "local": "local"}[kind], color, dim=(kind == "local")))
        return items

    def ips(self) -> List[DropdownItem]:
        items = []
        for k, e in registry.load().items():
            if e.get("ip"):
                items.append(_item(registry.hostport(e), (e.get("alias") or e.get("model") or k)[:20], theme.color_for(k), bold=True))
        for d in getattr(self.app, "devices", []):
            if d.ip and d.online:
                hp = f"{d.ip}:{config.get('wifi_port')}"
                if hp not in {i.value for i in items}:
                    items.append(_item(hp, d.display[:20], theme.color_for(d.key)))
        return items

    # --- historial ---
    def _load_history(self) -> Dict[str, List[str]]:
        try:
            return json.loads(HISTORY_FILE.read_text()) if HISTORY_FILE.exists() else {}
        except Exception:
            return {}

    def remember(self, field: str, value: str) -> None:
        value = value.strip()
        if not value:
            return
        lst = [v for v in self.history.get(field, []) if v != value]
        self.history[field] = [value] + lst[:19]
        try:
            config.ensure_dirs()
            HISTORY_FILE.write_text(json.dumps(self.history, indent=2, ensure_ascii=False))
        except Exception:
            pass


class DroidAutoComplete(AutoComplete):
    """Desplegable de sugerencias bajo un Input. `multi` completa solo el último token (campos con varios valores)."""

    DEFAULT_CSS = """
    DroidAutoComplete { max-height: 10; border-left: tall $accent; }
    """

    def __init__(self, target, provider: Callable[[], List[DropdownItem]], multi: bool = False, show_on_empty: bool = True,
                 sep: str = " ", **kw):
        self.provider = provider
        self.multi = multi
        self.show_on_empty = show_on_empty
        self.sep = sep
        super().__init__(target, candidates=self._candidates, **kw)

    def _candidates(self, state: TargetState) -> List[DropdownItem]:
        try:
            return self.provider()
        except Exception:
            return []

    def get_search_string(self, target_state: TargetState) -> str:
        text = target_state.text[: target_state.cursor_position]
        if self.multi:
            return re.split(r"[ ,]+", text)[-1] if text else ""
        return text

    def should_show_dropdown(self, search_string: str) -> bool:
        option_list = self.option_list
        n = option_list.option_count
        if n == 0:
            return False
        if not search_string:
            return self.show_on_empty
        if n == 1:
            first = option_list.get_option_at_index(0).prompt
            plain = first.plain if hasattr(first, "plain") else str(first)
            return plain != search_string
        return True

    def _handle_focus_change(self, has_focus: bool) -> None:
        if not has_focus:
            self.action_hide()
        else:
            self._handle_target_update()      # muestra la lista nada más entrar en el campo

    def apply_completion(self, value: str, state: TargetState) -> None:
        target = self.target
        if self.multi:
            before, after = state.text[: state.cursor_position], state.text[state.cursor_position:]
            m = re.search(r"[^ ,]*$", before)
            start = m.start() if m else len(before)
            new = before[:start] + value + self.sep + after.lstrip(" ,")
            target.value = new
            target.cursor_position = start + len(value) + len(self.sep)
        else:
            target.value = value
            target.cursor_position = len(value)
        new_state = self._get_target_state()
        self._rebuild_options(new_state, self.get_search_string(new_state))
