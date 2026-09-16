---
name: toctou-race
description: A check-then-act sequence with a concurrency window between the check and the act
path_trigger: "**/*.swift **/*.ts **/*.tsx **/routes/**/*.ts **/api/**/*.ts orchestrator/*.sh substrate/*.sh tools/*.sh"
---

`SELECT` to confirm a resource exists, then `DELETE` or mutate it in a separate call is a
TOCTOU race: between the check and the act, another request can delete or modify the
resource, leaving the second call with stale assumptions. The same shape appears in any
"does it exist?" → "now act on it" pair: fetch-then-update, stat-then-write, load-then-evict.

Concrete failure: a DELETE route fetches a project to distinguish 404 (gone) from 403
(unauthorized). A concurrent deletion between the SELECT and the RPC makes the RPC return
0 rows. The route interprets "0 rows deleted" as "not authorized" and returns 403 — the
wrong code, leaking that the resource existed.

The safe shape collapses check and act into one atomic operation:
- SQL: a single `DELETE … WHERE id = $1 AND owner_id = $2 RETURNING id` — the row count
  tells you both authorization and existence in one round-trip; no intermediate state.
- Swift/API: optimistic locking (`WHERE updated_at = $known`) or conditional writes that
  fail loudly on stale data rather than silently succeeding on a ghost.

Flag: any fetch whose SOLE purpose is to inform a subsequent mutation; any "not found after
mutation" path that returns a permission error; any stat/existence check before a write.

(Captured 2026-09-12→14: oracle flagged 404/403 race in FieldVision project deletion route;
kilabz flagged the same pattern in myndaix-runtime lock/eviction paths.)
