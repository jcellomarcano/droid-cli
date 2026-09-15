# Roadmap: `droid inspect` — App inspector / build analyzer

Objetivo: inspeccionar en vivo, desde la terminal, las apps **debuggables de tus proyectos** instaladas
en tus dispositivos, y analizar sus builds. Solo apps cuyo `applicationId` sale de un módulo Android
en `~/AndroidStudioProjects` (configurable con `droid config projects_dir`), para que sean tuyas y se
identifiquen sin ambigüedad.

## Principios

- **Sin agente en la app** en la fase inicial: todo se obtiene por `adb shell` (`dumpsys`, `/proc`,
  `run-as`, `simpleperf`, `perfetto`). Solo funciona con builds `debuggable`, que es exactamente el
  límite que quieres (tus apps, builds de desarrollo).
- **Misma caché**: cada sesión de inspección se guarda en `~/.droid/inspect/<dispositivo>/<fecha>/`
  (muestras en JSONL + resumen) para post-mortem, igual que los logs.
- **Mismos selectores**: `droid inspect pix`, alias, índice, etc.
- **TUI con [Textual](https://textual.textualize.io/)** (misma familia que `rich`, ya instalada):
  pestañas por sección, sparklines/gráficas en terminal, atajos de teclado. Toda pestaña tiene
  equivalente no interactivo (`--json`, `--csv`) para scripts y CI.

## Fase 0 — identificación (hecho: `droid apps`)

- Escaneo de `build.gradle(.kts)` → `applicationId`, `applicationIdSuffix`, `namespace`.
- Cruce con `pm list packages` + `dumpsys package` → versión, `DEBUGGABLE`, PID, fecha de instalación.
- Preferencia por el proyecto no archivado / más reciente cuando dos comparten `applicationId`.

## Sección "Filtros" (transversal a todas las pestañas)

Ya existe en la CLI (`droid ps`, `droid logs --pid/--tid/--thread`). En la TUI será un panel lateral
persistente que se aplica a Logs, CPU, Memoria y Red a la vez:

- **Procesos**: lista viva de procesos de la app (`pkg`, `pkg:servicio`, `:remote`) con PID, %CPU,
  RSS; marcar/desmarcar para incluir. Se recuerda entre reinicios del proceso (por nombre, no por PID).
- **Hilos**: árbol por proceso con nombre (`comm`), TID, %CPU, estado (R/S/D); buscar por nombre
  con comodines; presets: `main`, `RenderThread`, `OkHttp*`, `Room*`/`arch_disk_io*`, `pool-*`,
  `DefaultDispatch*` (coroutines), `Binder:*`.
- **Nivel / tag / regex** como en `logs`, más "solo excepciones" (`AndroidRuntime`, `E/`, stacktraces).
- Guardar/cargar filtros con nombre (`~/.droid/filters.json`) y aplicarlos por CLI:
  `droid logs pix --filter red-lenta`.

## Fase 1 — Overview + CPU + RAM (muestreo cada 1 s)  ✔ hecho (2026-09-08): `droid inspect`, pestaña 7

| Métrica | Fuente | Notas |
|---|---|---|
| CPU % app / total, por hilo | `/proc/<pid>/stat`, `/proc/<pid>/task/*/stat`, `top -n1 -p` | delta de jiffies; hilos ordenados por consumo (detecta bucles calientes) |
| PSS / RSS / Java heap / Native / Graphics / Code / Stack | `dumpsys meminfo <pkg>` | tabla + sparkline; alertas de crecimiento sostenido (leak sospechoso) |
| GC | logcat tag `art` ("Background concurrent copying GC freed…") | frecuencia y pausa media |
| Frames / jank | `dumpsys gfxinfo <pkg> framestats` | P50/P90/P99, frames >16/32 ms, "ciclos" de render |
| ANR / crash | logcat `ActivityManager`, `AndroidRuntime`, `/data/anr` via `run-as` | se anotan como eventos en la línea temporal |
| Batería / energía | `dumpsys batterystats --charged <pkg>` | wakelocks, alarms, jobs, radios |
| Estado app | `dumpsys activity <pkg>`, `am stack list` | actividad en primer plano, servicios, procesos `:remote` |

Comando previsto: `droid inspect <dev> [-p <pkg>] [--interval 1] [--duration 60s] [--json]`.

## Fase 2 — Red (Network inspector)  ◐ parcial: `droid net`, pestaña 9 (tasa por interfaz, totales por uid, sockets). Pendiente: HTTP

- **Contadores por UID** (`/proc/net/xt_qtaguid` no existe ya; usar `dumpsys netstats detail` y
  `/proc/<pid>/net/dev` delta) → gráfica rx/tx por segundo, total por sesión, WiFi vs móvil.
- **Sockets abiertos**: `ss -tanp` filtrado por PID (host:puerto, estado).
- **Peticiones HTTP** (dos opciones, elegir en su momento):
  1. *Sin tocar la app*: proxy local (`mitmproxy` como dependencia opcional) + `adb reverse` +
     `settings put global http_proxy`. Ve URL, método, código, latencia, tamaño; HTTPS solo con
     `network_security_config` de debug que confíe en el CA de usuario (típico en builds debug).
  2. *Con interceptor en la app*: un artefacto Gradle `droid-agent` (solo `debugImplementation`)
     que exporta OkHttp/Ktor events por un socket local → `adb forward`. Da cuerpo de
     request/response, timings por fase (DNS, TLS, TTFB) y trazas WebSocket.
- Pestaña con lista de peticiones, detalle, filtro por host/código, exportar HAR.

## Fase 3 — Base de datos  ✔ hecho: `droid db`, pestaña 8 (snapshot run-as, tablas, SQL, CSV, prefs). Pendiente: diff entre snapshots en la TUI, DataStore

- Descubrir DBs: `run-as <pkg> ls databases/` (+ `files/`, `no_backup/`, Room/SQLDelight/Realm).
- `run-as <pkg> cat databases/x.db > snapshot` → `sqlite3` local (con `-wal` y `-shm` para no perder
  transacciones abiertas). Tablas, esquemas, filas, consulta SQL libre, exportar CSV.
- Diff entre snapshots (qué filas cambiaron mientras reproducías un flujo).
- DataStore / SharedPreferences: `run-as <pkg> cat shared_prefs/*.xml` y `files/datastore/*.preferences_pb`.
- Modo "live": re-snapshot cada N segundos con el DB Inspector de Android Studio cerrado (evita locks).

## Fase 4 — Build analyzer

- APK/AAB de tu último build: `apkanalyzer` (cmdline-tools ya instalados en `/opt/android-sdk`):
  tamaño por tipo (dex, res, assets, native libs), número de métodos por paquete, comparar dos
  builds (`droid build diff app-debug.apk app-debug-prev.apk`).
- Detectar el APK instalado en el dispositivo (`pm path <pkg>`) y compararlo con el del proyecto
  (`build/outputs/apk/**`) → "lo que tienes instalado no es lo último compilado".
- Gradle: leer `build/reports/` y `--scan` opcional; duración por tarea desde
  `build/reports/configuration-cache` / `--profile`.
- Baseline profiles / R8: comprobar `mapping.txt`, desofuscar stacktraces de la caché de logs
  (`droid cache show … --retrace <mapping>`).

## Fase 5 — Traces profundos (opcional)

- `simpleperf record -p <pid>` → flamegraph HTML.
- `perfetto` con config predefinida (CPU, memoria, frames) → abrir en ui.perfetto.dev.
- Integrar con los eventos de la caché de logs (misma línea temporal).

## Diseño del comando

```
droid inspect [dispositivo] [-p pkg]           # TUI: Overview · CPU · Memoria · Red · DB · Build
droid inspect … --section cpu --duration 30s --json   # sin TUI, para scripts
droid inspect … --record                        # guarda la sesión en ~/.droid/inspect/
droid inspect replay <sesión>                   # post-mortem de una sesión guardada
droid db  [dispositivo] [-p pkg] [--query "SELECT …"]
droid net [dispositivo] [-p pkg] [--proxy]
droid build analyze <apk|aab> | diff <a> <b>
```

## Estructura de código prevista

```
droid/inspect/
  collectors/   cpu.py  mem.py  gfx.py  net.py  db.py  battery.py  (cada uno: sample() -> dict)
  session.py    muestreo periódico + escritura JSONL + resumen
  tui/          app.py (Textual), widgets de gráficas, pestañas
  build/        apkanalyzer.py, gradle.py, retrace.py
```

Cada collector es independiente y testeable con salidas de `dumpsys` capturadas (fixtures).

## Orden sugerido

1. Fase 1 (CPU/RAM/frames) — mayor valor, solo `dumpsys` + `/proc`.
2. Fase 3 (DB) — muy útil y sencilla con `run-as` + `sqlite3`.
3. Fase 2 opción 1 (red sin agente), luego el agente si hace falta cuerpo de peticiones.
4. Fase 4 (build analyzer) — `apkanalyzer` ya está en tu SDK.
5. Fase 5.
