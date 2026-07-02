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
from typing import Optional

from runtime.contracts import TransportEnvelope
from runtime.ledger.postgres_store import PostgresLedger
from runtime.registry import REGISTRY

DSN = os.environ.get("MYNDAIX_DSN", "postgresql://localhost/runtime")


async def submit(agent: str, task: str, *, context: Optional[dict] = None,
                 repo_id: Optional[str] = None, base_ref: Optional[str] = None,
                 timeout_s: Optional[float] = None) -> int:
    # the SYNC wait for the job to finish. Default 180s for interactive ops; the review path
    # (play-review) sets MXR_TIMEOUT_S higher so mxr doesn't abandon a slow review BEFORE the
    # agent's own ~300s exec cap. Parsed HERE, not in the import-time default arg — an empty or
    # malformed exported MXR_TIMEOUT_S would otherwise crash float() at import and take down the
    # WHOLE cli, including `mxr get`/`mxr skillselect` that never submit a job (kilabz+oracle).
    if timeout_s is None:
        raw = os.environ.get("MXR_TIMEOUT_S") or ""
        try:
            timeout_s = float(raw) if raw else 180.0
        except ValueError:
            print(f"warning: invalid MXR_TIMEOUT_S={raw!r}, using 180", file=sys.stderr)
            timeout_s = 180.0
    if agent not in REGISTRY:
        roster = ", ".join(sorted(REGISTRY))
        print(f"unknown agent '{agent}'. roster: {roster}", file=sys.stderr)
        return 2

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
            return 1

        if st["status"] == "done":
            reply = next((o["body"] for o in (st.get("outbound") or [])), None)
            if reply is not None:
                print(reply)
            for o in (st.get("outbound") or []):       # mark delivered so it doesn't linger
                if o["status"] == "pending":
                    await led.mark_outbound_sent(o["id"], f"cli-{o['id']}")
            return 0

        # failed/dead: surface WHY (the agent's error output, from the attempt)
        err = next((a.get("text") for a in (st.get("attempts") or [])
                    if a.get("status") == "failed" and a.get("text")), None)
        if err:
            print(err.strip(), file=sys.stderr)
        print(f"(job {st['status']})", file=sys.stderr)
        return 1
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
    return ctx


async def get_job(job_id: str) -> int:
    """`mxr get <job_id>` -> structured JSON of the job's status, including
    artifact_ref + base_sha. The fix stage (orchestrator/play-fix.sh) reads the
    diff artifact from HERE - via the ledger, parsed as JSON - NEVER by grepping a
    reply body an agent controls (a spoofable path is a security hole, not a bug)."""
    try:
        jid = uuid.UUID(job_id)
    except (ValueError, AttributeError):
        print(f"not a job id: {job_id!r}", file=sys.stderr)
        return 2
    led = await PostgresLedger.connect(DSN)
    try:
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
        gp.add_argument("job_id", help="the job uuid (from a prior `mxr` submit)")
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
    # `mxr outcome <finding_key_prefix> fp|wontfix` — the human's per-finding dismissal label.
    if raw and raw[0] == "outcome":
        from runtime import outcomerecord
        return outcomerecord.dismiss_main(["outcome", *raw[1:]])

    p = argparse.ArgumentParser(
        prog="mxr", description='submit a task to the MyndAIX runtime',
        epilog='for a task that starts with a dash, use --:  mxr recon -- "-v explain"')
    p.add_argument("agent", help="roster agent id (e.g. recon, higgsfield)")
    p.add_argument("task", help="the prompt / task text")
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
    args = p.parse_args(raw)
    return asyncio.run(submit(args.agent, args.task, context=_build_context(args),
                              repo_id=args.repo_id, base_ref=args.base_ref))


if __name__ == "__main__":
    raise SystemExit(main())
