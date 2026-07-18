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
    # env — incl. sibling agents' secrets — is scrubbed by runner._cli_env).
    # CLAUDE AGENTS USE A LONG-LIVED SUBSCRIPTION TOKEN (Claude Max), NOT a metered API key.
    # WHY not the plain Max login: claude's interactive Max login lives in the macOS KEYCHAIN,
    # which the launchd POOL can't use reliably — the keychain token is stale in the daemon
    # session while claude's file creds are only read when the keychain is absent. So every
    # controller review died at the lobster canary on the Mini ("401 Invalid authentication
    # credentials"), surfaced 2026-07-02 by the outcomes-rung E2E test. The dead-simple headless
    # fix (Anthropic's own, `claude setup-token`): mint a LONG-LIVED token FROM THE SUBSCRIPTION
    # (flat-rate, NOT a metered API key — philosophy-aligned) and hand it to claude via
    # CLAUDE_CODE_OAUTH_TOKEN. claude sends it as a bearer token and tries it FIRST (verified:
    # a set token is used before any keychain/file cred), so the stale keychain is never reached.
    # Provision: `claude setup-token` on the Mini -> put the token in ~/.myndaix/.secrets as
    # CLAUDE_CODE_OAUTH_TOKEN -> the scrub passes it through (declared below). Long-lived = no
    # weekly re-auth churn (unlike the agy/keychain OAuth). codex/agy still declare their own keys.
    AgentSpec(agent_id="lobster", reach=Reach.CLI, authority=Authority.CONTROLLER,
              model="sonnet", role="orchestration/judgment",
              # PIN --model (the oracle lesson, agy below): a bare `claude -p` runs the HOST's
              # default model — which drifts (it tracks whatever the operator last picked
              # interactively, e.g. a limited-window preview model) and differs per machine.
              # Triage is structured extraction on the automerge-gate path: it needs the SAME
              # model on both hosts, and sonnet-tier is sufficient for it (optimal-team brief);
              # auth is flat-rate (Max plan), so this is about determinism + preserving premium
              # model quota for hard work, not dollars.
              # TOOL CONFINEMENT (core-audit 2026-07-06): lobster is CONTROLLER-authority and runs on
              # the review/triage path, whose inputs are UNTRUSTED (PR diffs + review findings). A bare
              # `claude -p` inherits the operator's real $HOME + ~22 MCP servers (filesystem/firecrawl/
              # github) + full Bash/Write — the identical read-only-sandbox bypass the curator was
              # hardened against, but on a MORE dangerous agent (untrusted input -> injection -> RCE/
              # exfil). lobster is read-only judgment (text verdict from the prompt), so it takes the
              # SAME proven confinement as curator (minus staging_cwd): --tools Read Glob Grep (hard
              # built-in whitelist; no Write/Bash/net), --strict-mcp-config (ignore the inherited MCP
              # fleet), --safe-mode (no project/local hooks/plugins), scratch_home (empty throwaway HOME).
              # staging_cwd "optional" (mxr-review-context D3/D5): a caller MAY stage a
              # de-linked, non-writable snapshot of the reviewed tip as the cwd (runner-
              # validated; workdir ABSENT → scratch cwd, exactly the pre-staging review).
              # lobster-with-snapshot at triage is the fabrication-killer: the CONFINED
              # synthesis agent verifies both reviews' claims against real code. The tool
              # whitelist above is unchanged — a cwd is not a permission.
              adapter={"kind": "cli", "argv": ["claude", "-p", "--model", "sonnet",
                       "--output-format", "text", "--tools", "Read", "Glob", "Grep",
                       "--strict-mcp-config", "--safe-mode"],
                       "prompt_channel": "stdin", "scratch_home": True,
                       "staging_cwd": "optional",
                       "env_passthrough": ["CLAUDE_CODE_OAUTH_TOKEN"]}),  # long-lived subscription token
    AgentSpec(agent_id="mack", reach=Reach.CLI, authority=Authority.WORKSPACE_ACTOR,
              model="opus", role="hands-on builder",
              adapter={"kind": "cli", "argv": ["claude", "-p"], "prompt_channel": "stdin",
                       "env_passthrough": ["CLAUDE_CODE_OAUTH_TOKEN"]}),  # subscription token (roster header)
    AgentSpec(agent_id="mini", reach=Reach.CLI, authority=Authority.WORKSPACE_ACTOR,
              model="claude", role="pipeline builder",
              adapter={"kind": "cli", "argv": ["claude", "-p"], "prompt_channel": "stdin",
                       "env_passthrough": ["CLAUDE_CODE_OAUTH_TOKEN"]}),  # subscription token (roster header)
    AgentSpec(agent_id="kilabz", reach=Reach.CLI, authority=Authority.RESPONDER,
              model="gpt-5.5", role="code reviewer (read-only)",
              # PIN model + reasoning effort: without `-c model=...` codex runs the HOST's
              # ~/.codex/config.toml model — true-by-luck on one machine, unverified on the
              # other. The reviewer family must be deterministic from the REPO on every host.
              # gpt-5.5 stays (NOT the brief's 5.3-codex cost swap: codex auth is a flat-rate
              # ChatGPT subscription — Pro since 2026-07-03 (it sat on the FREE tier before,
              # which is what drained mid-cycle) — so the API-price argument is moot and a
              # downgrade would be a pure review-quality loss).
              # timeout_s=900: xhigh on a real review diff regularly exceeds the dead 300s
              # per-attempt default (2026-07-03: two killed attempts + one ok stranded a DONE
              # reply in the ledger while play-review's wait expired). invoke_cli uses THIS
              # profile timeout when job.timeout_s is unset (== the 300s field default); an
              # explicitly-set job.timeout_s wins in EITHER direction — see runner exec_timeout.
              profile=Profile(timeout_s=900),
              # staging_cwd "optional" (mxr-review-context D3/D5): a caller MAY stage a
              # de-linked, non-writable snapshot of the reviewed tip as the cwd so kilabz
              # verifies the fenced diff against real code instead of an empty scratch dir
              # (runner-validated; absent → scratch = pre-staging behavior). codex's
              # --sandbox read-only seatbelt is unchanged (writes+net OS-denied); the
              # accepted §5 residual — read-only exec of snapshot entry points — is
              # capability-identical to its existing un-path-scoped Read.
              adapter={"kind": "cli", "argv": ["codex", "exec", "--sandbox", "read-only",
                       "-c", "model=gpt-5.5", "-c", "model_reasoning_effort=xhigh",
                       "--skip-git-repo-check"], "prompt_channel": "stdin",
                       "staging_cwd": "optional",
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
    AgentSpec(agent_id="curator", reach=Reach.CLI, authority=Authority.WORKSPACE_ACTOR,
              model="sonnet", role="corpus librarian (research/ folder-agent)",
              # The curator NEVER runs against the live corpus: `mxr curate` (curate.py) stages a
              # filtered copy under $MYNDAIX_STAGING_ROOT and passes context.workdir; invoke_cli
              # honors that ONLY because staging_cwd is declared here AND the path resolves inside
              # the staging namespace (fail-closed, no scratch fallback) — a registry row can NOT
              # opt into an arbitrary cwd (PR #39 invariant; curator-design v0.4 r3). The REAL
              # write boundary is the guard's diff-audit + the runtime-authored path-scoped
              # .claude/settings.json it writes into staging; these argv flags are the belt.
              # TOOL CONFINEMENT (BUILD FINDING 2026-07-06, gate-proven under a HOSTILE HOME;
              # cross-family reviewed). Every tool name is its OWN argv element. Three layers:
              #  1. `--tools Read Glob Grep` — the HARD built-in whitelist (NOT --allowedTools,
              #     which is only pre-APPROVAL: Write/Bash are default-available under it). --tools
              #     makes ONLY these three built-ins exist; write/bash/net are unavailable. Proven
              #     to hold even when an inherited ~/.claude/settings.json allows everything.
              #  2. `--strict-mcp-config` — ignore ALL inherited MCP servers (the operator's real
              #     ~/.claude.json has ~22, incl. filesystem/firecrawl/github — a read-only-sandbox
              #     BYPASS; the cross-family BLOCKER). Proven: a hostile MCP server is NOT spawned.
              #  3. scratch_home (runner) — an EMPTY throwaway HOME (claude auths via the env token),
              #     so NO inherited ~/.claude settings/hooks/MCP-config load at all (belt).
              #  4. `--safe-mode` — disable ALL customizations (project/local hooks, plugins,
              #     commands, agents) that 1-3 don't cover (cross-family re-review BLOCKER: a hook
              #     could run code outside the --tools whitelist). Proven functional (read works).
              # RESIDUAL (read-only-accepted, documented): the Read tool is NOT path-scoped, so an
              # injected brief could make it read an absolute host path (e.g. ~/.ssh) into the reply.
              # Bounded by NO EXTERNAL CHANNEL (net/bash/MCP all denied) — a read can only surface in
              # the operator's own local curate output, never exfiltrated. OS sandbox (sandbox-exec)
              # scoping filesystem reads to the staging dir is the recorded next hardening rung.
              # SHIPPING READ-ONLY (write/bash/net/out-of-tree-write/MCP all denied; in-tree read works).
              # TO ENABLE WRITE: add "Write", "Edit" to --tools — but that reopens the out-of-tree
              # Write-TOOL leak (Write isn't cwd-confined like Bash), so Write stays GATED pending a
              # working path-scope (the promote guard only bounds IN-tree; see design BUILD FINDING).
              profile=Profile(timeout_s=600),
              adapter={"kind": "cli",
                       "argv": ["claude", "-p", "--model", "sonnet", "--output-format", "text",
                                "--tools", "Read", "Glob", "Grep", "--strict-mcp-config",
                                "--safe-mode"],
                       "prompt_channel": "stdin", "staging_cwd": True, "scratch_home": True,
                       "env_passthrough": ["CLAUDE_CODE_OAUTH_TOKEN"]}),  # subscription token
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
    # mx-engine: the content-factory folder promoted to an agent (Rung 1). Option B — a
    # DETERMINISTIC bash pipeline (reelcopy -> reel), NOT a confined claude producer: it runs the
    # REAL mx-engine repo with its real env (Chrome/ffmpeg/.venv/personas/.ttscache), so it declares
    # NO --tools / scratch_home (those are the curator's read-only belt; the opposite need here).
    #  * WORKSPACE_ACTOR -> NEVER auto-retried: a reel render burns ElevenLabs TTS credits and is
    #    non-idempotent, so a transient failure must never double-charge (same guarantee stitcher
    #    /higgsfield rely on). NEVER dispatch mx-engine with a repo_id — a plain `mxr mx-engine
    #    "topic"` carries none, so worker.py's C5 gate makes NO worktree and the self-locating
    #    wrapper runs the real repo; a stray repo_id would spawn a useless checkout-copy worktree
    #    (missing the gitignored venv/personas) — the wrapper's absolute path still works, just wasteful.
    #  * The wrapper + child .sh scripts source ~/.myndaix/.secrets THEMSELVES, so no env_passthrough
    #    is needed — real HOME/PATH from the P2 env-base is enough for them to self-load the keys.
    #  * timeout_s 1500: copy (~30s Claude call) + one full narrated 9:16 render, with wide margin.
    # REVIEW (kilabz r1, PR #101): FOLDED — concurrent dispatches could corrupt a render (reelgen
    # shutil.rmtree's <repo>/reel-out/<topic> on start + a FIXED render port 8731); mx-produce.sh now
    # takes a single-holder mkdir lock (2nd concurrent run fails fast, stale-reclaims past 1800s so a
    # SIGKILL-on-timeout leak self-heals). ACCEPTED RESIDUALS (Rung-1, documented):
    #  (a) nothing FORCES repo_id absent — the "NEVER dispatch with repo_id" contract is only doc'd.
    #      Fail-closed/benign though: a logical --repo fails pre-invoke, an absolute-path --repo only
    #      wastes a worktree (wrapper + reel*.py resolve every path from $0/__file__, never cwd, so it
    #      still runs the REAL repo). Follow-up: a `no_worktree` adapter flag honored by worker.py.
    #  (b) the wrapper self-sources the FULL ~/.myndaix/.secrets (bypasses _cli_env's per-agent
    #      allowlist) — but that EQUALS the manual `./reel.sh` exposure (same operator/HOME/file) and
    #      the topic reaches an LLM as COPY, never shell (no injection path). Follow-up: scope
    #      mx-engine to a narrow secret file (ANTHROPIC/OAuth + ELEVENLABS only).
    # PRE-DEPLOY AUDIT (5-dim adversarial fleet, 17 findings): FOLDED a BLOCKER the unit tests + diff
    # review missed — mx-produce.sh staged reel.json in an ABSOLUTE tempdir, which reelgen serves to
    # headless Chrome over http.server(cwd=repo); an out-of-root path 404s -> blank frame -> the MX
    # head-clip gate aborts EVERY render. Fixed in mx-produce.sh: stage under a gitignored .mx-work/
    # inside the repo + pass a DIR-relative path (regression-tested NON-paid in test_mx_produce.sh).
    # Remaining live-only item (NOT code): headless Chrome runs under the launchd daemon with the
    # operator's default profile — works from a GUI shell, first-run/profile-lock/TCC UNPROVEN from the
    # pool; bounded (1500s timeout -> dead, never auto-retried), so the FIRST mxr mx-engine dispatch is
    # a supervised smoke (don't run it while interactive Chrome holds the same profile). Other 15
    # findings are all low, bounded, self-healing (dash/empty-topic footguns, sync-wait ~26min, lock
    # stale-reclaim TOCTOU, stderr-on-success, stale-dest artifact) — documented, none block go-live.
    AgentSpec(agent_id="mx-engine", reach=Reach.CLI, authority=Authority.WORKSPACE_ACTOR,
              model="pipeline", role="content factory (mx-engine folder-agent): topic -> narrated reel",
              profile=Profile(timeout_s=1500),
              adapter={"kind": "cli",
                       "argv": ["bash",
                                "/Users/stevenfernandez/code/active/mx-engine/mx-produce.sh"],
                       "prompt_channel": "arg"}),   # whole dispatch string -> $1 = topic
]

REGISTRY: dict[str, AgentSpec] = {a.agent_id: a for a in V1_ROSTER}


def get(agent_id: str) -> Optional[AgentSpec]:
    return REGISTRY.get(agent_id)
