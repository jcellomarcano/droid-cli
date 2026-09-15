# Roadmap: single-binary distribution

Not built in 0.2. This document is a plan, not a commitment to a specific release.

## What it is for

Today `droid` needs a Python interpreter and a venv (`install.sh`). That is fine for a personal
machine, but it is one more thing to set up. A single self-contained binary would let someone drop
one file on a Mac (or Linux box) and run `droid ls` with nothing else installed.

## Priority order

1. **macOS first.** That is the only platform this tool is used on today. Get a single macOS
   binary working before anything else.
2. **macOS + Linux.** Once the macOS build is solid, add Linux, since the Python CLI already
   supports it and CI already tests it.
3. **+ Windows.** Lowest priority. Nothing about `droid` is Windows-specific today (no adb-only
   assumption that would block it), but it is not a target until macOS and Linux are done.

## Language candidates

- **Go** - favourite for readability. Straightforward concurrency for the live TUI panes
  (inspector, net, logs), a mature ecosystem for talking to `adb` as a subprocess, single static
  binary by default.
- **Rust** - stronger performance and memory guarantees, more ceremony. Worth benchmarking even
  though it is not the front-runner, because the inspector's sampling loop is exactly the kind of
  workload where it could matter.
- **Kotlin/Native** - attractive on paper for a project about Android tooling, but Kotlin/Native's
  cross-platform binary story and TUI library maturity are the weakest of the three. Included for
  completeness, not because it is likely to win.

## How the decision gets made

Not by preference. Before committing to a language, build a small prototype in each candidate that
reimplements a representative slice (device listing + one live logcat pane) and measure:

- **Startup time** - cold start to first rendered frame, since `droid` is invoked constantly from
  a shell.
- **Binary size** - a single-binary pitch loses its point if the binary is enormous.
- **adb interop** - how painless it is to spawn `adb`, stream its stdout, and handle
  reconnects/timeouts the way `droid/adb.py` and `droid/logs.py` do today.
- **TUI library maturity** - does the candidate have something comparable to Textual (tabs, live
  tables, sparklines, an autocomplete overlay) or would the TUI have to be rebuilt from primitives.

Whichever candidate wins the benchmark is the one that gets built out. The Python CLI stays the
behavioural reference for as long as this migration takes: any new binary has to match its
behaviour, and its test suite (`tests/`) is the oracle that behaviour is checked against, fixture
by fixture. The Python CLI does not get deprecated until the replacement passes that suite.
