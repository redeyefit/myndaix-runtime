# Controller-Loop ("the brain") — DESIGN v0.2

_North-star rung 3. v1 = **proactive review scheduler**, a bounded level-triggered reconciler with a small durable cursor. No LLM judgment, no auto-fix, no auto-merge, no learning (later rungs). Prior-art basis: `docs/controller-loop-research.md`. Status: **revised after cross-family design review** (Oracle APPROVE-WITH-FIXES, codex NEEDS-REVISION); reviews in `docs/reviews/controller-loop-design-v0.1-*.md`._

**Decisions locked (Jefe, 2026-06-25):** (1) trigger = **synthetic-stdin, zero-touch** (no edits to `play-review.sh`); (2) watch scope = **default branch only** (`refs/heads/main`); (3) cadence = **hourly**; (4) cross-family design review before code (done).

### v0.2 changelog (folded review findings)
- **B4 (codex):** dropped the "pure stateless" model — added a durable **`review_cursor`** table (the accurate last-reviewed signal + dedup key + bootstrap state). State lives in the ledger (litmus-green).
- **B1 / G1 (both):** brain **`git fetch`es** the watched ref before dispatch (fetch is a `.git` write, not read-only).
- **B2 / G2 (both):** **bootstrap** — first sight of a repo seeds the cursor at current HEAD as `baseline` (not reviewed); only future deltas are reviewed.
- **B3 (codex):** brain passes a **scrubbed env that never sets `PLAY_AUTOFIX`**, so brain-triggered reviews can never auto-fix even if `AUTOFIX_ENABLED` is armed. (Belt-and-suspenders: warn-and-continue if the durable flag is present.)
- **M4 (codex):** corrected — `submit_job` admission only gates *child* jobs; brain enforces its **own** per-tick + daily dispatch budget + blocked-head backoff.
- **M1/M2 (both):** dedup softened to *at-least-once, cursor-gated*; controller lock gets TTL + metadata + trap + stale-reap (copy `play-review.sh:207-218`).
- **M3 (both):** expanded security — array subprocess/no-shell, exact ref, URL-scheme allowlist, env scrub, trusted fixed script path (owner/mode/symlink), repo-path canonicalization.
- **M5 (codex):** corrected the false durability claim — pool-down → review aborts on canary → cursor doesn't advance → **retry next tick**.
- **M6 (codex):** `repo_id = basename($repo)` to match play-review; reject duplicate basenames at config load.

---

## 1. What it does and why

**What:** A bounded, non-Claude controller (launchd, hourly) that for each trusted repo: fetches the watched ref, compares HEAD to a durable per-repo cursor, and — if HEAD advanced past the last *successfully reviewed* SHA and no review is in flight — triggers the existing `play-review.sh` pipeline for the delta. Then it exits.

**Why:** Today the runtime is purely reactive (a human `git push` fires the hook). The controller is the first time a non-human component *decides and drives* work from observed state, and a **level-triggered backstop** that catches anything the push-hook missed (hook-less machines, dropped events). Review is read-only; worst-case misfire = one wasted review job, never a repo mutation or merge.

**Explicitly NOT in v1:** LLM-in-the-decision-path, auto-fix (B3), auto-merge, learning, webhook ingestion. Later rungs / rejected deps.

## 2. State: the `review_cursor` table (the one structural addition)

```
review_cursor(
  repo_id      text,          -- basename($repo), matches play-review.sh:76 (M6)
  ref          text,          -- 'refs/heads/main'
  baseline_sha text,          -- HEAD at first sight (bootstrap high-water mark)
  reviewed_sha text,          -- last SHA whose review DELIVERED (advances only on success)
  pending_sha  text,          -- SHA currently dispatched/in-flight (NULL when idle)
  state        text,          -- baseline | dispatching | running | delivered | blocked
  attempts     int,           -- consecutive failed dispatches for the current pending_sha
  updated_at   timestamptz,
  PRIMARY KEY (repo_id, ref)
)
```
Migration `0003_review_cursor` (idempotent, + schema.sql mirror). This is the durable, accurate answer to "what was last reviewed" and "is one in flight" — neither derivable from `base_ref` (stamped pre-delivery, B4) nor from file markers (state-in-ledger, north-star litmus).

## 3. Data flow (input → process → output)

