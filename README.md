# droid

CLI global para tu Mac que envuelve `adb` con lo que usas a diario. `droid` a secas abre la **app de terminal**
(pestañas, teclado, logs en vivo); cada pestaña tiene su subcomando para scripts.

- `droid ls` — qué dispositivos hay conectados por **USB** y por **WiFi** (modelo, Android, IP, batería; detecta cuando el mismo móvil está por las dos vías).
- `droid wifi` — pasar un dispositivo USB a WiFi, reconectarlo por alias, vincular por código (Android 11+).
- `droid logs` — logcat en vivo con colores, filtros por app/tag/nivel/regex y wrap.
- **Caché post-mortem** — cada sesión de logs se guarda por dispositivo en `~/.droid/logs/` y sigue ahí cuando el dispositivo muere o se desconecta (con reconexión automática).
- `droid record` — grabación en background aunque cierres la terminal.
- `droid apps` — apps de **tus proyectos** instaladas en el dispositivo (versión, debuggable, PID).
- `droid inspect` — **inspector en vivo**: CPU por hilo, RSS/PSS, Java/Native heap, Views/Activities, fps, jank y percentiles de frame, salidas de la app (crash/ANR/LMK). Sesiones guardadas como JSONL.
- `droid db` — bases de datos SQLite de apps debuggables: snapshot vía `run-as`, tablas, filas, SQL libre, CSV, `shared_prefs`.
- `droid net` — tasa por interfaz en vivo, totales por app y sockets abiertos.
- `droid ps` — procesos e hilos (PID/TID, %CPU) para elegir qué filtrar.
- **Identidad de color**: cada dispositivo, proceso (PID), hilo y tag tiene un color fijo en toda la herramienta; niveles y estados con colores fijos; excepciones, `FATAL EXCEPTION`/ANR/`has died` y URLs se resaltan solos.

## La app de terminal

```bash
droid            # abre la app (si hay terminal); droid ui / droid app también
droid ui pixel   # arranca con ese dispositivo seleccionado
```

**Seleccionar y copiar texto:** arrastra con el ratón sobre cualquier panel de log y `ctrl+c` (o `cmd+c`) lo copia al
portapapeles del Mac (usa `pbcopy`, así que funciona también en Terminal.app, que ignora el método OSC 52 del terminal).
Las líneas partidas por el ancho de la pantalla se vuelven a unir al copiar, así que lo que pegas son líneas de log completas.

| Tecla | Qué copia |
|---|---|
| `y` | la selección; si no hay, las últimas líneas del panel (en una tabla, la fila del cursor) |
| `Y` | todo el panel o toda la tabla (TSV con cabecera) |
| `v` | abre el **visor**: texto congelado para leer y recortar con calma |

En el visor: ratón o `shift+flechas` selecciona, `a` selecciona todo, `c` copia (la selección o todo), `g` guarda el recorte
en `~/.droid/clips/`, `e` lo abre en el editor, `w` alterna el ajuste de línea, `Esc` cierra. En logs en vivo, `v` pausa el
stream para que puedas recortar sin que se mueva (`p` lo reanuda). Lo copiado es texto limpio: la línea cruda de logcat,
sin colores ni marcos, lista para pegar en un ticket o pasarla por `grep`.

Para usar la selección propia del terminal en vez de la de la app, mantén ⌥ (Option) mientras arrastras.

**Autocompletado en todos los campos:** al entrar en un campo aparece una lista de sugerencias con lo que droid ya conoce
(apps de tus proyectos marcadas con ★, procesos vivos con su PID, paquetes instalados, hilos del proceso elegido, niveles,
tags vistos en el log, tablas y columnas de la DB abierta, IPs conocidas) más tus valores recientes (`~/.droid/history.json`).
↑↓ elige, Tab o Enter completa, Esc oculta, y escribir filtra. Los campos con varios valores (paquetes, hilos, PIDs) completan el último token.

