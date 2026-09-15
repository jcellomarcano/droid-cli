# Roadmap: remote ADB hosts (device farms over WiFi)

Status: not built in 0.2. Design written on 2026-09-15 for the next releases. The owner's first
target: a Raspberry Pi Zero on a home WiFi with a Pixel 4 XL on the same network; the laptop
reaches the Pi through a server the Pi is exposed on, and from there installs APKs, streams logs,
opens a shell and runs tests with `droid`, as if the phone were plugged in.

## What must stay true

- INV-R1: nobody except the owner's keys can reach a device through the farm. The ADB protocol
  itself has no authentication between client and server, so the server port (5037) and the
  device port (5555) are never exposed beyond the Pi itself.
- INV-R2: every byte between the laptop and the Pi travels encrypted (SSH or WireGuard).
- INV-R3: an APK install or a shell command through the farm leaves a line in an audit log on
  the Pi with who, when and what.
- INV-R4: the farm never weakens the phone: no `adb root`, no disabling of verification, no
  ADB over TCP left enabled on a phone that leaves the test network.

## Architecture (recommended)

```
laptop ── SSH (key only) ──> Pi (adb server bound to 127.0.0.1:5037) ── USB or WiFi ──> Pixel 4 XL
              │                       │
              └── ssh -L 5037:127.0.0.1:5037 ──> droid uses ADB_SERVER_SOCKET=tcp:127.0.0.1:5037
```

1. The Pi runs the ADB server as a systemd service: `adb -a nodaemon server start` is NOT used
   (`-a` binds all interfaces); the default `adb start-server` binds 127.0.0.1 only.
2. The laptop opens an SSH tunnel that forwards a local port to the Pi's 5037. `droid` sets
   `ADB_SERVER_SOCKET=tcp:127.0.0.1:<port>` (or `adb -L`) for every call, so every existing
   feature (logs, cache, inspector, files, db, net, install) works unchanged: the local `adb`
   binary is only a client talking to the remote server.
3. The phone is attached to the Pi by USB (OTG cable; the Pi Zero cannot charge it, so keep a
   powered hub) or by WiFi debugging inside the test network. WiFi debugging on Android 11+ uses
   pairing (`adb pair`) with a code shown on the phone; on Android 10 and older it needs one
   USB connection to run `adb tcpip 5555`, and then the port stays open until reboot.
4. Exposure of the Pi to the outside: prefer a mesh VPN (Tailscale or plain WireGuard) over
   port forwarding on the router; if a jump server is used, the laptop goes
   `ssh -J jump pi` and never exposes the Pi's SSH to the internet directly.

Why not `adb connect <pi>:5037` or `adb connect <phone>:5555` over the internet: both are plain
TCP with no authentication; the phone only checks the RSA key of the *adb server host* (the Pi),
so anyone who reaches the Pi's server gets a device that already trusts it, with no prompt.

## Hardware notes

- Pi Zero (v1, ARMv6): Google ships no platform-tools for it and it is slow; expect to build
  `adb` from source or use a distro package. Pi Zero 2 W (ARMv8, 64-bit) runs Debian's
  `adb` package or the official ARM64 platform-tools and is the practical choice.
- One USB OTG port: one phone by USB; more phones by WiFi debugging or a powered hub.
- SD card wear: logs and caches go to tmpfs or are rotated; `droid` caches stay on the laptop
  (the client), not on the Pi.

## Phases

### P0: manual proof (no code)
`ssh -L 5037:127.0.0.1:5037 pi@<host>` in one terminal, then
`ADB_SERVER_SOCKET=tcp:127.0.0.1:5037 droid ls` in another. Success criterion: the Pixel 4 XL
appears as `USB` or `WiFi`, `droid logs` streams and `droid adb install app-debug.apk` works.
Measure: latency of `adb shell echo`, logcat throughput, install time of a 30 MB APK.

### P1: `droid farm` (client side)
- `droid farm add <name> ssh://pi@host[:port] [--jump host] [--identity key]`,
  `droid farm ls`, `droid farm rm`.
