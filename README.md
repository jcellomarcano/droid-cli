# droid

A personal command-line tool for Android development on top of `adb`. It manages devices over USB
and WiFi, streams colored logcat with filters, keeps every log session in a post-mortem cache, and
adds a live in-terminal inspector for CPU, memory, frames, crashes, network, files and SQLite
databases of your own debuggable apps.

The interactive app (`droid` with no arguments) is a terminal UI built with
[Textual](https://textual.textualize.io/). Every tab also has a non-interactive subcommand for
scripts and CI. The UI itself is in Spanish; this document quotes its labels and shortcuts as they
appear on screen, so you will see Spanish words inside the English explanations below.

## What it is

`droid` wraps `adb` with the day-to-day workflow of building and debugging Android apps from a
Mac or Linux terminal: listing devices, following logs with filters that survive process restarts,
recording sessions that outlive the terminal, and inspecting a running app's CPU, memory, network,
files and database without attaching a debugger.

## What it is for

It is built around **your own projects**. Most commands that need to pick an app (`droid apps`,
`droid inspect`, `droid db`, `droid net`, `droid files`) look at `applicationId` values found under
a projects directory (`~/AndroidStudioProjects` by default, see Configuration) and match them
against installed, debuggable packages on the device. That keeps the tool's app-facing views
scoped to apps you actually built, instead of every package on the phone.

## Requirements

- macOS or Linux. (There is no Windows support; see Limitations.)
- Python 3.9 or newer.
- `adb`, from Android's platform-tools.

On a clean Mac, get both with Homebrew:

```bash
brew install python@3.11
brew install --cask android-platform-tools
```

`adb` also comes with any Android Studio install (inside its SDK's `platform-tools/`); if you
already have Android Studio, you don't need the cask. If `droid` cannot find `adb` on `PATH`,
point it at one explicitly:

```bash
droid config adb /path/to/adb
```

## Install, step by step

```bash
git clone https://github.com/jcellomarcano/droid-cli.git
cd droid-cli
./install.sh
```

What `install.sh` does to your machine:

1. Picks a Python interpreter: `PYTHON=/path/to/python3.x ./install.sh` forces one; otherwise it
   tries `python3`, then `python3.13`, `3.12`, `3.11` on `PATH`, then the usual Homebrew keg paths,
   and stops at the first one that is 3.9 or newer.
2. Creates a private virtual environment at `.venv` inside the repository (`python -m venv
   --clear`), so it never touches a system or user-wide Python.
3. Installs `droid` into that venv in editable mode, pulling in `rich`, `textual` and
   `textual-autocomplete`.
4. Symlinks `~/.local/bin/droid` to `.venv/bin/droid` - this is the only thing that goes outside
   the repository and outside `.venv`.
5. If `~/.local/bin` is not already on `PATH`, appends a line exporting it to `~/.zshrc` (or
   `~/.bashrc` if your shell is bash). This only edits your shell rc file when the directory is
   missing from `PATH`; it does not touch it otherwise.
6. Creates `~/.droid/logs` and `~/.droid/run` (see Where data lives).

Then open a new terminal (so the `PATH` change takes effect) and run:

```bash
droid ls
```

## First run

With no arguments, `droid` opens the terminal UI (equivalent to `droid ui`). If no interactive
terminal is available, or you prefer scripting, every feature is also a direct subcommand - start
with `droid ls` to see connected devices, or `droid --help` for the full command list.

## Commands

| Command | What it does |
|---|---|
| `droid ls` (`devices`) | list connected devices, USB and WiFi |
| `droid refresh` (`reconnect`) | re-detect devices (`adb reconnect offline`; `--hard` restarts the adb server) |
| `droid wifi` | set up/connect/pair/disconnect a device over WiFi |
| `droid logs` (`log`, `logcat`) | live logcat with colors (and automatic caching) |
| `droid cache` | saved log sessions per device (post-mortem) |
| `droid record` | record logcat in the background, survives closing the terminal |
| `droid alias` | give a device a short name |
| `droid apps` | your projects' apps installed on the device (debuggable) |
| `droid ps` | processes and threads on the device |
| `droid inspect` | live inspector: CPU per thread, RAM, frames/jank, app exits |
| `droid db` | SQLite databases of a debuggable app: tables, rows, SQL, CSV, prefs |
| `droid files` | file explorer: app sandbox (`run-as`) and `/sdcard`, `/data/local/tmp`, `/proc/<pid>` |
| `droid net` | network: live per-interface rate, per-app totals, open sockets |
| `droid shell` | interactive `adb shell` on the chosen device |
| `droid run` (`x`) | one shell line with `adb` already pointed at the device |
| `droid adb` | run `adb -s <device> <args...>` |
| `droid ui` (`app`, `tui`, `menu`) | open the terminal app (same as `droid` with no arguments) |
| `droid config` | view/change configuration (`~/.droid/config.json`) |

Every command that takes a device accepts an index from `droid ls`, a serial (or prefix), an
alias, a model name, an IP, or the literal `usb` / `wifi`. If there is exactly one device, you are
not asked to pick.

Full flags for any command: `droid <command> --help`.

## The terminal UI and its keys

Tabs, keys `1`-`9` and `0`, plus `F`:

`1` Dispositivos (Devices) · `2` Logs · `3` Caché (Cache) · `4` Procesos (Processes) · `5` Apps ·
`6` Grabar (Record) · `7` Inspector · `8` DB · `9` Red (Network) · `0` Consola (Console) ·
`F` Archivos (Files)

### Global keys

| Key | Action |
|---|---|
| `1`-`9`, `0`, `F` | switch tab |
| `?` | help screen |
| `y` | copy the selection, or the visible lines if nothing is selected (in a table, the row under the cursor) |
| `Y` | copy the whole panel or table (TSV with a header row) |
| `v` | open the viewer: frozen text for reading and trimming calmly |
| `r` / `F5` | refresh |
| `R` | force `adb reconnect offline` for stuck devices |
| `ctrl+d` | next device |
| `esc` | leave the focused field |
| `q` | quit |

The device list refreshes itself when a device connects or disconnects.

**Selecting and copying text:** drag with the mouse over any log panel, then `ctrl+c` (or `cmd+c`)
copies to the Mac clipboard through `pbcopy` - this works in Terminal.app too, which ignores the
terminal's own OSC 52 copy method. Lines that were wrapped by the screen width are rejoined on
copy, so what you paste is whole log lines. In the viewer (`v`): mouse or `shift+arrows` selects,
`a` selects all, `c` copies (selection or everything), `g` saves the clip to `~/.droid/clips/`, `e`
opens it in your editor, `w` toggles line wrap, `Esc` closes. In live logs, `v` pauses the stream
so you can trim without it scrolling (`p` resumes it). What gets copied is clean text: the raw
logcat line, no colors or box characters, ready to paste into a ticket or pipe through `grep`. To
use the terminal's own selection instead of the app's, hold ⌥ (Option) while dragging.

**Autocomplete everywhere:** entering any field shows suggestions droid already knows about
(your projects' apps marked with ★, live processes with their PID, installed packages, threads of
the chosen process, levels, tags seen in the log, tables and columns of the open database) plus
your recent values (`~/.droid/history.json`). `↑↓` picks, `Tab` or `Enter` completes, `Esc` hides
it, typing filters. Fields that take several values (packages, threads, PIDs) complete the last
token.

### Per-tab keys

**1 Dispositivos** - `Enter` view logs · `space` select as current · `w` USB→WiFi · `c` connect · `d`
disconnect · `u` back to USB · `a` alias

**2 Logs** - filter bar: package, level, grep, thread, pid · `s` start/stop · `p` pause · `x` clear ·
`e` errors+warnings only · `t` thread column · `n` process column

**3 Caché** - `Enter` open session · `f` follow live · `x` delete session · `o` open folder in Finder

**4 Procesos** - `t` threads of the process · `l` logs of the process

**5 Apps** - `Enter`/`l` logs of that app · `i` inspect · `d` database

**6 Grabar** - `s` record current device · `S` record all · `x` stop selected · `X` stop all

**7 Inspector** - `s` start/stop · `l` logs of the thread · `m` meminfo now · `e` app exits · `f`
switch app · `o` Resumen (Overview) view · `u` Memoria (Memory) view · `h` Salud (Health) view

**8 DB** - `Enter` on a database: snapshot · `Enter` on a table: rows · `/` SQL · `x` CSV · `p`
shared_prefs · `r` reload · `b` back to the table list

**9 Red** - `s` start/stop · `r` sockets · `f` switch app · `h` focus hosts · `e` focus events · `k`
focus sockets

**0 Consola** - a block area: paste one or more commands and `ctrl+r` (or the ▶ button) runs them.
Lines that do not start with `adb` go whole to `adb shell` on the current device (several lines =
a script run on the phone); lines starting with `adb` run on the Mac with `adb` already pointed at
the device (e.g. `adb shell getprop | grep -iE "…"`). There is also a quick one-line field above
with suggestions. `ctrl+g` kills a running command, `ctrl+l` clears the output, `f` focuses the
quick line, `b` focuses the block.

**F Archivos** - `backspace` go up a directory · `r` reload · `p` pull to disk · `v` preview a file
· `b` open a `.db` file directly in the DB tab · `x` delete (asks for confirmation) · `f` focus the
package field

## Post-mortem log cache

- Path: `~/.droid/logs/<Model>-<serial>/<YYYYMMDD-HHMMSS>.log` plus a `.json` sidecar (device,
  filters used, start/end time, line count, PIDs seen, events).
- Everything the device emits is saved (filters only affect what you see live), so you can
  re-filter later with `droid cache show`.
- If the device disconnects or reboots, `droid logs` waits, reconnects - even if it comes back
  over the other transport, USB↔WiFi - and keeps appending to the **same** file from the last
  timestamp. Gaps are annotated as `#droid …` lines.
- A session marked "unclosed" in `droid cache ls` means `droid` died without saying goodbye (e.g.
  `kill -9`); the log is still on disk, since it is flushed every second.
- `droid logs` does not duplicate the cache if `droid record` is already recording that device.

## Inspector

`droid inspect <device> -p <package>` opens a live view sampled once a second (configurable with
`--interval`), with an optional fixed duration (`--duration 30s`, `5m`) and JSON output for
scripts (`--json`). Sessions are recorded to `~/.droid/inspect/` unless you pass `--no-record`.

Review a saved session with `droid inspect --list` (or `--replay <file|#>`); add `--chart` to get
sparklines and histograms instead of a text summary.

In the TUI, the Inspector tab (`7`) has three views, switched with `o`/`u`/`h`:

- **Resumen** (Overview, `o`) - CPU per thread, RSS/PSS, Java/Native heap, code/graphics, view and
  activity counts, fps and jank percentiles.
- **Memoria** (`u`) - see Memory below.
- **Salud** (`h`) - see Health below.

### What each metric comes from

| Section | Source | Cadence |
|---|---|---|
| App and per-thread CPU (% of one core) | `/proc/stat`, `/proc/<pid>/stat`, `/proc/<pid>/task/*/stat` | every tick (1 s) |
| RSS anon/file/swap, thread count | `/proc/<pid>/status` | every tick |
| Exact PSS | `run-as <pkg> cat /proc/<pid>/smaps_rollup` (debuggable apps only) | every tick |
| Total PSS, Java/Native heap (alloc/size), Code, Graphics, Views, Activities | `dumpsys meminfo <pid>` | every 10 s (it is slow) |
| fps, jank %, p50/p90/p99, missed vsyncs, slow UI | `dumpsys gfxinfo <pkg>` (deltas) | every tick |
| State (foreground / visible / cached) | `/proc/<pid>/oom_score_adj` | every tick |
| App exits: CRASH, ANR, LOW_MEMORY, SIGNALED... | `dumpsys activity exit-info <pkg>` | on start and on death |
| Network: rate per interface | `/proc/net/dev` (deltas) | every tick |
| Network: per-app totals (WiFi/mobile) | `dumpsys netstats detail` (by uid) | every 15 s |
| Network: app sockets | `/proc/net/tcp*`, `udp*` filtered by uid | every 15 s |
| Database | `run-as <pkg>` + local `sqlite3` over a snapshot (copies `.db`, `-wal` and `-shm`; consolidated on close) | on demand |

## Health (Salud)

The Salud view (`7`, then `h`) groups everything under `dumpsys activity exit-info` - crashes,
ANRs and process deaths - by **stack signature**, not by raw timestamp, so repeats of the same
underlying failure collapse into one row with a count instead of flooding the list. Each group
shows its type, signature, title, how many times it happened, and when it last happened, with a
timeline sparkline above the table. Keys: `l` opens the logs around that pid, `e` refreshes exit
data on demand, `o` goes back to Resumen.

## Memory (Memoria)

The Memoria view (`7`, then `u`) tracks Java heap, Native heap and total PSS over the session, and
overlays ART GC events parsed from logcat (frequency, bytes freed, pause).

**Leak heuristic.** For each of `java_heap_kb`, `native_heap_kb` and `pss_total_kb`, droid fits a
least-squares slope over the sampled points and flags a metric as a possible leak when *all* of
these hold:

- at least 6 samples, spanning at least 60 seconds;
- the slope is positive (memory is trending up, not down or flat);
- total growth over the window is at least 5% of the first value, or 2048 KB, whichever is larger;
- if a GC ran inside the window, the value right after the last GC is still meaningfully higher
  than the value at the start of the window (otherwise a GC that simply hadn't run yet would look
  like a leak).

**Limits.** This is a heuristic over a fixed window, not a diagnosis: it is labeled "Inferido"
(Inferred) in the data, not "confirmed". A short session, a slow leak, or a workload that legitimately
allocates more memory over time (loading more data, opening more screens) can all produce the same
slope. Treat a flag as "worth a longer look with the same filters", not as proof of a bug.

## Database

`droid db <device> -p <package>` snapshots an app's SQLite databases through `run-as` (copying the
`.db` file plus `-wal`/`-shm` so nothing mid-transaction is lost) and lets you browse tables, run
arbitrary SQL against the local copy, export CSV, or dump `shared_prefs/*.xml`. `--export DIR`
copies the consolidated database out to disk. In the TUI (tab `8`), `Enter` on a database takes a
snapshot, `Enter` on a table shows rows, `/` opens a SQL field, `x` exports CSV, `p` shows prefs.

## Network

`droid net <device> [-p <package>]` shows a live per-interface rate, socket list, and per-app
totals. Without `-p` it reports the whole device; with it, droid also asks `dumpsys netstats
detail` for that app's WiFi/mobile totals.

**What "per app" means without root:** Android does not expose live per-app byte counters without
root (the old `xt_qtaguid` interface is gone on current devices). The **rate graph is always
per-interface**, sampled every tick; the **per-app totals** come from `dumpsys netstats detail`
and only update when the system consolidates them, roughly every 15 seconds - the TUI labels this
explicitly rather than implying a live per-app rate. Sockets (`/proc/net/tcp*`, `udp*` filtered by
the app's uid) refresh on the same 15-second cadence, or on demand with `r` / `--sockets`.
`droid net --sockets` on its own just lists sockets and exits.

There is no HTTP-level capture (URLs, methods, bodies) in this release; see Limitations and
`docs/design/http-capture.md` for the plan.

## Files

`droid files <device> [PATH]` (TUI tab `F`) browses four roots: an app's sandbox (via `run-as`,
selected with `-p`), `/sdcard`, `/data/local/tmp`, or a process's `/proc/<pid>` (with `--pid`).
`--cat` prints a file as a preview, `--pull DEST` downloads it, `--json` lists a directory as JSON.
`--rm` deletes the path at `PATH` and asks for confirmation unless you pass `-y`/`--yes`; in the
TUI, `x` does the same and always confirms first. `b` opens a `.db` file directly in the DB tab
without leaving Files.

## Console

Tab `0` (`droid run`/`droid shell`/`droid adb` are its CLI equivalents) is a place to paste a
block of commands and run them against the current device. Lines that do not start with `adb` are
sent whole to `adb shell` (multiple lines become a script executed on the phone); lines that do
start with `adb` run on the Mac, with `adb` already resolved to `-s <serial>` for the current
device - useful for piping through a local `grep`, e.g. `adb shell getprop | grep -iE "…"`. A
quick one-line field above the block offers the same with autocomplete suggestions. `droid run -d
<device> '!ls ~/Desktop'` runs a line locally, unmodified, when it starts with `!`.

## Configuration

`droid config` prints the current configuration; `droid config <key> <value>` changes one key.
Stored at `~/.droid/config.json`.

| Key | Default | Meaning |
|---|---|---|
| `adb` | `""` (autodetect) | path to the `adb` binary |
| `wifi_port` | `5555` | port used for `adb tcpip` |
| `projects_dir` | `~/AndroidStudioProjects` | where `droid apps` and friends look for your projects' `applicationId` |
| `tag_width` | `22` | width of the TAG column in `droid logs` |
| `reconnect` | `true` | retry automatically when a device is lost |
| `ui_on_empty` | `true` | whether `droid` with no arguments opens the terminal app |

## Where data lives

```
~/.droid/
  config.json      configuration (see above)
  devices.json     known devices: alias, IP, last seen
  history.json      recent values per field, for autocomplete
  logs/            post-mortem log cache, per device
  run/             pidfiles for background `droid record` workers
  inspect/         saved inspector sessions (JSONL) and DB snapshots
  clips/           text clips saved from the viewer (`g`)
```

## Uninstall

```bash
./uninstall.sh
```

Removes the `~/.local/bin/droid` symlink and deletes `.venv`. The `~/.droid` cache is kept; delete
it by hand if you want it gone too (`rm -rf ~/.droid`).

## Limitations

- No HTTP-level capture (URLs, methods, bodies) in this release. The plan is a local proxy
  (`mitmproxy`) - see `docs/design/http-capture.md` - not built yet.
- No live per-app network byte rate without root; see Network above for exactly what is and is not
  live.
- Test fixtures are captured from one emulator; behavior against other devices/Android versions is
  not covered by the automated tests, only used in practice.
- No root is used anywhere; every feature that needs app-internal data relies on `run-as` and only
  works against debuggable builds.
- The Python 3.9 floor is only verified by CI (see `.github/workflows/ci.yml`), not by hand on an
  actual 3.9 install.

## Roadmap

- [docs/ROADMAP-inspector.md](docs/ROADMAP-inspector.md): what shipped in 0.2 and what is next in the inspector (HTTP capture is designed in [docs/design/http-capture.md](docs/design/http-capture.md), build analyzer, deep traces).
- [docs/ROADMAP-remote-adb.md](docs/ROADMAP-remote-adb.md): remote ADB hosts and small device farms over WiFi (a Raspberry Pi with phones on the same network, reached through an SSH tunnel), with the security analysis.
- [docs/ROADMAP-bundle.md](docs/ROADMAP-bundle.md): a compiled single-binary distribution, macOS first, then Linux, then Windows.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md).

## License

MIT - see [`LICENSE`](LICENSE).