La lista de dispositivos se **refresca sola** al conectar o desconectar (aviso en pantalla); `r`/`F5` refresca a mano y `R` fuerza `adb reconnect offline` para dispositivos atascados.

Pestañas (teclas 1-9, 0): **1 Dispositivos** (Enter: logs, espacio: elegir, w: USB→WiFi, c: conectar, d: desconectar, u: USB, a: alias) ·
**2 Logs** (barra de filtros: paquete, nivel, grep, hilo, pid; s iniciar/parar, p pausa, x limpiar, e solo W+, t columna hilo, n columna proceso) ·
**3 Caché** (Enter abre sesión, f seguir, x borrar, o Finder) · **4 Procesos** (t hilos, l logs del proceso) ·
**5 Apps** (Enter/l logs, i inspeccionar, d base de datos) · **6 Grabar** · **7 Inspector** · **8 DB** · **9 Red** · **0 Consola**: un área donde **pegas un bloque** de comandos y `ctrl+r` (o ▶) lo ejecuta contra el dispositivo actual. Si las líneas no empiezan por `adb` van enteras a `adb shell` (varias líneas = script en el móvil); si empiezan por `adb` corren en el Mac con `adb` ya apuntando al dispositivo (p.ej. `adb shell getprop | grep -iE "…"`). Arriba hay una línea rápida con sugerencias. ctrl+g corta, ctrl+l limpia. `?` muestra la ayuda y el plan.

## Instalación

```bash
cd ~/AndroidStudioProjects/droid-cli && ./install.sh
```

Crea un venv propio, instala `rich` y enlaza `~/.local/bin/droid`. Para quitarlo: `./uninstall.sh`.
Requisitos: Python 3.9+ y `adb` (platform-tools). Si `adb` no se detecta: `droid config adb /ruta/a/adb`.

## Uso rápido

```bash
droid                      # = droid ls
droid ls -a                # incluye WiFi conocidos aunque no estén conectados
droid ls -w                # tabla en vivo: se refresca sola al conectar/desconectar (adb track-devices)
droid refresh              # re-detecta: adb reconnect offline + lista   (--hard reinicia el servidor adb)
droid alias 1 pixel        # nombre corto; sirve en todos los comandos

droid wifi setup pixel     # USB → WiFi (adb tcpip + connect); guarda IP y puerto
droid wifi connect pixel   # reconectar más tarde (o: droid wifi connect --all)
droid wifi pair 192.168.1.20:37123 123456   # Android 11+ "Depuración inalámbrica"
droid wifi disconnect pixel
droid wifi usb pixel       # volver a modo USB

droid logs                 # pregunta el dispositivo si hay varios
droid logs pixel -p com.example.app      # solo mi app (sigue reinicios del proceso)
droid logs pixel -p com.mi.app -l W -g "Retrofit|SQLite" -x "Choreographer"
droid logs pixel -t "OkHttp*" -t Room --show-tid --date
droid ps pixel                                   # procesos de apps por %CPU (--all: todos, -s mem)
droid ps pixel --threads -p com.mi.app           # hilos del proceso: TID, nombre, %CPU
droid logs pixel -p com.mi.app --thread main --thread "OkHttp*"   # solo esos hilos (columna con nombre)
droid logs pixel --pid 2981 --tid 3007           # por PID/TID exactos
droid logs pixel -p com.mi.app --show-thread     # columna con el nombre del hilo sin filtrar
droid logs pixel -n 200 -c        # últimas 200 líneas / vaciar buffer antes
droid logs pixel -b all           # todos los buffers (main, system, crash, events, radio)

droid cache                # dispositivos con logs guardados
droid cache ls pixel       # sesiones (fecha, duración, líneas, filtros, si se cerró bien)
droid cache show pixel                    # última sesión, con los mismos colores/filtros que logs
droid cache show pixel -s 2 -p com.mi.app -l E -n 100
droid cache show pixel -f                 # seguir una sesión en curso (p.ej. de record)
droid cache path pixel | droid cache open pixel
droid cache clean --older-than 30d        # o --keep 20 / --all (pide confirmación)

droid record start pixel   # graba en background; sobrevive a cerrar la terminal
droid record status
droid record stop --all

droid apps pixel           # apps de ~/AndroidStudioProjects instaladas: versión, debuggable, PID

droid inspect pixel -p com.mi.app                 # vista en vivo (Ctrl+C termina y resume)
droid inspect pixel -p com.mi.app -d 60s --thread "OkHttp*" --top 20
droid inspect pixel -p com.mi.app --json -d 30s > perf.jsonl   # para scripts
droid inspect --list / droid inspect --replay 1   # sesiones guardadas en ~/.droid/inspect

droid db pixel -p com.mi.app                      # DBs → tablas con filas y columnas
droid db pixel -p com.mi.app --table tasks --limit 20
droid db pixel -p com.mi.app --query "SELECT id, title FROM tasks WHERE done=0" --csv > pendientes.csv
droid db pixel -p com.mi.app --prefs              # shared_prefs/*.xml
droid db pixel -p com.mi.app --export ./snap      # copia la DB (aplica el -wal al cerrar: queda un .db autocontenido)

droid net pixel -p com.mi.app                     # tasa ↓↑ por interfaz, totales de la app, sockets
droid net pixel --sockets
droid run -d pixel 'adb shell getprop | grep -iE "_for_attestation|^\[ro\.product\.(brand|model|name|device|manufacturer)\]"'
                           # línea de shell local: `adb` ya apunta al dispositivo (-s serial); el grep corre en el Mac
droid run -d pixel 'getprop ro.product.model'      # sin `adb` delante se antepone `adb shell`
droid run --all -q 'adb shell getprop ro.build.version.release'   # en todos los dispositivos
droid run -d pixel '!ls ~/Desktop'                 # `!` = comando local sin tocar
droid shell pixel          # adb shell interactivo del dispositivo elegido
droid adb pixel reboot     # cualquier comando adb con -s resuelto
droid config               # ~/.droid/config.json
```

