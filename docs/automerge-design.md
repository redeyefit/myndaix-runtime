# Auto-merge (docs-only PR-gate) — DESIGN v0.1

_North-star rung 4. v1 = the brain auto-merges **docs-only** PRs that pass a hard gate. First removal of the human merge gate. Prior-art: `docs/automerge-research.md`. Status: **for the adversarial design workflow + Oracle, then Jefe plan approval BEFORE any merge code ships**._

**Decisions locked (Jefe, 2026-06-26):** (1) safe class = **docs-only** (`*.md` etc.); (2) target = **auto-merge green safe-class PRs** (human-authored; the brain gates+merges, does NOT author). Safety defaults: **OFF by default**, every merge revertible, hard CI-green + diff-class gate, no autonomous merge of *code* to the runtime.

---

## 1. What it does & why
A bounded, non-Claude (launchd, hourly) sibling of the controller-loop. Each tick it lists open PRs against `main`; for each, it applies a **hard, all-must-pass gate** and, only if every gate passes, **merges the PR** — no human click. Then exits.

**Why:** this is the first rung where the brain *acts* on `main` autonomously rather than only surfacing work. It's deliberately the **narrowest, lowest-blast-radius** action possible (docs can't break a build or brick the brain), so the machinery that removes the human gate is proven on a class where a mistake is near-harmless before any code class is ever considered.

**NOT in v1:** code/test/manifest/lockfile merges · self-authored fixes · evidence/learning-based promotion · any *code* merge to the runtime.

## 2. The gate (ALL must pass, else skip → leave for human)
For an open PR `#n` (base `main`, head `H`):
1. **Mergeable & open** — `gh` reports `MERGEABLE` / `CLEAN`, not a draft, not already merged/closed.
2. **Docs-only diff-class** (§3, the crux) — every changed path is a pure-doc file; reject on any non-doc path or any uncertain status.
3. **CI green** — the required `test` check is `SUCCESS` (not pending/failure/missing).
4. **Review PASS** — play-review on `base...H` returns PASS (no NEEDS-FIX, no abort). Reuses the controller's pipeline.
5. **Bounds** — under the per-tick + per-day auto-merge caps; `AUTOMERGE_ENABLED` durable flag present; not DRY-RUN.
6. **Head-pinned** — re-fetch the PR head right before merging and assert it still equals `H` (no TOCTOU: the diff/CI/review we judged must be the commit we merge).

Pass all → `gh pr merge #n --merge` (revertible merge commit). Any fail → log the reason, leave the PR for a human.

## 3. The docs-only diff-class gate (load-bearing; must be un-gameable)
This is the only net-new security-critical code. Rules:
- Get the changed files with **both rename sides**: `git diff --name-status -M base...H` (or `gh pr view --json files` cross-checked).
- **Every** path — for adds/mods/deletes AND **both** old+new of any rename/copy — must match the strict doc allowlist: `*.md` (v1; `.rst`/`.txt` only if added later). 
- **Reject** if: any path is outside the allowlist · the changed-file list is empty · a rename's *other* side is a non-doc · a submodule/symlink mode change (`git diff --raw` mode `160000`/`120000`) · a file mode flips to executable · the PR adds a `.gitattributes`/`.github/`/CI/manifest/lockfile path.
- A `.md` is judged safe **regardless of directory** (a `.md` is rendered text, never executed), BUT a non-`.md` file under `docs/` is NOT safe (e.g. `docs/conf.py`). So the rule is **by extension, not by path** — strictly `endswith('.md')` on every side.
- Compute the gate against `base...H` exactly (the merge-base range), the same content the merge would introduce.

## 4. Data flow
```
launchd (hourly) → automerge tick  [bounded job, flock single-instance]
  ├─ AUTOMERGE_ENABLED present? else exit (OFF by default)
  ├─ gh pr list --base main --state open --json number,headRefOid,isDraft,mergeable,...
  └─ for each open PR (capped):
       gate 1 mergeable/open → gate 2 docs-only diff → gate 3 CI green
         → gate 4 play-review(base...H) == PASS → gate 5 bounds → gate 6 head still == H
       all pass:  gh pr merge #n --merge   (+ charge caps, log loudly)
       any fail:  log reason, leave for human
  exit
```
State: dedup so the same (PR, head) isn't re-reviewed every tick — a small `automerge_seen(repo, pr_number, head_sha, decision)` ledger row (or reuse play-review's done-marker keyed by H). Re-evaluate only when the PR head changes.

