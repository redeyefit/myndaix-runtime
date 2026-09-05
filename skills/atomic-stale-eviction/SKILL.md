---
name: atomic-stale-eviction
description: Prefer kernel flock; mv-evict stale locks, never rm-recreate
path_trigger: "tools/*.sh substrate/*.sh orchestrator/*.sh orchestrator/phone/*.sh"
---

Shell advisory locks come in three strengths — the phone surface walked all three in
one PR, so pin the ladder:

1. rm-then-recreate stale eviction: BROKEN (TOCTOU — two waiters both observe stale,
   both rm, the second rm destroys the first waiter's fresh lock; both enter the
   critical section; caps bypass, counters lose increments).
2. mv-eviction (`mv "$lock" "$lock.reaped.$$"` — one-winner rename, staleness re-checked
   adjacent to the mv): strictly better, but still check-then-act. Three review rounds
   each found a NARROWER race in it (mxr-phone r1 M-4/M-5 → r2 MED-4 → r3 HIGH-1) —
   the staleness check and the rename are separate ops, so the class never fully closes.
   If you ship this shape, state which residual it accepts.
3. kernel flock on a permanent lock FILE: ELIMINATES the class. A crashed holder's fd
   dies with its process, so there is no stale state to evict — no reaper, no eviction
   threshold to tune or drift. macOS has no flock(1); perl (always present) is the
   house route: a session-held lock lives on a bash-held fd (`exec 8>>file` + perl
   fdopen ">>&=" + LOCK_NB); a scoped critical section execs the child from perl while
   HOLDING the locked fd — and must CLEAR FD_CLOEXEC first, or perl closes the fd (and
   releases the lock) at execve and the section runs unlocked (r4 MAJOR-1, probe-proven).

Flag: any `rm -rf <lockpath>` on a staleness branch; mv-eviction presented as fully
atomic; NEW lock code choosing mkdir+reaper where flock fits; a perl flock wrapper that
execs without clearing FD_CLOEXEC; pid files written with `|| true` where write failure
still counts as acquisition.
