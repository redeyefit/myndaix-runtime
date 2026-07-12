"""mxr - submit a task to the running MyndAIX runtime and print the agent's reply.

    PYTHONPATH=src python3 -m runtime.cli <agent> "<task>"
    # or a wrapper on PATH (see docs/OPERATING.md):  mxr <agent> "<task>"

Needs the worker-pool service running (`python3 -m runtime.serve`) and $MYNDAIX_DSN.
This is direct ops: you name the agent, the runtime dispatches it durably and hands
back the real reply. No orchestrator in the loop.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

from runtime.contracts import TransportEnvelope
from runtime.ledger.postgres_store import PostgresLedger
from runtime.registry import REGISTRY

DSN = os.environ.get("MYNDAIX_DSN", "postgresql://localhost/runtime")


def _resolve_sync_wait(agent: str) -> float:
    """The SYNC wait for a submitted job to finish: MXR_TIMEOUT_S when set (env ALWAYS
    wins — play-review exports it for slow reviews), else the agent's profile-derived
    wait (Profile.sync_wait(): exec timeout + margin, so the wait scales with the
    agent instead of a flat 180s that expired under kilabz's 900s exec cap), else 180.
    Parsed HERE, not in an import-time default arg — an empty or malformed exported
    MXR_TIMEOUT_S would otherwise crash float() at import and take down the WHOLE cli,
    including `mxr get`/`mxr skillselect` that never submit a job (kilabz+oracle)."""
    raw = os.environ.get("MXR_TIMEOUT_S") or ""
    if raw:
        try:
            return float(raw)
        except ValueError:
            print(f"warning: invalid MXR_TIMEOUT_S={raw!r}, using the agent default",
                  file=sys.stderr)
    prof = getattr(REGISTRY.get(agent), "profile", None)
    return prof.sync_wait() if prof is not None else 180.0


async def submit(agent: str, task: str, *, context: Optional[dict] = None,
                 repo_id: Optional[str] = None, base_ref: Optional[str] = None,
                 timeout_s: Optional[float] = None) -> int:
    rc, _terminal = await run_job(agent, task, context=context, repo_id=repo_id,
                                  base_ref=base_ref, timeout_s=timeout_s)
    return rc


async def run_job(agent: str, task: str, *, context: Optional[dict] = None,
                  repo_id: Optional[str] = None, base_ref: Optional[str] = None,
                  timeout_s: Optional[float] = None) -> tuple[int, bool]:
    """Submit + sync-wait + print the reply. Returns (rc, job_terminal): job_terminal is
    False ONLY when the sync wait expired with the job still in flight — the review verb
    gates staging teardown on it (a job can outlive the wait; deleting the staged cwd on
    sync-timeout would yank a RUNNING reviewer's cwd — the age-reaper owns that case)."""
    if agent not in REGISTRY:
        roster = ", ".join(sorted(REGISTRY))
        print(f"unknown agent '{agent}'. roster: {roster}", file=sys.stderr)
        return 2, True
    if timeout_s is None:
        timeout_s = _resolve_sync_wait(agent)

    led = await PostgresLedger.connect(DSN)
    try:
        # the CLI is a transport: ingest -> submit, so completion auto-queues the reply
        env = TransportEnvelope(transport="cli", account="cli", sender_id="operator",
                                reply_target="cli:operator", dedupe_key=str(uuid.uuid4()))
        event_id = await led.ingest_inbound(env, task)
        # repo_id/base_ref scope the job to a repo bucket (omitted -> NULL -> cap-exempt)
        jid = await led.submit_job(to_agent=agent, prompt=task, context=context,
                                   inbound_event_id=event_id, created_by="operator",
                                   repo_id=repo_id, base_ref=base_ref)
        print(f"-> {agent}  (job {str(jid)[:8]})", file=sys.stderr, flush=True)
        # full id on its own parseable line so a caller (orchestrator/play-fix.sh) can
        # `mxr get <jid>` for the artifact_ref. stderr-only -> the stdout reply is untouched.
        print(f"JOB_ID={jid}", file=sys.stderr, flush=True)

        deadline = time.monotonic() + timeout_s
        st = None
        while time.monotonic() < deadline:
            st = await led.get_status(jid)
            if st and st["status"] in ("done", "failed", "dead"):
                break
            await asyncio.sleep(0.3)
        else:
            print("timed out (is the pool running? `python3 -m runtime.serve`)", file=sys.stderr)
            return 1, False

        if st["status"] == "done":
            reply = next((o["body"] for o in (st.get("outbound") or [])), None)
            if reply is not None:
                print(reply)
            for o in (st.get("outbound") or []):       # mark delivered so it doesn't linger
                if o["status"] == "pending":
                    await led.mark_outbound_sent(o["id"], f"cli-{o['id']}")
            return 0, True

        # failed/dead: surface WHY (the agent's error output, from the attempt)
        err = next((a.get("text") for a in (st.get("attempts") or [])
                    if a.get("status") == "failed" and a.get("text")), None)
        if err:
            print(err.strip(), file=sys.stderr)
        print(f"(job {st['status']})", file=sys.stderr)
        return 1, True
    finally:
        await led.close()


def _build_context(args: argparse.Namespace) -> dict:
    """Pack the optional media flags into Job.context (free-form dict, no contract
    change). Only set keys the operator actually passed."""
    ctx: dict = {}
    if args.image is not None:
        ctx["image_url"] = args.image
    if args.application is not None:
        ctx["application"] = args.application
    if getattr(args, "motion_id", None) is not None:
        ctx["motion_id"] = args.motion_id
    if getattr(args, "motion_strength", None) is not None:
        ctx["motion_strength"] = args.motion_strength
    if getattr(args, "shotlist", None):
        try:
            with open(args.shotlist) as f:
                ctx["shotlist"] = json.load(f)
        except (OSError, ValueError) as e:
            raise SystemExit(f"--shotlist: {e}")
    if getattr(args, "end_card", None):
        ec = args.end_card
        if not ec.startswith(("http://", "https://")):
            raise SystemExit("--end-card must be an http(s) URL (local paths are not accepted; "
                             "host the image or upload it first)")
        ctx["end_card_url"] = ec
    if getattr(args, "staged_workdir", None) is not None:
        # pass-through only: the RUNNER is the trust boundary (realpath strictly inside
        # $MYNDAIX_STAGING_ROOT, staging-cwd adapters only — fail-closed TERMINAL there).
        # `is not None` (not truthy): an EXPLICIT empty --staged-workdir "" must propagate
        # so the runner rejects it TERMINAL, never silently drop to a scratch downgrade
        # (kilabz r2 MED — a wrapper passing an unset var must not quietly de-contextualize).
        ctx["workdir"] = args.staged_workdir
    return ctx


async def get_job(job_id: str) -> int:
    """`mxr get <job_id>` -> structured JSON of the job's status, including
    artifact_ref + base_sha. The fix stage (orchestrator/play-fix.sh) reads the
    diff artifact from HERE - via the ledger, parsed as JSON - NEVER by grepping a
    reply body an agent controls (a spoofable path is a security hole, not a bug).

    Accepts a FULL uuid or an id PREFIX of >=8 hex chars (hyphens ignored on both
    sides, so both the 8-char short id `submit` prints and a hyphen-spanning slice
    of the full JOB_ID work). Ambiguous prefix -> fail closed listing candidates —
    same shape as the finding_key resolver (postgres_store.human_dismiss)."""
    raw_id = (job_id or "").strip()
    prefix: Optional[str] = None
    try:
        jid: Optional[uuid.UUID] = uuid.UUID(raw_id)
    except (ValueError, AttributeError):
        jid = None
        prefix = raw_id.replace("-", "").lower()
        if len(prefix) < 8 or any(c not in "0123456789abcdef" for c in prefix):
            print(f"not a job id: {job_id!r} (need a full uuid, or an id prefix of "
                  f">=8 hex chars)", file=sys.stderr)
            return 2
    led = await PostgresLedger.connect(DSN)
    try:
        if jid is None:
            matches = await led.resolve_job_prefix(prefix)
            if not matches:
                print(f"no such job: {job_id}", file=sys.stderr)
                return 1
            if len(matches) > 1:
                print(f"ambiguous job id prefix {job_id!r} — candidates:", file=sys.stderr)
                for m in matches:
                    print(f"  {m}", file=sys.stderr)
                return 2
            jid = uuid.UUID(matches[0])
        st = await led.get_status(jid)
        if not st:
            print(f"no such job: {job_id}", file=sys.stderr)
            return 1
        out = {
            "job": str(st.get("id")),
            "status": st.get("status"),
            "to_agent": st.get("to_agent"),
            "repo_id": st.get("repo_id"),
            "artifact_ref": st.get("artifact_ref"),
            # base_ref carries the anchor the caller passed (--base-ref); base_sha is a
            # distinct column not populated on this path, so binding uses base_ref.
            "base_ref": st.get("base_ref"),
            "base_sha": st.get("base_sha"),
            "attempts": [{"status": a.get("status")} for a in (st.get("attempts") or [])],
        }
        print(json.dumps(out))
        return 0
    finally:
        await led.close()


def main(argv: Optional[list[str]] = None) -> int:
    raw = sys.argv[1:] if argv is None else argv
    # `mxr get <job_id>` — structured status read (D1). Special-cased ABOVE the flat
    # positional parser so the established `mxr <agent> <task>` interface is untouched.
    if raw and raw[0] == "get":
        gp = argparse.ArgumentParser(prog="mxr get",
                                     description="print a job's status as JSON")
        gp.add_argument("job_id", help="the job uuid, or an id prefix of >=8 hex chars "
                                       "(from a prior `mxr` submit; hyphens optional)")
        gargs = gp.parse_args(raw[1:])
        return asyncio.run(get_job(gargs.job_id))

    # `mxr skillselect <repo_id> <changed-path>...` — the +learning rung READ path (build
    # plan Step 4). Routed through mxr so it inherits the runtime venv + PYTHONPATH +
    # MYNDAIX_DSN exactly like every other entry point (bare `python3 -m runtime.skillselect`
    # would not resolve the package in play-review's hook env, and the package lives here
    # regardless of which repo is under review). Special-cased ABOVE the flat agent/task
    # parser, like `get`; skillselect fails OPEN to empty stdout. PLAY_NONCE/PLAY_ID/PLAY_GATE
    # pass through the inherited env.
    if raw and raw[0] == "skillselect":
        from runtime import skillselect
        return skillselect.main(["skillselect", *raw[1:]])

    # `mxr capture-record ...` — auto-capture INSTRUMENTATION (observe-only). Same routing
    # rationale as skillselect (inherits venv/PYTHONPATH/DSN via mxr); fails OPEN, never blocks
    # a review. Records cross-family-agreed rule:<tag> signals; never opens a PR (no proposer yet).
    if raw and raw[0] == "capture-record":
        from runtime import capturerecord
        return capturerecord.main(["capture-record", *raw[1:]])

    # `mxr outcome-record ...` — outcomes-ledger INSTRUMENTATION (the per-finding OUTCOME LABEL
    # recorder). Same routing rationale as capture-record (inherits venv/PYTHONPATH/DSN via mxr);
    # fails OPEN, HARD no-op in gate mode, never opens a PR. Records finding:<tag> lines from BOTH
    # families into finding_outcome (CLOSE + OPEN) and prints the recorded keys for play-review.
    # `mxr outcome-stats` is checked BEFORE `outcome` because the flat parser would read "-stats" as
    # a prefix; both are special-cased above the flat agent/task parser like get/skillselect.
    if raw and raw[0] == "outcome-record":
        from runtime import outcomerecord
        return outcomerecord.main(["outcome-record", *raw[1:]])
    if raw and raw[0] == "outcome-stats":
        from runtime import outcomerecord
        return outcomerecord.stats_main(["outcome-stats", *raw[1:]])
    # `mxr labelqueue` — read-only browser of findings awaiting a human label, clustered by
    # (rule_tag, family) with paste-ready keys (label-throughput PR-A §2c). OPERATOR tier:
    # fail-CLOSED (exit 2) if the ledger is unreachable.
    if raw and raw[0] == "labelqueue":
        from runtime import outcomerecord
        return outcomerecord.labelqueue_main(["labelqueue", *raw[1:]])
    # `mxr outcome <key12> real|fp|wontfix` (single) or `mxr outcome <kind> <key12>...` (batch) —
    # the human's per-finding label, ALL kinds routed through the fence's confirm_outcome
    # (label-throughput PR-A D-1: `real` is the gating numerator and finally has a caller).
    if raw and raw[0] == "outcome":
        from runtime import outcomerecord
        return outcomerecord.label_main(["outcome", *raw[1:]])

    # `mxr knowledge-ingest / recall / knowledge-rebuild / curate` — the curator rung
    # (docs/curator-design.md v0.4). Same routing rationale as the verbs above (inherits
    # venv/PYTHONPATH/DSN via mxr). Operator verbs: unknown scope is a HARD error (exit 2),
    # never fail-open — misconfiguration must not read as "no knowledge". `curate` is the
    # deterministic guard around the pool's curator agent (stage-in -> dispatch -> promote).
    if raw and raw[0] == "knowledge-ingest":
        from runtime import knowledgerecord
        return knowledgerecord.ingest_main(["knowledge-ingest", *raw[1:]])
    if raw and raw[0] == "knowledge-rebuild":
        from runtime import knowledgerecord
        return knowledgerecord.rebuild_main(["knowledge-rebuild", *raw[1:]])
    if raw and raw[0] == "recall":
        from runtime import knowledgerecord
        return knowledgerecord.recall_main(["recall", *raw[1:]])
    if raw and raw[0] == "knowledge-index":
        from runtime import knowledgerecord
        return knowledgerecord.index_main(["knowledge-index", *raw[1:]])
    if raw and raw[0] == "curate":
        from runtime import curate
        return curate.main(["curate", *raw[1:]])

    # `mxr review <agent> --repo <path|basename> ...` — the review-context verb
    # (docs/mxr-review-context-design.md D6): stage a de-linked read-only snapshot of the
    # reviewed tip as the CONFINED reviewer's cwd, build the objective-above-fence prompt
    # with the nonce-fenced range diff, dispatch, tear down. Replaces the hand-embed
    # `mxr kilabz "$(cat prompt+diff)"` workflow end-to-end.
    if raw and raw[0] == "review":
        from runtime import review
        return review.main(["review", *raw[1:]])

    # `mxr review-stage <repo> <tip> | review-teardown <dir> | review-reap` — the STAGING
    # primitives for play-review.sh (PR-2). play-review runs its OWN review pipeline (fences,
    # skillselect, oracle-inline, triage), so it wants only the exporter, not the full `review`
    # verb. Routed through mxr so the hook env inherits the runtime venv + PYTHONPATH + DSN
    # (bare `python3 -m runtime.staging` would not resolve). stage prints the snapshot dir;
    # teardown refuses anything not a review-* dir under the staging root; reap fails CLOSED
    # if the ledger is unreachable (never blind mtime-reap).
    if raw and raw[0] in ("review-stage", "review-teardown", "review-reap"):
        from runtime import staging
        sub = {"review-stage": "stage", "review-teardown": "teardown",
               "review-reap": "reap"}[raw[0]]
        return staging.main(["staging", sub, *raw[1:]])

    p = argparse.ArgumentParser(
        prog="mxr", description='submit a task to the MyndAIX runtime',
        epilog='for a task that starts with a dash, use --:  mxr recon -- "-v explain"')
    p.add_argument("agent", help="roster agent id (e.g. recon, higgsfield)")
    p.add_argument("task", nargs="?", help="the prompt / task text (or use --prompt-file)")
    p.add_argument("--prompt-file", metavar="PATH", dest="prompt_file",
                   help="read the task text from this file instead of argv — sidesteps the OS "
                        "argv/env size ceiling (E2BIG) for large embedded diffs/reviews "
                        "(issue #83); trusted operator input, read verbatim")
    p.add_argument("--image", metavar="URL",
                   help="input image url (media agents, e.g. higgsfield image->video)")
    p.add_argument("--application", metavar="PATH",
                   help="override the agent's media application/model path")
    p.add_argument("--motion-id", metavar="UUID", dest="motion_id",
                   help="DoP camera-preset uuid (higgsfield/stitcher); see GET /v1/motions")
    p.add_argument("--motion-strength", metavar="N", dest="motion_strength", type=float,
                   help="DoP motion strength, 0.3 (subtle) to 1.0 (dramatic)")
    p.add_argument("--shotlist", metavar="PATH",
                   help="path to a JSON shot-list (stitcher): ordered list of shot objects")
    p.add_argument("--end-card", metavar="URL", dest="end_card",
                   help="branded end-card image URL to append (stitcher; http(s) only)")
    p.add_argument("--repo", metavar="ID", dest="repo_id",
                   help="repo bucket id for per-repo concurrency (omitted -> cap-exempt)")
    p.add_argument("--base-ref", metavar="REF", dest="base_ref",
                   help="base git ref/SHA the work is anchored to (e.g. the reviewed tip)")
    p.add_argument("--staged-workdir", metavar="DIR", dest="staged_workdir",
                   help="a staging dir the CALLER created for this job's cwd — honored ONLY "
                        "by staging-cwd adapters (kilabz/lobster/curator), MUST resolve "
                        "strictly inside $MYNDAIX_STAGING_ROOT, and fails the job TERMINAL "
                        "otherwise (it cannot select an arbitrary cwd)")
    args = p.parse_args(raw)
    # exactly ONE task source: the positional or --prompt-file (operator error -> exit 2).
    if (args.task is None) == (args.prompt_file is None):
        p.error("provide exactly one of <task> or --prompt-file")
    task = args.task
    if args.prompt_file:
        try:
            task = Path(args.prompt_file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:   # UnicodeDecodeError is a ValueError, not OSError
            p.error(f"--prompt-file: {e}")
        if not task.strip():
            p.error("--prompt-file: file is empty")
    return asyncio.run(submit(args.agent, task, context=_build_context(args),
                              repo_id=args.repo_id, base_ref=args.base_ref))


if __name__ == "__main__":
    raise SystemExit(main())