```
launchd timer (hourly, RunAtLoad)  → non-Claude trigger (classifier never runs)
        ▼
runtime.controller tick  [bounded JOB, not a daemon]
  ├─ acquire single-instance lock (atomic mkdir + TTL/mtime reap + metadata + trap; exit 0 if freshly held)
  ├─ load trusted repos from $ORCH/repos.json  (path + watch_ref); reject duplicate basenames (M6)
  ├─ optional: if $ORCH/AUTOFIX_ENABLED present → log a warning (brain never arms it anyway, B3)
  └─ for each repo, until MAX_DISPATCH_PER_TICK and daily budget allow:
       FETCH:   git fetch --no-tags --quiet origin <watch_ref>  (timeout, env-scrubbed, refspec-only) [B1]
       OBSERVE: head = the fetched ref's SHA (validate ^[0-9a-f]{40}$); cur = review_cursor row
       DECIDE (level-triggered, derived from cursor + head):
         no cursor row          → INSERT baseline(reviewed=head, state=baseline); DO NOT review [B2]
         head == reviewed_sha   → no-op (up to date)
         pending_sha == head    → no-op (in flight; on stale pending past TTL → re-dispatch w/ backoff)
         attempts >= MAX_ATTEMPTS for head → state=blocked, surface once, back off (M4)
         else                   → DISPATCH
       DISPATCH (only after cat-file -e head^{commit} AND reviewed_sha^{commit} succeed locally):
         set pending_sha=head, state=dispatching, attempts++  (UPDATE ... WHERE state matches = the dedup gate)
         subprocess.run(["$ORCH/play-review.sh","origin",remote_url],
                        input=f"{watch_ref} {head} {watch_ref} {reviewed_sha}\n", cwd=repo_path, env=SCRUBBED)
       (play-review delivers verdict → inbox/jefe; on its OWN done-<sha> it writes the success marker)
  ▼
ADVANCE: cursor reviewed_sha is advanced to head ONLY when delivery is confirmed (done-<sha> marker present
         OR a delivered review job for head) on a later tick; an aborted review leaves the cursor un-advanced
         → retry next tick (M5). pending_sha cleared on advance or on stale-TTL.
```

Note the dedup is **cursor-gated, not airtight** (M1): the conditional `UPDATE review_cursor SET pending_sha=head WHERE (pending_sha IS NULL OR stale)` is the at-most-one-dispatch-per-tick gate; `play-review`'s `$ORCH`-global lock (`play-review.sh:207`) still prevents two *full* reviews running at once, and its `done-<sha>` marker prevents re-review of a delivered SHA. The residual: a brain dispatch + a simultaneous human push for the same SHA — the loser hits the global lock and emits a SKIPPED note (cheap, rare, acceptable for v1).

### Synthetic-stdin correctness (locked Option A)
Brain pipes `"<watch_ref> <head> <watch_ref> <reviewed_sha>"` + argv `origin <url>` into `play-review.sh`. Its FRONT (`play-review.sh:59-65`) sees `remotesha=reviewed_sha`; since we fetched and asserted the object exists, `cat-file -e` passes → `base=reviewed_sha` → reviews exactly `reviewed_sha..head`. Byte-unchanged pipeline. (The bootstrap rule guarantees `reviewed_sha` is never `0000…` at dispatch, so the EMPTY_TREE path B2 is never taken.)

## 4. Edge cases & failure modes

| Case | Handling |
|---|---|
| Local clone behind remote (B1) | `git fetch` the exact refspec first; assert `cat-file -e` for head + reviewed_sha before dispatch; skip+log on fetch failure. |
| First-ever repo (B2) | seed cursor baseline=head, state=baseline, **no review**; review future deltas. |
| Autofix armed while brain reviews (B3) | brain env never sets `PLAY_AUTOFIX` → autofix can't fire; warn if `AUTOFIX_ENABLED` present. |
| Review aborts (pool down/canary fail/over-cap) (M5) | cursor `reviewed_sha` NOT advanced; `attempts++`; retry next tick; after MAX_ATTEMPTS → `blocked` + surface once + backoff. |
| Two ticks overlap | atomic-mkdir lock + TTL/mtime reap + trap cleanup; second exits 0. |
| Crash mid-tick leaves lock (M2) | lock carries pid/start/host; next tick reaps if older than TTL (> max tick runtime). |
| Brain dispatch races a real push for same SHA (M1) | cursor gate + play-review global lock + done-marker → exactly one full review; loser emits SKIPPED. |
| Runaway dispatch (M4) | `MAX_DISPATCH_PER_TICK` + daily dispatch budget (own counters; submit_job admission does NOT cover root jobs) + per-head backoff. |
| repos.json missing/malformed | log + exit 0 (never crash-loop launchd). |
| Duplicate repo basenames (M6) | reject at config load (basename is the repo_id key). |
| HEAD advances mid-tick | level-triggered: next tick sees newer SHA, reviews the new delta. |

