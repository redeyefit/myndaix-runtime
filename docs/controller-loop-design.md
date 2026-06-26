# Controller-Loop ("the brain") — DESIGN v0.1

_North-star rung 3. v1 = **proactive review scheduler**, built as a thin level-triggered reconciler. No LLM judgment, no auto-merge, no learning (later rungs). Prior-art basis: `docs/controller-loop-research.md`. Status: **for Oracle review BEFORE any code** (per new-systems.md)._

**Decisions locked (Jefe, 2026-06-25):** (1) trigger = **synthetic-stdin, zero-touch** (no edits to `play-review.sh`); (2) watch scope = **default branch only** (`refs/heads/main`); (3) cadence = **hourly** (match `fix-sweep`); (4) **cross-family design review (Oracle + codex) before any code**.

---

## 1. What it does and why

**What:** A bounded, non-Claude controller that wakes on a timer (launchd), and for each configured repo checks whether the remote HEAD has advanced past what was last reviewed. If so — and no review is already in flight for that exact SHA — it triggers a review of the new commits. Then it exits.

**Why:** Today the runtime is purely *reactive* — a human `git push` fires the `play-review.sh` pre-push hook. That misses any push from a machine without the hook, and means **nothing decides what the team does without a human in the loop.** The controller is the first time a non-human component *decides and drives* work from observed state. It's also a **level-triggered backstop**: even if a push-hook event is missed, the next tick sees the SHA gap and reviews it. This is the smallest, safest step that makes the brain genuinely autonomous (review is read-only; worst-case misfire = one wasted review job, never a repo mutation or merge).

**Explicitly NOT in v1:** LLM-in-the-decision-path, auto-fix triggering, auto-merge, outcomes/learning, webhook ingestion. Each is a later rung or a rejected dependency.

## 2. Data flow (input → process → output)

```
launchd timer (e.g. every 30 min, RunAtLoad)
        │  (non-Claude trigger → classifier never runs → genuinely autonomous)
        ▼
runtime.controller tick  [bounded JOB, not a daemon]
        │
        ├─ acquire single-instance lock (atomic mkdir; exit clean if held)
        │
        ├─ load trusted repo list from $ORCH/repos.json  (path + watch_ref; reuse the play-fix map)
        │
        └─ for each repo (capped at MAX_DISPATCH_PER_TICK):
              observe:  head = git -C <path> ls-remote origin <watch_ref>   (REMOTE HEAD)
              observe:  last = ledger: most-recent review job for (repo_id, *) → its base_ref
              decide (level-triggered, pure function of observed state):
                 if head == last                       → no-op (up to date)
                 if EXISTS open/recent review job (repo_id, base_ref=head) → no-op (dedup: in flight)
                 if $STATE/done-<head> marker exists    → no-op (already reviewed; play-review's own marker)
                 else                                   → DISPATCH
              emit:  invoke the existing review entrypoint for (repo, watch_ref, head, last)
        ▼
play-review.sh worker  →  mxr canary / kilabz / oracle / lobster  →  verdict to inbox/jefe
  (unchanged proven pipeline: its own dedup via done-<sha>, daily cap, diff cap, lock, delivery)
```