### Filtros por proceso e hilo

| Opción | Qué hace |
|---|---|
| `-p PKG` | por paquete; sigue al proceso aunque se reinicie (incluye `pkg:servicio`) |
| `--pid N` | por PID exacto (repetible) |
| `--tid N` | por TID exacto (repetible); muestra la columna TID |
| `--thread NOMBRE` | por nombre de hilo (`/proc/<pid>/task/*/comm`): substring o comodín (`main`, `OkHttp*`, `pool-*`); necesita `-p` o `--pid`. Muestra la columna de hilo |
| `--show-tid` / `--show-thread` | solo presentación, sin filtrar |

`droid ps` lista procesos (PID, usuario, %CPU, RSS) y `droid ps --threads -p PKG` los hilos con su nombre y %CPU para saber qué filtrar. Los nombres de hilo se guardan en la caché (`threads_seen`), así `droid cache show … --thread` funciona en post-mortem si la sesión usó `--thread` o `--show-thread`.

Selector de dispositivo en cualquier comando: índice de `droid ls`, serial (o prefijo), alias, modelo, IP, o `usb` / `wifi`. Si hay un solo dispositivo no pregunta.

## Colores en `logs`

- Nivel con fondo: `V` gris, `D` azul, `I` verde, `W` naranja, `E` rojo, `F` magenta. El mensaje de W/E/F va del color del nivel.
- Cada TAG tiene un color estable (hash), alineado a la derecha (`--tag-width`).
- `-g` filtra y resalta; `--hl` resalta sin filtrar. `--raw` da la línea cruda de logcat.
- `NO_COLOR=1` o `--color never` para tuberías; `--color always` para forzar.

## Caché post-mortem

