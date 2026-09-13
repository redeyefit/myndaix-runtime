---
name: fail-open
description: Gates that default OPEN on the unhandled path keep recurring
path_trigger: orchestrator/*.sh src/runtime/*.py substrate/*.sh
---

A security or merge GATE whose error/unknown branch lets the action proceed is fail-open — this
repo's reviews keep re-finding the same class: `2>/dev/null || true` on a check that guards a
mutation; an empty or unreadable allowlist treated as "allow everything"; a classifier/parse
error mapped to "no decision" and the caller proceeding; corrupted state files loaded as empty
(silently discarding the reservations they held); a missing config that skips the guard instead
of the action.

Preferred pattern — default DENY on every unhandled branch of a gate:
- Enumerate what is allowed; anything else — including "couldn't tell" — is denied. No decision
  → the caller fails closed.
- On a read/parse error of gate state: PRESERVE the evidence (never overwrite with a clean
  default) and disable the acting path; read-only paths may continue.
- Distinguish the two kinds of code before writing the error branch: INSTRUMENTATION may fail
  open (observability must never break the caller); GATES must fail closed. Most recurrences
  here are gate code written with an instrumentation reflex.
- Never suppress a gate's stderr; a silent failure IS the open door.

Flag any change matching the trigger where an error, empty, or unknown branch of a gate lets the
gated action happen.

(Promoted from recurring reviewer findings [fail-open]. Provenance: origin_repo=myndaix-runtime;
finding_ids=3a6c76fd7a02c51fc2daba06b0b2ee0811187663, a982bcbfd765e64de85d01b410c6524b8a1b84bf,
2c5777978e68d7789b41e175fdf7f4f9fef70664.)