- `droid --farm <name> <any command>` and `droid farm use <name>` (sticky): `droid` opens the
  tunnel itself (`ssh -N -L` as a child process, or an existing Tailscale address), waits for
  the port, exports `ADB_SERVER_SOCKET` for the whole process and closes the tunnel on exit.
- The device list shows the farm name and the transport as seen by the Pi.
- Post-mortem cache and inspector sessions are keyed by device as today; the farm name is in
  the session meta.
- Tests: fake `ssh` script in tmp_path; no real network in the suite.

### P2: Pi setup script (server side)
`scripts/farm-setup.sh` run on the Pi: installs `adb`, udev rules for the phone vendor ids, a
systemd unit for the ADB server bound to localhost, SSH hardened (key only, no password, no
root login), `unattended-upgrades`, optional Tailscale, an `adb-audit` wrapper that logs every
`install`, `shell` and `push` to `/var/log/droid-farm/audit.log` with the SSH key fingerprint.
Also documents WiFi debugging pairing and how to revoke the Pi's key on the phone.

### P3: farm jobs
`droid farm run <name> --apk app-debug.apk --test "am instrument ..."`: install, run an
instrumentation or a monkey pass, pull logs and a `droid inspect` session, report in JSON. Several
phones on the same Pi run in sequence; several Pis in parallel.

### P4 (optional): an agent instead of a tunnel
Only if the tunnel model becomes a limit (browser access, non-SSH clients): a small service on
the Pi with mutual TLS exposing list, install, logcat and shell as streams. It must not replace
the SSH model without a security review, because it re-implements what SSH already gives.

## Security implications, in full

| Risk | Where | Mitigation |
|---|---|---|
| ADB server port reachable from the network gives full device control without any prompt | Pi 5037, phone 5555 | bind 5037 to localhost; never port-forward 5037 or 5555 on the router; firewall the Pi (`ufw` default deny, allow SSH and the VPN only) |
| The Pi's ADB RSA key (`~/.android/adbkey`) is the trust anchor of the phone | Pi filesystem | file mode 600, encrypted SD or at least no shared users on the Pi; revoke on the phone ("Revoke USB debugging authorizations") when the Pi is retired |
| SSH exposure | Pi 22 | key-only, `PasswordAuthentication no`, `PermitRootLogin no`, non-default port does not add security but reduces noise, `fail2ban`, or no exposure at all with a mesh VPN |
| APK install is remote code execution on the phone | `adb install` | only the owner's keys; audit log; install only APKs signed with the owner's debug key; never install from a URL the farm fetches itself |
| Logs, databases and shared_prefs carry secrets (the app prefs cipher key is one example) | logcat, `droid db`, `droid files` | they travel only inside the tunnel; `droid` caches land on the laptop, not on the Pi; the prefs warning of the Archivos tab stays |
| Pixel 4 XL no longer receives security updates | the phone | keep it on an isolated WiFi (guest network or VLAN) with no access to the home LAN; test accounts only, no personal accounts on it |
| WiFi debugging left enabled | phone 5555 | `adb usb` or reboot after the session; on Android 11+ wireless debugging turns itself off on reboot |
| A compromised Pi becomes a pivot into the home network | Pi | separate VLAN for Pi and phones; the Pi has no credentials for anything else; unattended upgrades |
| Loss of the Pi (theft) | Pi | nothing on it but the ADB key and the audit log; no caches, no APKs kept after install |
| Supply chain of the Pi image | SD card | flash official Raspberry Pi OS Lite, verify the checksum, change the default user before first network access |

## What is NOT built (SIMP-6)
No web UI, no multi-tenant farm, no scheduling across sites, no device reservation system, no
root or custom recovery on the phones, no exposure of 5037 through TLS proxies.

## Open questions for the owner
- Pi Zero v1 or Zero 2 W? (decides whether `adb` runs at all without building it).
- Mesh VPN (Tailscale) or SSH through the existing exposed server?
- Does the Pixel 4 XL stay by USB on a powered hub or by WiFi debugging with a charger?