- Ruta: `~/.droid/logs/<Modelo>-<serial>/<AAAAMMDD-HHMMSS>.log` + `.json` (dispositivo, filtros, inicio/fin, líneas, PIDs vistos, eventos).
- Se guarda **todo** lo que emite el dispositivo (los filtros solo afectan a lo que ves) para poder re-filtrar después con `droid cache show`.
- Si el dispositivo se desconecta o reinicia, `droid logs` espera, reconecta (incluso si vuelve por la otra vía USB↔WiFi) y sigue en el **mismo** archivo desde la última marca de tiempo. Los cortes quedan anotados como líneas `#droid …`.
- Una sesión "sin cerrar" en `droid cache ls` significa que `droid` murió sin despedirse (p.ej. `kill -9`); el log está igualmente en disco, se hace flush cada segundo.
- `droid logs` no duplica la caché si ya hay `droid record` activo para ese dispositivo.

## Inspector: qué mide y de dónde

| Sección | Fuente | Cada |
|---|---|---|
| CPU app y por hilo (% de un core) | `/proc/stat`, `/proc/<pid>/stat`, `/proc/<pid>/task/*/stat` | tick (1 s) |
| RSS anon/file/swap, nº hilos | `/proc/<pid>/status` | tick |
| PSS exacto | `run-as <pkg> cat /proc/<pid>/smaps_rollup` (solo debuggables) | tick |
| PSS total, Java/Native heap (alloc/size), Code, Graphics, Views, Activities | `dumpsys meminfo <pid>` | 10 s (es lento) |
| fps, jank %, p50/p90/p99, vsync perdidos, UI lenta | `dumpsys gfxinfo <pkg>` (deltas) | tick |
| Estado (primer plano / visible / en caché) | `/proc/<pid>/oom_score_adj` | tick |
| Salidas de la app: CRASH, ANR, LOW_MEMORY, SIGNALED… | `dumpsys activity exit-info <pkg>` | al arrancar y al morir |
| Red: ↓↑ por interfaz | `/proc/net/dev` (deltas) | tick |
| Red: totales por app (WiFi/móvil) | `dumpsys netstats detail` (uid) | 15 s |
| Red: sockets de la app | `/proc/net/tcp*`, `udp*` filtrado por uid | 15 s |
| DB | `run-as <pkg>` + `sqlite3` local sobre el snapshot (copia `.db`, `-wal` y `-shm`; al cerrar queda consolidado) | bajo demanda |

Cada snapshot queda en `~/.droid/inspect/<dispositivo>/db/<paquete>/<fecha>/`, así puedes comparar dos momentos con cualquier herramienta SQLite.

Limitación conocida: Android no expone bytes por app en tiempo real sin root; la gráfica por segundo es por interfaz y los totales por app se actualizan cuando el sistema los consolida.

## Estructura

```
droid/
  cli.py       comandos y parser
  adb.py       descubrimiento de adb, listado de dispositivos, tcpip/connect/pair
  ui.py        tablas rich, selector de dispositivo
  logs.py      parseo de logcat, filtros, colores, stream con reconexión
  cache.py     sesiones en ~/.droid/logs
  record.py    grabación en background (worker + pidfiles en ~/.droid/run)
  wifi.py      setup / connect / pair / usb
  registry.py  ~/.droid/devices.json (alias, IP, último visto)
  apps.py      applicationId de tus proyectos ↔ paquetes instalados
  theme.py     identidad de color (dispositivo, PID, hilo, tag, niveles, estados)
  suggest.py   sugerencias/autocompletado de los campos de la app (+ historial)
  runner.py    consola: shell local con `adb` envuelto para apuntar al dispositivo
  clip.py      portapapeles (pbcopy), recortes en ~/.droid/clips y extracción de texto limpio
  tui.py       app de terminal (Textual): pestañas 1-6
  tui_panes.py paneles Inspector / DB / Red (7-9)
  inspector.py muestreo CPU/RAM/frames/salidas + JSONL
  db.py        run-as + sqlite3
  net.py       /proc/net/dev, netstats por uid, sockets
docs/ROADMAP-inspector.md   plan (hecho: fases 0-3 básicas; pendiente: HTTP con proxy/agente, build analyzer, perfetto)
```
