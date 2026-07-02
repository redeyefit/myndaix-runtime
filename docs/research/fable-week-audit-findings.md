# Fable-Week Audit — Findings & Dispositions

_Two adversarial multi-agent audits run 2026-07-02 (Fable week). Every non-trivial finding was
independently re-verified against the code (refute-by-default) before landing here._

- **Runtime spine** (`runtime-spine-audit`, 23 agents / ~1.5M tok): 11 confirmed, 1 refuted.
- **FieldVision prod** (`fieldvision-prod-audit`, 14 agents / ~780k tok): 6 confirmed, 3 refuted.

The refuted findings matter as much as the confirmed: the two scariest FieldVision hypotheses
(cross-tenant photo / voice-note leaks via the service-role client) were **REFUTED** on trace —
the authorization helper does gate them. That's the adversarial-verify pass doing its job.

Legend — **Status**: ✅ FIXED (PR up) · 🔧 QUEUED (quick-win, batchable) · 🧠 JEFE-CALL (design
intent) · 📋 ACCEPTED (documented, no action). Severity is the VERIFIER's reassessed severity.

---

## Runtime spine (11 confirmed)

| # | Sev | Area | Finding | Status |
|---|-----|------|---------|--------|
| 1 | HIGH | automerge | **Paid review runs BEFORE caps** → a cap-deferred PR re-runs the full 3-agent review every tick forever (spend leak + the #45 stuck-PR wedge). | ✅ **PR #51** |
| 2 | MED | runner | `scratch_home` blocks only `$HOME`-relative reads; an injected codex prompt can read `~/.ssh`/`~/.aws`/`~/.myndaix/.secrets` by ABSOLUTE path on the plain `invoke_cli` path (the sbpl read-deny lives only in `play-fix.sh`). Output returned un-redacted. | 🔧 QUEUED |
| 3 | MED | pool | Captured-diff `wt-<id>.patch` artifacts are never swept on the graceful-completion path → monotonic disk/inode growth on the shared worktree root (the exact fix-generation workload being scaled). | 🔧 QUEUED (easy) |
| 4 | MED | controller | Sustained pool/agent outage inflates `attempts` → a good head BLOCKS and is never reviewed until a new push. | ✅ **substantially fixed by PR #48** (transient-canary refund); verify overlap, then close |
| 5 | MED | controller | A review-cursor BLOCK (a real push left permanently unreviewed) is logged but **not** alerted to Jefe, unlike skill-blocks. | 🔧 QUEUED (bundle w/ #48: add `_alert_jefe` on `mark_blocked`) |
| 6 | MED | controller | `_branch_protection_ok` passes with **0 required approvals** — "PR review required" isn't actually enforced, so an admin self-merge could land a `skills/` change the promoted-skill trust model assumes was reviewed. | 🔧 QUEUED (tighten to require `required_approving_review_count ≥ 1`) |
| 7 | MED | shell | `PLAY_LSREMOTE_TIMEOUT` / `PLAY_CAPTURE_TIMEOUT` feed `perl alarm()` **unvalidated** — a non-numeric value silently disables the bound, making the very ls-remote it guards unbounded (can wedge the held review lock). | 🔧 QUEUED (validate like `STALE`) |
| 8 | MED | shell | Lock acquire **double-reap race**: two workers can both reap the same >45-min stale lock in the stat-vs-mkdir window and run reviews concurrently (release side is already fixed; acquire side isn't). | 🔧 QUEUED (trickier — atomic claim after reap) |
| 9 | MED | shell | Nonce-collision belt covers the diff→reviewer hop but not the reviewer→lobster hop; a reviewer induced to echo the nonce could forge a fence + `PLAY_PASS` into lobster's trusted context. Defense-in-depth (needs a model to leak a 128-bit secret). | 🔧 QUEUED (mirror the existing check on `$review`/`$oracle_review`) |
| 10 | MED | play-fix | Post-exec tamper check uses `git diff` (unstaged only) → a STAGED runtime edit of a tracked test/source file reads as clean. Masked today by the sandbox's index-write denial (independent layer). | 🔧 QUEUED (`git diff HEAD`) |
| 11 | MED | api | `InboundIn.reply_target` skips the NUL guard → a client can 500 `/inbound` via a jsonb-rejected value. | 🔧 QUEUED (add to `_strip_nul`) |

**Refuted (1):** "single-author allowlist hard-caps the gate to 1 merge/day forever" — refuted;
the cap is per-UTC-day and resets, and finding #1's fix removes the wasteful re-review. The cap
itself is a deliberate safety rail, not a defect.

## FieldVision prod (6 confirmed)

| # | Sev | Area | Finding | Status |
|---|-----|------|---------|--------|
| F1 | HIGH | rate-limit | `/parse-import`, `/import` (paid Gemini/Claude) + `/reports/generate` fall through the limiter → unbounded external-AI spend, near-anonymous. | ✅ **PR #31** (adds keys + `/api/reports` gate + fail-closed 30/min default) |
| F2 | MED | health | `/api/health?delay=` is a client-controlled unauthenticated dwell knob (holds a function slot up to 55s). | 🔧 QUEUED (bundle: health-hardening PR) |
| F3 | MED | health | `/api/health` leaks the env-var inventory + verbatim DB error strings + the Anthropic key length to unauthenticated callers. | 🔧 QUEUED (same health-hardening PR: bare `{ok}` or auth-gate) |
| F4 | MED | authz | Anon-project claim runs a service-role `UPDATE` on an **unverified** `x-anonymous-id` header (no `verifyAnonymousId`). Bounded (needs the victim's 128-bit token), overlaps the known-deferred anon-claim item. | 🔧 QUEUED (one-liner: add `verifyAnonymousId` to the guard) |
| F5 | below-MED | rls | `line_items`/`tasks` RLS grants `FOR ALL` to any org MEMBER while `projects` writes are admin-gated. Not cross-tenant; consistent with the app's own `canAccessProject` (members CAN edit schedules); the RLS path is never used in prod (service-role). | 🧠 JEFE-CALL: **are schedule edits meant to be member-level?** If yes, 📋 accept + document. If admin-only, split the policy. |
| F6 | — | (rolled into F1) | `/reports/generate` no rate limit | ✅ covered by **PR #31** |

**Refuted (3):** cross-tenant photo/voice-note leak via `/reports/[id]` (authz gates it); a second
RLS-child-table leak claim (tables aren't reachable as described); a `parse` raw-text-to-Gemini
"injection" (properly authorized, only affects the caller's own project).

---

## Disposition summary

**Shipped this session (the 2 live spend leaks — highest wallet risk on paid/autonomous paths):**
- ✅ PR #51 — automerge caps-before-review (runtime)
- ✅ PR #31 — FieldVision paid-AI rate-limit gap + fail-closed default
- ✅ (already) PR #48 — transient-canary refund covers spine finding #4

**Quick-win batch (recommended next — all MED, small, mostly mechanical):** spine #3 (patch
sweep), #7 (timeout validation), #10 (`git diff HEAD`), #11 (NUL guard), #9 (nonce belt) are each
a few lines; #5 (block alert) folds onto the controller; #6 (branch-protection) and #2
(sbpl-on-invoke_cli) and #8 (lock reap) are the meatier three. FieldVision #F2+#F3 = one
health-hardening PR; #F4 = a one-line verify. Good candidates to **dogfood the autonomous fix
stage** on once it's proven, or to knock out as small human-gated PRs across the rest of Fable
week.

**Needs Jefe's design intent (1):** FieldVision #F5 — is schedule editing (tasks/line-items) meant
to be any-member or admin-only? The verifier says member-level is consistent with the current app
authorization, so this is likely 📋 accept-and-document, but it's your product call.

**Not found (the good news):** no cross-tenant data breach, no auth bypass, no secret exposure to
end users, no autonomous-wrong-merge path. The spine's heavily-commented prior-review defenses held
up — every confirmed spine finding is a MEDIUM residual, and the one HIGH was a spend/wedge bug, not
a correctness or security breach.
