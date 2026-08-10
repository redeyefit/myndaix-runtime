# substrate/lab — jobs that run on the LAB (MacBook), hand-managed

The factory's substrate (reconcile + rogue sweep + liveness-canary) governs the Mini only.
Anything here is installed BY HAND on the MacBook and is deliberately outside that loop.

## tailnet-watch

The lab watches the factory: probes the Mini's tailnet node (ssh port, then `tailscale ping`)
every 10 min. Three consecutive failures → macOS notification (re-alerted at most once/day),
recovery notice on heal. Exists because of the 2026-07-18→08-09 incident: the Mini ran dark
off the tailnet for 22 days while its own liveness-canary stayed green (every LOCAL job was
healthy — reachability had no watcher). The Mini is now also the offsite backup mirror, so a
dark Mini = silently single-copy.

Design guards (lessons from the liveness-canary audit): per-signal latch with daily re-arm
(no spam, no permanent silence), atomic state writes, `10#` base-10 normalization on all
numerics read from state, corrupt-state fail-safe, BSD-only (`date -r`) — this is a
macOS-lab-only script by design.

### Install (once, on the MacBook)

```
mkdir -p ~/.myndaix/state   # launchd opens the plist's stdout/err paths BEFORE the script's own mkdir
cp substrate/lab/ai.myndaix.tailnet-watch.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.myndaix.tailnet-watch.plist
```

Note: `tailscale ping -c 1` is verified against the installed GUI-app CLI (1.90.x) — docs
list variants; trust the binary.

### Verify / operate

```
bash substrate/lab/test.sh                      # hermetic suite
launchctl list | grep tailnet-watch             # loaded?
tail ~/.myndaix/state/tailnet-watch.log         # transitions + alerts
TW_HOST=192.0.2.1 TW_FAIL_THRESHOLD=1 bash substrate/lab/tailnet-watch.sh  # live alert drill
```

Residual (accepted): detection requires the MacBook to be awake — worst case is hours, not
weeks. The 24/7 upgrade path is a healthchecks.io dead-man ping from the Mini (needs a Jefe
account); deliberately not built.
