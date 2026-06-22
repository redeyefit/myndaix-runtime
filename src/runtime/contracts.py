"""Core contracts for the MyndAIX Team Runtime.

These Pydantic models ARE the contracts (C0-C3) from DESIGN.md. The spine,
workers, transports, and adapters all speak in these types. Persistence lives
in ledger/schema.sql; the Command API (command_api.py) is the only writer.

Nothing here knows *how* an agent does its work - only how it's reached
(reach), what it's allowed to do (authority), and the shape of a job/result.
That separation is the whole point (the openclaw failure was coupling).
"""
from __future__ import annotations

import enum
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field


# -- C0: capability model -------------------------------------------------
class Reach(str, enum.Enum):
    """How an agent is invoked (drives the adapter + auth/cost)."""
    CLI = "cli"
    API = "api"


class Authority(str, enum.Enum):
    """What an agent may do - drives retry-safety, isolation, dispatch rights.
    This, not reach, is the load-bearing distinction."""
    RESPONDER = "responder"               # prompt->text, no side effects; auto-retry safe
    WORKSPACE_ACTOR = "workspace_actor"   # reads/writes files; gets a worktree; NEVER auto-retried
    CONTROLLER = "controller"             # may emit new dispatches (via Command API only)
    COMPOSITE = "composite"               # multiple internal calls; declares net authority


class Profile(BaseModel):
    """Cost/concurrency/timeout characteristics - consumed by C4."""
    timeout_s: int = 300
    concurrency_weight: int = 1
    cost_budget: Optional[float] = None   # api agents; None = no $ budget


# -- ledger state-machine enums (C2 / C4) --------------------------------
class JobStatus(str, enum.Enum):
    QUEUED = "queued"
    LEASED = "leased"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    DEAD = "dead"


class AttemptStatus(str, enum.Enum):
    OPEN = "open"
    OK = "ok"
    FAILED = "failed"


class OutboundStatus(str, enum.Enum):
    PENDING = "pending"
    LEASED = "leased"
    SENT = "sent"
    FAILED = "failed"


class ResultStatus(str, enum.Enum):
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    KILLED = "killed"
    NEEDS_HUMAN = "needs_human"


class ErrorClass(str, enum.Enum):
    RETRYABLE = "retryable"       # transient: network, rate-limit, read-only timeout
    TERMINAL = "terminal"         # bad-auth, validation, non-zero-exit on a mutation
    NEEDS_HUMAN = "needs_human"   # interactive/TTY prompt detected -> park, never loop


class Stream(str, enum.Enum):
    STDOUT = "stdout"
    STDERR = "stderr"
    HEARTBEAT = "heartbeat"


# -- C3: transport envelope ----------------------------------------------
class FormattingCaps(BaseModel):
    max_len: int = 2000
    chunking: bool = True


class TransportEnvelope(BaseModel):
    """Normalized inbound context. Transport semantics live HERE and must never
    leak into Job/agent fields - that leak was the Discord 'group -> lurk -> ghost'
    failure of 2026-06-21."""
    transport: str
    account: str
    sender_id: str
    channel_id: Optional[str] = None
    thread_id: Optional[str] = None
    reply_target: str
    provider_msg_id: Optional[str] = None
    dedupe_key: str
    formatting_caps: FormattingCaps = Field(default_factory=FormattingCaps)


# -- C1: invocation contract ---------------------------------------------
class Job(BaseModel):
    """A unit of work handed to invoke(). Workspace fields enable chaining:
    a child job sets base_ref = a prior job's artifact_ref, so its worktree is
    created from the previous step's output instead of the live tree."""
    id: UUID
    to_agent: str
    prompt: str
    context: dict[str, Any] = Field(default_factory=dict)
    # workspace (workspace-actors only)
    repo_id: Optional[str] = None
    base_ref: Optional[str] = None
    base_sha: Optional[str] = None
    worktree_path: Optional[str] = None
    timeout_s: int = 300
    attempt_no: int = 1


class Result(BaseModel):
    status: ResultStatus
    text: str = ""
    exit_code: Optional[int] = None
    error_class: Optional[ErrorClass] = None
    artifact_ref: Optional[str] = None   # e.g. a branch/patch for a workspace-actor diff
    cost: Optional[float] = None
    ms: Optional[int] = None