The brain holds **no state of its own** — a pure reconciler. Every decision is re-derived each tick from (ledger ∪ live HEAD ∪ play-review's done-markers). State stays in the ledger / existing review state, never a new brain file. (Litmus green: state in ledger not files.)

### Key decision — HOW the brain triggers a review (for Oracle)
Review is orchestrated by `play-review.sh`, a pre-push hook that reads the git pre-push stdin protocol (`<localref> <localsha> <remoteref> <remotesha>`) and reviews `remotesha..localsha`. Two ways for the brain to invoke it:

- **(A) Synthetic-stdin (zero-touch):** feed `play-review.sh` a constructed pre-push line `"<watch_ref> <head> <watch_ref> <last>"` with cwd=repo. Reviews exactly `last..head`. **Zero changes to the security-reviewed review pipeline.** Slightly clever (simulating a hook event).
- **(B) Explicit scheduled mode (small refactor):** split `play-review.sh` into "resolve trigger → (repo,ref,tip,base)" and "run pipeline(repo,ref,tip,base)"; the hook and the brain both feed the core. Cleaner contract, but touches a heavily-reviewed file.

**Recommendation: (A) for v1** — lowest risk to the proven path, no re-review of `play-review.sh` needed; the synthetic line is derived from *real* observed state (live HEAD + last-reviewed), so the diff reviewed is exactly correct. Flag (B) as the clean follow-up once the brain has earned its keep. _Want Oracle's read on A-vs-B._

## 3. Edge cases & failure modes

| Case | Handling |
|---|---|
| Two controller ticks overlap (slow tick + timer fires again) | atomic-`mkdir` single-instance lock; second exits clean (the Celery-Beat duplicate-dispatch lesson). |
| Brain dispatches, then a real push also fires the hook for same SHA | both hit `play-review`'s `done-<sha>` / ledger dedup → exactly one review. At-least-once + idempotent handler. |
| `git ls-remote` fails / network down for a repo | that repo is skipped this tick, logged; **fail-soft, never wedge the loop** (other repos still processed). Next tick retries. No state mutated. |
| Repo absent from `$ORCH/repos.json` | not watched. Fail-closed (only the trusted, explicitly-listed repos are polled). |
| `repos.json` missing/malformed | tick logs error and exits 0 (no dispatch). Never crash-loops launchd. |
| HEAD advances mid-tick | reconciler is level-triggered — next tick sees the newer SHA and reviews the delta. No lost work. |
| play-review daily cap / diff cap hit | the brain's dispatch is absorbed by play-review's existing caps (it ABORTs/SKIPs and delivers a note). Brain doesn't need its own cap logic. |
| Runaway dispatch (misconfig → many repos all "advanced") | `MAX_DISPATCH_PER_TICK` hard cap + `submit_job`'s existing admission (`max_children`/`cost_budget`/`chain_ttl`) downstream. Conservative fallback = skip the rest this tick. |
| launchd fires while serve/pool is down | dispatch still records to ledger (durable); reviews drain when the pool returns. (Brain decoupled from execution — litmus green.) |

## 4. Security surface (untrusted / injected / stored)

- **Untrusted input:** the only external data the brain ingests is `git ls-remote` output (a 40-char SHA + ref name). **Validate the SHA matches `^[0-9a-f]{40}$` and ref matches an allowlist pattern before use** — never interpolate raw remote output into a shell command. Repo path comes ONLY from the trusted `$ORCH/repos.json` (never from remote data) — same model as play-fix (resolves the repo_id-as-path hazard).
- **Injected:** nothing. The brain builds no prompts; it triggers `play-review.sh`, which already wraps all task content in `treat-as="DATA"` fences. The brain passes only validated SHAs/refs/paths.
- **Stored:** nothing new. No brain-owned state file. Decisions are ephemeral; durable state lives in the ledger (review jobs) and play-review's existing `done-<sha>` markers.
- **Trigger legitimacy:** launchd is the originator (not Claude) → passes the [[autonomous-dispatch-classifier]] litmus (fires on its own; remove Claude and it still runs). The brain is plain Python in the runtime layer, so `mxr` (used downstream by play-review) is not classifier-walled for it.
- **Privilege:** read-only on repos (`ls-remote`); the only write is triggering a review (which itself never mutates a repo or merges). No new secrets, no network listener bound.
- **`repos.json` is the trust root** — lives OUTSIDE any repo at `$ORCH/repos.json` (chmod 600); a malicious commit can't redefine which repos are watched or their paths.

## 5. Patterns borrowed / deliberately NOT built (from the brief)

- **BORROW:** K8s level-triggered + idempotent reconcile (decide from observed state, not the event). Deterministic dedup key `(repo_id, head_sha)` + conditional dispatch.
- **ADOPT:** launchd (the live `fix-sweep` plist pattern); `submit_job`'s existing admission bounds (downstream of play-review's `mxr`); single-instance atomic-`mkdir` lock (existing watcher pattern).
- **BUILD (thin, the only net-new code):** `git ls-remote` poll + the decide-then-dispatch tick.
- **NOT built:** controller framework / Temporal / Argo; webhook listener; Redis dedup; any LLM in the decision path; learning; auto-merge. (Later rungs or rejected deps.)

## 6. Components & footprint

- **New:** `src/runtime/controller.py` (~150 lines: load config → per-repo observe/decide/dispatch → bounded) + `python -m runtime.controller tick` entry. `orchestrator/ai.myndaix.controller.plist.example`. A small ledger read method `recent_review_for(repo_id, base_ref)` (or reuse `get_status`/a count query). `test_controller.py` + extend `orchestrator/test.sh`.
- **Reused unchanged:** `play-review.sh` (review pipeline), `repos.json` (+ optional `watch_ref` field, default `refs/heads/main`), the pool/ledger, the launchd pattern.
- **Config knobs:** `MYNDAIX_CONTROLLER_INTERVAL` (launchd), `MAX_DISPATCH_PER_TICK` (default e.g. 3), `watch_ref` per repo. Feature-flag / instant rollback = unload the launchd agent (one command) — the rest of the runtime is untouched.

## 7. Decisions (Jefe-locked) + remaining gates for Oracle/codex

**Locked:**
1. **Trigger = (A) synthetic-stdin, zero-touch** — `play-review.sh` byte-unchanged.
2. **Watch scope = default branch only** (`refs/heads/main`).
3. **Cadence = hourly** (match `fix-sweep`).

**Still for the review to pressure-test:**
4. **"Last reviewed SHA" source / double-review window** — ledger review-job `base_ref` as the in-flight dedup authority + play-review's `done-<sha>` as its own completion guard. Is there a window where the brain dispatches before any review job row exists yet (race between dispatch and play-review's first `mxr` insert)? Mitigation candidate: brain writes nothing, relies on play-review's `done-<sha>` + a short "recently dispatched" check. **Want Oracle/codex to confirm the dedup is airtight under overlapping push + tick.**
5. **Stateless confirm** — no new ledger table for v1 (reconciler derives all state). Confirm the `base_ref` query + done-markers fully cover dedup without a brain-owned row.
6. **Synthetic-stdin correctness** — does feeding `"<ref> <head> <ref> <last>"` exactly reproduce the diff a real push would review, including the new-branch / `last == 0000…` (first-ever review) path in play-review's base computation?

## 8. Climb position
`orchestrator-v0` ✓ → phase2 human-gated fix + concurrency ✓ → **controller-loop (this) ← rung 3** → +learning (outcomes ledger) → auto-merge one narrow class → widen → broad self-fixing → self-fixing its own code. This rung adds the *decide-and-drive* skeleton; the next rung (learning) plugs an outcomes read into `decide()`; the rung after (auto-merge) is the first time `decide()` may emit a merge — all on this same level-triggered frame.
