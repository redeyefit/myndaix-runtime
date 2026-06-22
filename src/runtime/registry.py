"""Agent registry - the roster as DATA (C0/C5). Adding an agent is a new
AgentSpec row, never a spine edit (the non-negotiable principle). Authority
drives behavior; reach drives the adapter.

These are seed values from DESIGN.md S5; the live registry should load from
config/DB so the roster stays flexible. Models/commands change here, not in code.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel

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


# v1 seed roster (data - expected to change). See DESIGN.md S5.
V1_ROSTER: list[AgentSpec] = [
    AgentSpec(agent_id="lobster", reach=Reach.CLI, authority=Authority.CONTROLLER,
              model="opus", role="orchestration/judgment",
              adapter={"kind": "cli", "argv": ["claude", "-p", "--output-format", "text"],
                       "prompt_channel": "stdin"}),
    AgentSpec(agent_id="mack", reach=Reach.CLI, authority=Authority.WORKSPACE_ACTOR,
              model="opus", role="hands-on builder",
              adapter={"kind": "cli", "argv": ["claude"], "prompt_channel": "stdin"}),
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
              adapter={"kind": "cli", "argv": ["agy", "-p"], "prompt_channel": "arg"}),
    AgentSpec(agent_id="recon", reach=Reach.API, authority=Authority.COMPOSITE,
              model="sonar-pro+claude", role="research (read-only)",
              profile=Profile(cost_budget=5.0),
              adapter={"kind": "api", "endpoint": "https://api.perplexity.ai/chat/completions",
                       "secret_ref": "PERPLEXITY_API_KEY", "model": "sonar-pro"}),
]

REGISTRY: dict[str, AgentSpec] = {a.agent_id: a for a in V1_ROSTER}


def get(agent_id: str) -> Optional[AgentSpec]:
    return REGISTRY.get(agent_id)
