# Design: HTTP capture for `droid net`

**Status: not built yet, next release.** This document describes the plan for the option 1 path
already named in `docs/ROADMAP-inspector.md` (Fase 2): a local proxy, no code changes required in
the inspected app.

## Why a proxy and not an in-app agent

`docs/ROADMAP-inspector.md` lists two approaches for HTTP visibility. This design picks the proxy
approach first because it needs nothing added to the target app's build - it works against any
debuggable app already supported by the rest of `droid`, not only ones that pull in a `droid-agent`
Gradle dependency. The in-app interceptor stays on the roadmap as a later option for request/response
bodies and precise phase timings, which a proxy can only approximate.

## Distribution: optional extra

HTTP capture depends on `mitmproxy`, which is a heavier dependency than anything else `droid`
needs. It ships as an optional extra so the base install stays light:

```bash
pip install droid-cli[http]
```

`droid net --http` without the extra installed fails with a clear message telling the user to
install the extra, rather than failing to import at startup for everyone.

## How capture works

1. `droid net --http` spawns `mitmdump` as a subprocess, pointed at a bundled addon script that
   writes one JSON object per flow (request + response, once both are complete) to a JSONL file
   under `~/.droid/inspect/<device>/<date>/http.jsonl` - the same session directory shape the
   inspector already uses for its own JSONL samples.
2. The addon subscribes to `response` (not `request`) so a flow is only written once it is
   complete, avoiding half-written entries if the session ends mid-request.
3. Traffic is routed to the proxy without touching the app itself:
   - `adb reverse tcp:8080 tcp:8080` makes the device's `localhost:8080` reach the Mac's
     `mitmdump`.
   - `adb shell settings put global http_proxy 127.0.0.1:8080` points the device's global HTTP
     proxy setting at that forwarded port.
4. The `net` pane's Hosts/Events views read `http.jsonl` the same way the CPU/memory panes read
   their own sample stream, so a request shows up as an event on the same timeline as GC pauses,
   crashes, and exits.

## Cleanup

The device-wide proxy setting and the `adb reverse` tunnel are process state on the device, not on
the Mac, so they survive a `droid` crash. Cleanup has to be deliberate in more than one place:

- **Normal stop**: `droid net --http` unsets the proxy setting and removes the `adb reverse` rule
  when the session is stopped from the TUI (`s` to toggle it off) or the CLI process exits.
- **`quit` and `atexit`**: the same cleanup is registered with `atexit` and run on the app's quit
  path, so closing the TUI (`q`) or a normal Ctrl+C leaves the device clean.
- **Recovery**: if `droid` was killed with `SIGKILL` (or the Mac slept/crashed) before cleanup ran,
  the device is left with a stale proxy setting that breaks its network access even for unrelated
  apps. `droid net --http --stop` runs just the cleanup step - unset the proxy, remove the
  `adb reverse` rule - against a chosen device without starting a capture, so this is always
  recoverable without touching the device by hand.

## HTTPS

Plain capture only sees HTTP in the clear. For HTTPS:

- The device needs `mitmproxy`'s CA certificate installed as a **user** CA (`mitmproxy` serves it
  at `mitm.it` once the proxy setting is active).
- The inspected app needs to actually trust that user CA. Starting at `targetSdk` 24, apps do
  **not** trust user-installed CAs by default - only system CAs - unless the app ships a
  `network_security_config` that opts back in (a `<trust-anchors>` entry with
  `<certificates src="user"/>`, typically scoped to debug builds). This is common in debug builds
  of apps built for local proxying, but it is not automatic, and `droid` cannot add it to a third
  party's build. HTTPS capture is opt-in per app, and the design has to document this limitation
  rather than silently show an empty request list when it is not met.

## Export

Once capture works, `droid net --http --export session.har` (or an equivalent TUI action) converts
the session's `http.jsonl` into a HAR 1.2 file, so a capture can be opened in any HAR-aware tool
(browser dev tools, Charles, Postman) instead of only `droid`'s own views.
