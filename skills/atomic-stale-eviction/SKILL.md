---
name: atomic-stale-eviction
description: Stale-lock eviction must be one-winner (mv), never rm-then-recreate
path_trigger: "*.sh"
---

The rm-then-recreate stale-lock pattern is a TOCTOU: two waiters can both observe a
stale lock (mkdir dir, flock-less file, slot dir), both `rm -rf` it, both recreate —
and the second rm can destroy the FIRST waiter's freshly acquired lock, so both enter
the critical section. Caps get bypassed, counters lose increments, "max N concurrent"
guarantees silently break.

The safe shape makes eviction a ONE-WINNER kernel op: `mv "$lock" "$lock.reaped.$$"`
(atomic rename — exactly one mover succeeds; losers see ENOENT and loop), THEN rm the
renamed corpse, THEN compete on a fresh mkdir. Re-check staleness (mtime/pid-liveness)
immediately adjacent to the mv, not cached from before a wait — a recreated lock has a
fresh mtime and must fail the staleness gate.

Flag: any `rm -rf <lockpath>` on a staleness branch followed by mkdir/create; staleness
decisions computed before a retry loop and reused after it; pid files written with
`|| true` where a write failure still counts as acquisition (an unownable lock leaks
until the next stale window).

(Learned 2026-09-03: both reviewer families independently flagged this pair in
mxr-phone's slot + cap locks — merged finding, fixed with the mv-eviction `_reap_dir`.)
