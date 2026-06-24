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
    # adapter is intentionally a dict: cli {argv, prompt_channel} | api {endpoint, secret_ref, model}
    # (validated by the runner's adapter layer, not the spine)
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
    AgentSpec(agent_id="lobster", reach=Reach.CLI, authority=Authority.CONTROLLER,
              model="opus", role="orchestration/judgment",
              adapter={"kind": "cli", "argv": ["claude", "-p", "--output-format", "text"],
                       "prompt_channel": "stdin"}),
    AgentSpec(agent_id="mack", reach=Reach.CLI, authority=Authority.WORKSPACE_ACTOR,
              model="opus", role="hands-on builder",
              adapter={"kind": "cli", "argv": ["claude", "-p"], "prompt_channel": "stdin"}),
    AgentSpec(agent_id="mini", reach=Reach.CLI, authority=Authority.WORKSPACE_ACTOR,
              model="claude", role="pipeline builder",
              adapter={"kind": "cli", "argv": ["claude", "-p"], "prompt_channel": "stdin"}),
    AgentSpec(agent_id="kilabz", reach=Reach.CLI, authority=Authority.RESPONDER,
              model="gpt-5.5", role="code reviewer (read-only)",
              adapter={"kind": "cli", "argv": ["codex", "exec", "--sandbox", "read-only",
                       "--skip-git-repo-check"], "prompt_channel": "stdin"}),
    AgentSpec(agent_id="codex", reach=Reach.CLI, authority=Authority.WORKSPACE_ACTOR,
              model="gpt-5.5", role="builder/debugger",
              adapter={"kind": "cli", "argv": ["codex", "exec", "--skip-git-repo-check"],
                       "prompt_channel": "stdin"}),
    AgentSpec(agent_id="oracle", reach=Reach.CLI, authority=Authority.RESPONDER,
              model="gemini-3.1-pro", role="reviewer/vision",
              # `agy` is the Gemini CLI (the standalone gemini-cli individual tier was retired)
              adapter={"kind": "cli", "argv": ["agy", "-p"], "prompt_channel": "arg"}),
    AgentSpec(agent_id="recon", reach=Reach.API, authority=Authority.COMPOSITE,
              model="sonar-pro+claude", role="research (read-only)",
              profile=Profile(cost_budget=5.0),
              adapter={"kind": "api", "endpoint": "https://api.perplexity.ai/chat/completions",
                       "secret_ref": "PERPLEXITY_API_KEY", "model": "sonar-pro"}),
    # Higgsfield async media queue (image/text->video). reach=API but adapter.kind
    # is 'higgsfield', so the runner routes it to invoke_higgsfield, not invoke_api.
    # `higgsfield` stays the cheapest default (DoP/lite). v2 adds premium models as pure
    # rows behind the SAME runner — proving "a new model is a new row, never a spine edit".
    # Only paths CONFIRMED live (the 400/422-signature probe, 2026-06-23 research) are
    # listed; inferred premium paths are commented out below until gallery-verified.
    AgentSpec(agent_id="higgsfield", reach=Reach.API, authority=Authority.RESPONDER,
              model="dop-lite", role="image/text->video generation",
              profile=Profile(timeout_s=600, cost_budget=2.0),
              adapter={"kind": "higgsfield",
                       "base": "https://platform.higgsfield.ai",
                       "secret_ref": "HF_KEY",
                       "application": "/higgsfield-ai/dop/lite"}),
    # DoP Standard — same body family + a `duration` knob (documented). `params` rides
    # into the submit body via the runner's param-merge.
    AgentSpec(agent_id="higgsfield-dop-std", reach=Reach.API, authority=Authority.RESPONDER,
              model="dop-standard", role="image->video generation (DoP standard)",
              profile=Profile(timeout_s=600, cost_budget=2.0),
              adapter={"kind": "higgsfield",
                       "base": "https://platform.higgsfield.ai",
                       "secret_ref": "HF_KEY",
                       "application": "/higgsfield-ai/dop/standard",
                       "params": {"duration": 5}}),
    # Kling 2.1 Pro — premium tier (~$0.40/clip); path + body {prompt,image_url} documented.
    AgentSpec(agent_id="higgsfield-kling", reach=Reach.API, authority=Authority.RESPONDER,
              model="kling-2.1-pro", role="premium image->video generation (Kling 2.1 Pro)",
              profile=Profile(timeout_s=600, cost_budget=4.0),
              adapter={"kind": "higgsfield",
                       "base": "https://platform.higgsfield.ai",
                       "secret_ref": "HF_KEY",
                       "application": "/kling-video/v2.1/pro/image-to-video"}),
    # Seedance 1.0 Pro — premium tier; path + body {prompt,image_url} documented.
    AgentSpec(agent_id="higgsfield-seedance", reach=Reach.API, authority=Authority.RESPONDER,
              model="seedance-1-pro", role="premium image->video generation (Seedance 1.0 Pro)",
              profile=Profile(timeout_s=600, cost_budget=4.0),
              adapter={"kind": "higgsfield",
                       "base": "https://platform.higgsfield.ai",
                       "secret_ref": "HF_KEY",
                       "application": "/bytedance/seedance/v1/pro/image-to-video"}),
    # NOT added — exact model_id NOT in public docs (LOW-confidence inferences only); verify
    # in the authed gallery's "API" tab before promoting to a row, else a 404 "Model not found":
    #   Veo 3.1 i2v      ~ /google/veo/v3.1/image-to-video
    #   Kling 3.0 i2v    ~ /kling-video/v3/pro/image-to-video
    #   Seedance 2.0 i2v ~ /bytedance/seedance/v2/pro/image-to-video
    #   WAN 2.5 i2v      ~ /wan/v2.5/image-to-video
]

REGISTRY: dict[str, AgentSpec] = {a.agent_id: a for a in V1_ROSTER}


def get(agent_id: str) -> Optional[AgentSpec]:
    return REGISTRY.get(agent_id)