## 5. Safety pillars (mapped to the north-star)
1. **Verify load-bearing + un-gameable:** the by-extension diff-class gate (§3) + CI-green + cross-family review PASS. Three independent gates; the diff-class one is mechanical and un-foolable.
2. **Bounded blast radius:** docs-only ONLY; never code; per-day merge cap; anything uncertain → human. A bad docs merge = a wrong sentence, instantly revertible.
3. **Instant rollback:** a merge is an ordinary revertible commit; `AUTOMERGE_ENABLED` off halts instantly; DRY-RUN proves decisions first.
4. **Learning IS safety — DEFERRED:** we skipped the learning rung, so promotion is HARD-CODED to this one class (no auto-widening). Every auto-merge is logged loudly; widening to any code class waits for the outcomes ledger.
5. **Self-runtime gated longest:** docs are EXEMPT (can't brick the brain), so docs-PR auto-merge on the runtime is allowed. CODE auto-merge (future rungs) will exclude the runtime entirely.

## 6. Security surface (untrusted / injected / stored)
- **Untrusted:** the PR's diff + the PR author. A docs change can carry bad *content* (misinformation, prompt-injection text) but **cannot execute** — that's the whole reason docs is the first class. The review gate reads it (catches obviously-bad content); the merge introduces only rendered text. Validate all `gh`/git output (SHA `^[0-9a-f]{40}$`, PR number integer, ref allowlist); array subprocess, never `shell=True`.
- **The merge is the privileged action** — gated 6-deep, capped, flag-gated, head-pinned. `gh pr merge --merge` only (never `--admin`, never force, never bypass a red check).
- **Injection into the review:** the PR diff flows into play-review, which already DATA-fences untrusted content; the gate passes only validated SHAs/numbers.
- **Stored:** the `automerge_seen` dedup rows (PR number + head + decision). No secrets. `gh` token is HOME-based (not in our config).
- **Trigger legitimacy:** launchd (non-Claude) → classifier-clean. The brain is plain Python; `gh` runs as the user.
- **Branch-protection interplay:** if `main` has required reviews, `gh pr merge` without an approval will fail (safe — it just won't merge). v1 does NOT add/relax branch protection; it works within it.

## 7. Components & footprint
- **New:** `src/runtime/automerge.py` (the tick: list PRs → gate → merge; ~200 lines) + `python -m runtime.automerge tick`; `orchestrator/automerge-tick.sh` (launchd wrapper) + `ai.myndaix.automerge.plist.example`; a tiny `automerge_seen` ledger table + migration `0004`; `tests/test_automerge.py` (gate truth-table over synthetic PRs, the diff-class gate adversarially) + a docs-only-gate unit suite.
- **Reused:** play-review (review gate), the controller's lock/cap/flag/DRY-RUN patterns, `gh`, the ledger.
- **Knobs:** `AUTOMERGE_ENABLED` (durable flag, OFF), `MAX_AUTOMERGE_PER_TICK` (1), `MAX_AUTOMERGE_PER_DAY` (3), DRY-RUN, the doc-extension allowlist.
- **Rollback:** `launchctl unload` the agent; `git revert` any merge.

## 8. Open questions for the adversarial workflow + Jefe
1. **Is the by-extension `.md`-only gate truly un-gameable?** Adversarial: a `.md` that's actually a symlink; a `.md` with a malicious `.gitattributes` smudge filter elsewhere; a rename `evil.py → evil.md`; an LFS pointer; a submodule gitlink with a `.md`-looking name. (§3 tries to cover these — pressure-test.)
2. **Does the review gate add value for docs, or is CI-green + docs-only enough?** Recommend keeping the review (catches bad content), but it's a cost/latency call.
3. **Required-approval interplay** — should the brain's merge count as the approval, or stay subordinate to branch protection? Recommend: stay within branch protection (don't `--admin`); if main requires a human approval, the brain merges only PRs that already have it (or we accept it just won't merge until then).
4. **One PR per tick** (serialize) vs several — recommend 1/tick + 3/day for the first rung.
