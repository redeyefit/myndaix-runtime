# DESIGN — auto-capture rung v0.1 ("the proposer"): turn a recurring review lesson into a PROPOSED skill

**Status:** DRAFT for cross-family review (kilabz + oracle) BEFORE build. Governing constraints:
default OFF; **the proposer NEVER promotes** (it only opens a PR — the existing human-merge-under-
-branch-protection gate is unchanged); fail-CLOSED; no LLM in any security decision.

## What it does + why
The +learning rung (LIVE) injects human-seeded review skills. The gap Jefe named: a human-seeded
corpus depends on the human remembering to seed it. Auto-capture removes that: when a review keeps
surfacing the **same** lesson, the system DRAFTS a `skills/<name>/SKILL.md` and opens it as a PR.
Jefe just clicks merge (or closes it). **The agent remembers; the human decides.**

It does NOT auto-promote, auto-merge, or auto-inject anything. A proposal is an ordinary PR that
lands in the EXISTING gate: `skills/` ∈ automerge `_DENY_DIRS` → routed to a human; the human merge
under branch protection IS the promotion (controller stamps `provenance='promoted'`). So the worst
a bad/injected proposal can do is be a PR Jefe rejects. The security boundary is unchanged.

## Data flow (input → process → output)
1. **Signal** — a play-review/controller verdict is `NEEDS-FIX` with a finding. The verdict +
   findings already persist (ledger / `$run/fixlist.txt`). Input is TRUSTED-internal (our own
   reviewers), not arbitrary external text.
2. **Recurrence gate (deterministic)** — a finding becomes a candidate ONLY after the same
   lesson-class recurs ≥ `CAPTURE_MIN_RECUR` times (default 3) across distinct reviews. Class key =
   a deterministic fingerprint (normalized file-glob + a stable rule tag the reviewer emits), NOT a
   fuzzy LLM match. Stored in an append-only `capture_candidate` ledger table (count + first/last
   seen). No single review can trigger a capture → no one-off noise, no single-injection→capture loop.
3. **Draft** — when a candidate crosses the threshold, render a SKILL.md from a FIXED template:
   name = slug(rule tag); description ≤60; path_trigger = the normalized glob; body = the finding
   text, run through the SAME `skillmatch.lint_skill` + `scan_injection` the controller uses at
   promotion. A draft that fails lint is DROPPED (never opened) + alerts jefe — never a malformed PR.
4. **Propose** — open ONE PR per candidate (`gh pr create`, branch `skill/auto/<name>`), labeled
   `auto-proposed`, body citing the N reviews that produced it. Idempotent: never reopen a
   candidate that already has an open/merged/closed PR (dedupe key in the table).
5. **Promote (UNCHANGED)** — Jefe merges → controller indexes → `provenance='promoted'`. Reject =
   close the PR; the candidate is marked `declined` and not re-proposed unless it recurs MANY more.

## Edge cases / failure modes
- **Proposal storm** — caps: ≤ `CAPTURE_MAX_OPEN` (default 3) open auto-PRs at once, ≤1 new/day.
- **Declined-then-recurs** — a closed proposal is not re-proposed until recurrence ≥ a higher
  `CAPTURE_REPROPOSE` floor (avoids nagging Jefe with a skill he already rejected).
- **Reviewer doesn't emit a stable tag** — then no candidate forms (fail-closed; capture simply
  doesn't fire). v1 requires the reviewer to emit a `rule:<tag>` line; absent it, no capture.
- **The finding text is itself adversarial** (a prompt-injected reviewer output) — it goes through
  `scan_injection` + lint before drafting; a hit DROPS the draft. And even a clean-but-wrong skill
  is just a PR Jefe rejects.

## Security surface
- **Untrusted:** the finding TEXT (reviewer LLM output — could be injected/hallucinated). Mitigation:
  lint + injection-scan before drafting; the human merge gate is the real boundary.
- **Injected:** nothing into a live prompt — a proposal is a PR, never injected until promoted+armed.
- **Stored:** `capture_candidate` (append-only counts) — no executable content.
- **The proposer's gh token** opens PRs only; it MUST NOT have merge rights on `skills/` (enforce via
  the fine-grained PAT scope + the branch-protection required-PR). It can open, never merge.

## Borrows from openclaw / deliberately does NOT build
- BORROW: propose → re-scan-at-apply → human-promote → reversible; `status:proposed` (never live);
  draft integrity (lint+sha); decline-memory. REJECT openclaw's `approvalPolicy:auto` fail-open.
- Does NOT build (v1): LLM clustering of findings (recurrence is a deterministic tag-count, not an
  embedding match); auto-edit of EXISTING skills; capture from anything but our own reviewers;
  any auto-merge. Consolidation/dedup of overlapping skills is DEFERRED.

## Open questions for the reviewers
1. Is a reviewer-emitted `rule:<tag>` the right recurrence key, or should the controller derive the
   class from the changed-file glob alone (no reviewer cooperation needed)?
2. Threshold defaults (`MIN_RECUR=3`, `MAX_OPEN=3`, `1/day`) — too eager / too timid?
3. Should the proposer run inline in the controller tick (like the indexer) or as a separate
   launchd job? (Leaning inline — same fail-soft per-repo try/except, no new cron.)