## 5. Security surface (untrusted / injected / stored)

- **Untrusted input:** only `git ls-remote`/`fetch` ref SHA. Validate `^[0-9a-f]{40}$`; require `ls-remote --exit-code` to be exactly one two-field line (reject noisy/multi output). Repo path + remote come ONLY from trusted `$ORCH/repos.json` (chmod 600, outside any repo).
- **Execution (M3):** `subprocess.run` argv form, **never `shell=True`**, explicit `cwd`, `input=` bytes. Execute only the **fixed trusted `$ORCH/play-review.sh`** after owner/mode/symlink checks (never a worktree fallback copy). Exact ref equality `refs/heads/main` (not a glob). Remote URL **scheme allowlist** (ssh/https/file only).
- **Env scrub:** pass a minimal env (PATH/HOME/TMPDIR + the needed LLM keys for downstream `mxr`); **strip `PLAY_AUTOFIX`, `GIT_*`, `BASH_ENV`, protocol-helper vars**. (B3 + M3)
- **Injected:** nothing — the brain builds no prompts; passes only validated SHAs/refs/paths into the DATA-fenced pipeline.
- **Stored:** the `review_cursor` row (SHAs + state). No secrets.
- **Trigger legitimacy:** launchd originator → [[autonomous-dispatch-classifier]] litmus green (fires without Claude). Brain is plain Python in the runtime layer; downstream `mxr` is not classifier-walled for it.
- **Privilege:** read + fetch on repos; the only "write" action is triggering a review (never mutates a repo / merges). No network listener bound.

## 6. Patterns borrowed / NOT built (from the brief)
- **BORROW:** K8s level-triggered + idempotent reconcile; deterministic dedup key `(repo_id, ref, head_sha)` + cursor-gated conditional dispatch.
- **ADOPT:** launchd (live `fix-sweep` pattern); the play-review lock/stale-reap pattern; the existing migration + schema.sql mirror convention.
- **BUILD (net-new):** `git fetch`+poll, the `review_cursor` table + migration 0003, the decide/dispatch/advance tick, own dispatch budget.
- **NOT built:** controller framework / Temporal / Argo; webhook listener; Redis dedup; any LLM in the decision path; learning; auto-merge. (Later rungs or rejected.)

## 7. Components & footprint
- **New:** `src/runtime/controller.py` (~200–250 lines now: config load → per-repo fetch/observe/decide/dispatch/advance → bounded + lock). `python -m runtime.controller tick`. Migration `0003_review_cursor` + schema.sql mirror + ledger methods (`get_cursor`, `upsert_baseline`, `claim_dispatch`, `advance_cursor`, `mark_blocked`). `orchestrator/ai.myndaix.controller.plist.example`. `tests/test_controller.py` + `orchestrator/test.sh` extension.
- **Reused unchanged:** `play-review.sh`, `repos.json` (+ optional `watch_ref`, default `refs/heads/main`), pool/ledger, launchd pattern.
- **Knobs:** launchd interval (hourly), `MAX_DISPATCH_PER_TICK` (default 3), `MAX_CONTROLLER_DISPATCH_PER_DAY`, `MAX_ATTEMPTS` (default 3), lock TTL, `watch_ref` per repo.
- **Rollback:** `launchctl unload` the controller agent — one command; the rest of the runtime is untouched. (The cursor table is inert without the controller.)

## 8. Remaining gates for plan/Jefe
1. **Scope grew** from "150-line stateless" to "~250 lines + a table + migration" — correctness demanded it (B4). Confirm OK before the implementation plan.
2. **Cursor-advance signal** — confirm "advance only when `done-<sha>` marker exists" is the right confirmation source (vs a delivered review job query). Recommend done-marker (it's play-review's own ground truth for a delivered review).
3. **Re-review v0.2?** — optional focused cross-family re-review of this revision, or proceed to the implementation plan. Recommend: proceed to plan (findings were convergent + concrete; a re-review of the *plan* + built code covers it).

## 9. Climb position
`orchestrator-v0` ✓ → phase2 human-gated fix + concurrency ✓ → **controller-loop (this) ← rung 3** → +learning (outcomes ledger plugs into `decide()`) → auto-merge one narrow class → widen → broad self-fixing → self-fixing its own code. This rung adds the decide-and-drive skeleton + the cursor; the learning rung reads outcomes into `decide()`; the auto-merge rung is the first time `decide()` may emit a merge — all on this same level-triggered frame + cursor.
