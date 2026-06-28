"""Agent registry - the roster as DATA (C0/C5). Adding an agent is a new
AgentSpec row, never a spine edit (the non-negotiable principle). Authority
drives behavior; reach drives the adapter.

These are seed values from DESIGN.md S5; the live registry should load from
config/DB so the roster stays flexible. Models/commands change here, not in code.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, field_validator

from runtime.contracts import Authority, Profile, Reach


class AgentSpec(BaseModel):
    agent_id: str
    reach: Reach
    authority: Authority
    model: str
    role: str
    profile: Profile = Profile()
    # adapter is intentionally a dict: cli {argv, prompt_channel, env_passthrough?} |
    # api {endpoint, secret_ref, model} (validated by the runner's adapter layer, not the spine).
    # env_passthrough (cli): env vars THIS agent is allowed to inherit through the P2 scrub —
    # its own auth key(s) only. Everything else (sibling agents' secrets) is dropped. See runner._cli_env.
    adapter: dict[str, Any]

    @field_validator("agent_id")
    @classmethod
    def _reserve_api_namespace(cls, v: str) -> str:
        # 'api:' is reserved for API job ownership (created_by = 'api:<principal>').
        # Forbidding it here keeps that namespace airtight: an agent (or its sub-jobs)
        # can never mint a created_by that an API principal could then read.
        if v.startswith("api:"):
            raise ValueError("agent_id must not start with the reserved 'api:' prefix")
        return v


# v1 seed roster (data - expected to change). See DESIGN.md S5.
V1_ROSTER: list[AgentSpec] = [
    # CLI agents declare env_passthrough = their OWN auth key only (the rest of the pool's
    # env — incl. sibling agents' secrets — is scrubbed by runner._cli_env). claude/codex/agy
    # also accept $HOME login; declaring the key keeps env-auth deploys working too.
    AgentSpec(agent_id="lobster", reach=Reach.CLI, authority=Authority.CONTROLLER,
              model="opus", role="orchestration/judgment",
              adapter={"kind": "cli", "argv": ["claude", "-p", "--output-format", "text"],
                       "prompt_channel": "stdin", "env_passthrough": ["ANTHROPIC_API_KEY"]}),
    AgentSpec(agent_id="mack", reach=Reach.CLI, authority=Authority.WORKSPACE_ACTOR,
              model="opus", role="hands-on builder",
              adapter={"kind": "cli", "argv": ["claude", "-p"], "prompt_channel": "stdin",
                       "env_passthrough": ["ANTHROPIC_API_KEY"]}),
    AgentSpec(agent_id="mini", reach=Reach.CLI, authority=Authority.WORKSPACE_ACTOR,
              model="claude", role="pipeline builder",
              adapter={"kind": "cli", "argv": ["claude", "-p"], "prompt_channel": "stdin",
                       "env_passthrough": ["ANTHROPIC_API_KEY"]}),
    AgentSpec(agent_id="kilabz", reach=Reach.CLI, authority=Authority.RESPONDER,
              model="gpt-5.5", role="code reviewer (read-only)",
              adapter={"kind": "cli", "argv": ["codex", "exec", "--sandbox", "read-only",
                       "--skip-git-repo-check"], "prompt_channel": "stdin",
                       "env_passthrough": ["OPENAI_API_KEY"]}),
    AgentSpec(agent_id="codex", reach=Reach.CLI, authority=Authority.WORKSPACE_ACTOR,
              model="gpt-5.5", role="builder/debugger",
              # --sandbox workspace-write: codex's own seatbelt (P2) — writes scoped to the
              # worktree cwd (+ tmp); executed-command network egress is restricted ONLY if the
              # host ~/.codex config hasn't re-enabled it, so treat egress as best-effort, NOT
              # guaranteed. Per design §7 the sandbox is weak; the human merge gate is the real
              # backstop. PR-4 pins `-c sandbox_workspace_write.network_access=false` here so the
              # fixer's executed commands can't phone home from a config-default-on host (best-
              # effort: a hardened ~/.codex could still override; the human merge gate remains the
              # real backstop). The env-scrub still denies this process every secret it didn't
              # declare, regardless of sandbox config.
              # scratch_home: run under a throwaway HOME seeded with only codex's auth (PR-4
              # fix-stage containment) so an injected fix-list can't read ~/.ssh/~/.aws/~/.myndaix.
              adapter={"kind": "cli", "argv": ["codex", "exec", "--sandbox", "workspace-write",
                       "-c", "sandbox_workspace_write.network_access=false",
                       "--skip-git-repo-check"], "prompt_channel": "stdin",
                       "env_passthrough": ["OPENAI_API_KEY"], "scratch_home": True}),
    AgentSpec(agent_id="oracle", reach=Reach.CLI, authority=Authority.RESPONDER,
              model="gemini-3.1-pro", role="reviewer/vision",
              # `agy` is the Gemini CLI (the standalone gemini-cli individual tier was retired).
              # PIN --model: the bare `agy -p` ran agy's DEFAULT (Gemini 3.5 Flash — fast but shallow),
              # NOT the gemini-3.1-pro this spec declares. Passing the model the picker lists gives
              # Oracle real review depth. (Debug-first 2026-06-28: the old "~50KB prompt -> empty"
              # field note did NOT reproduce — agy returns correct output at 662KB, so there is no
              # packet ceiling to engineer around; the only real handicap was the un-pinned model.)
              adapter={"kind": "cli", "argv": ["agy", "--model", "Gemini 3.1 Pro (High)", "-p"],
                       "prompt_channel": "arg",
                       "env_passthrough": ["GEMINI_API_KEY", "GOOGLE_API_KEY"]}),
    AgentSpec(agent_id="recon", reach=Reach.API, authority=Authority.COMPOSITE,
              model="sonar-pro+claude", role="research (read-only)",
              profile=Profile(cost_budget=5.0),
              adapter={"kind": "api", "endpoint": "https://api.perplexity.ai/chat/completions",
                       "secret_ref": "PERPLEXITY_API_KEY", "model": "sonar-pro"}),
    # Higgsfield async media queue (image/text->video). reach=API but adapter.kind
    # is 'higgsfield', so the runner routes it to invoke_higgsfield, not invoke_api.
    # Pinned to DoP/lite for v1 (cheapest path); premium models are a later row.
    AgentSpec(agent_id="higgsfield", reach=Reach.API, authority=Authority.RESPONDER,
              model="dop-lite", role="image/text->video generation",
              profile=Profile(timeout_s=600, cost_budget=2.0),
              # non_idempotent: the submit POST CHARGES credits and is NOT deduplicated. A worker
              # CRASH after the charged submit (no TERMINAL Result returned) would otherwise expire
              # the lease and let reclaim REQUEUE this RESPONDER -> a second charged submit (double
              # charge, bounded only by MAX_ATTEMPTS). The flag makes _requeue_safe return False, so
              # a crashed/expired paid job goes dead+surfaced (recover via `mxr get <jid>`), never
              # auto-resubmitted (cross-family review CRITICAL — clean TERMINAL returns were already
              # safe; this closes the worker-CRASH window).
              adapter={"kind": "higgsfield",
                       "base": "https://platform.higgsfield.ai",
                       "secret_ref": "HF_KEY",
                       "application": "/higgsfield-ai/dop/lite",
                       "non_idempotent": True}),
    # Stitcher: long video from a shot-list (generate per shot -> last-frame chain ->
    # ffmpeg concat -> deterministic brand overlay). reach=API + adapter.kind 'stitch'
    # routes to invoke_stitch. authority=WORKSPACE_ACTOR -> NEVER auto-retried (so the
    # multi-segment spend is never silently re-charged); it does file I/O in a workspace.
    AgentSpec(agent_id="stitcher", reach=Reach.API, authority=Authority.WORKSPACE_ACTOR,
              model="dop-lite", role="long video via chained clips",
              # 2400s: 5 sequential ~3-4min DoP renders + downloads/chains + concat, with margin.
              # NOTE: read via spec.profile.timeout_s in invoke_stitch — the spine's lease_job
              # does NOT apply Profile.timeout_s to the Job (job.timeout_s stays the 300s default).
              profile=Profile(timeout_s=2400, cost_budget=5.0),
              adapter={"kind": "stitch",
                       "base": "https://platform.higgsfield.ai",
                       "secret_ref": "HF_KEY",
                       "application": "/higgsfield-ai/dop/lite",
                       "max_segments": 12,
                       "non_idempotent": True}),   # paid; WORKSPACE_ACTOR already non-requeue (belt)
]

REGISTRY: dict[str, AgentSpec] = {a.agent_id: a for a in V1_ROSTER}


def get(agent_id: str) -> Optional[AgentSpec]:
    return REGISTRY.get(agent_id)
