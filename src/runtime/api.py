"""HTTP Command-API (DESIGN.md S4b) - the runtime as a service, with API-key auth.

FastAPI over the ledger's external verbs, so a transport (or any client) can submit
work and read status over the network while workers run as SEPARATE processes
against the same Postgres ledger. The Command API stays the SOLE ledger writer;
these endpoints just expose its verbs, hold no state of their own, and (the ledger
being the only shared state) scale out as N API + M worker instances.

Auth (fail-closed):
  * every endpoint requires an API key - `Authorization: Bearer <key>`; a missing or
    unknown key is a 401, and an EMPTY key store rejects everything. The interactive
    docs (`/docs`, `/openapi.json`) are disabled so nothing is reachable unauthenticated.
  * a key maps to a Principal {id, role}. API-submitted jobs are owned by the
    NAMESPACED id `api:<id>` - which can never collide with the ledger's provenance
    sentinels (`human`, an agent_id, an inbound_event id). A `client` reads ONLY its
    own `api:<id>` jobs; anything else (transport/agent-originated) is admin-only.
    Reading a job that isn't yours is a 404 (not 403) so ids never leak.
  * keys come from $MYNDAIX_API_KEYS ("token:principal:role,...") - STRICTLY parsed
    (exactly 3 non-empty fields, role client|admin, no duplicate token or id; any
    misconfig is a loud startup error, never a silent phantom/escalated key) - or are
    injected via create_app(api_keys=...). No keys live in source.

Serve:  MYNDAIX_API_KEYS="s3cret:alice:client" LEDGER_DSN=postgresql://localhost/runtime \\
            uvicorn runtime.api:app --port 8080
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Optional
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, field_validator

from runtime.contracts import TransportEnvelope
from runtime.ledger.postgres_store import PostgresLedger

_MAX_BODY = 100_000          # bound the payload so a huge body can't DoS the queue/storage
_API_NS = "api:"             # API ownership namespace (never collides with provenance)
_security = HTTPBearer(auto_error=False)  # we raise our own 401 (not 403) on no key


def _no_nul(v: str) -> str:
    # Postgres text can't hold NUL; reject it up front so it's a clean 422, not a 500
    if "\x00" in v:
        raise ValueError("NUL bytes are not allowed")
    return v


class Principal(BaseModel):
    id: str
    role: str = "client"   # "client" | "admin"


def load_api_keys(spec: str) -> dict[str, Principal]:
    """Parse $MYNDAIX_API_KEYS = 'token:principal:role,...'. STRICT + fail-loud: a
    security-critical parse must turn a misconfig into a startup error, never a
    silent phantom/escalated key. No token-as-id fallback, no duplicate token/id."""
    keys: dict[str, Principal] = {}
    ids: set[str] = set()
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) != 3:
            raise ValueError(f"API key entry must be token:principal:role, got {entry!r}")
        token, pid, role = (p.strip() for p in parts)
        if not token or not pid or not role:
            raise ValueError(f"API key entry has an empty field: {entry!r}")
        if role not in ("client", "admin"):
            raise ValueError(f"API key role must be client|admin, got {role!r}")
        if token in keys:
            raise ValueError("duplicate API token in MYNDAIX_API_KEYS")
        if pid in ids:
            raise ValueError(f"duplicate API principal id {pid!r} in MYNDAIX_API_KEYS")
        keys[token] = Principal(id=pid, role=role)
        ids.add(pid)
    return keys


# -- request models (bounded + validated: this is an exposed HTTP surface) -----
class InboundIn(BaseModel):
    """A transport-originated message: it's ingested (deduped) then queued."""
    sender_id: str = Field(min_length=1, max_length=200)
    dedupe_key: str = Field(min_length=1, max_length=200)   # non-empty, bounded
    body: str = Field(min_length=1, max_length=_MAX_BODY)
    to_agent: str = Field(min_length=1, max_length=100)
    transport: str = Field(default="http", max_length=50)
    account: str = Field(default="default", max_length=100)
    reply_target: Optional[str] = Field(default=None, max_length=200)

    _strip_nul = field_validator(
        "sender_id", "dedupe_key", "body", "to_agent", "transport", "account")(_no_nul)


class SubmitIn(BaseModel):
    """A direct (non-transport) job submission."""
    to_agent: str = Field(min_length=1, max_length=100)
    prompt: str = Field(min_length=1, max_length=_MAX_BODY)

    _strip_nul = field_validator("to_agent", "prompt")(_no_nul)


class JobOut(BaseModel):
    job_id: str


class JobStatusOut(BaseModel):
    """A client-safe ALLOWLIST of the status fields - so a future internal column
    added to get_status() can never auto-leak over the API."""
    id: str
    to_agent: str
    status: str
    created_by: Optional[str] = None
    created_at: Optional[str] = None
    artifact_ref: Optional[str] = None
    attempts: Optional[list] = None
    outbound: Optional[list] = None


def _ledger(request: Request) -> PostgresLedger:
    return request.app.state.ledger


def _principal(request: Request,
               creds: Optional[HTTPAuthorizationCredentials] = Depends(_security)) -> Principal:
    keys = request.app.state.api_keys
    if creds is None or creds.credentials not in keys:
        raise HTTPException(status_code=401, detail="invalid or missing API key")
    return keys[creds.credentials]


def create_app(ledger: Optional[PostgresLedger] = None, *,
               dsn: Optional[str] = None,
               api_keys: Optional[dict] = None) -> FastAPI:
    """Build the app. Pass a connected `ledger` (tests) OR leave it None to connect
    on startup from `dsn`/$LEDGER_DSN (serving). `api_keys` (token -> Principal) is
    injected for tests/demo, else loaded from $MYNDAIX_API_KEYS (empty -> fail-closed)."""
    no_docs = dict(docs_url=None, redoc_url=None, openapi_url=None)  # nothing unauth'd
    if ledger is not None:
        app = FastAPI(title="MyndAIX Team Runtime", version="0.1.0", **no_docs)
        app.state.ledger = ledger
    else:
        @asynccontextmanager
        async def lifespan(app: FastAPI):
            target = dsn or os.environ.get("LEDGER_DSN", "postgresql://localhost/runtime")
            app.state.ledger = await PostgresLedger.connect(target)
            try:
                yield
            finally:
                await app.state.ledger.close()
        app = FastAPI(title="MyndAIX Team Runtime", version="0.1.0", lifespan=lifespan, **no_docs)

    app.state.api_keys = (api_keys if api_keys is not None
                          else load_api_keys(os.environ.get("MYNDAIX_API_KEYS", "")))

    @app.get("/health")
    async def health(_p: Principal = Depends(_principal), led: PostgresLedger = Depends(_ledger)):
        return {"status": "ok", "queued": await led.count_queued()}

    @app.post("/inbound", response_model=JobOut, status_code=201)
    async def inbound(msg: InboundIn, p: Principal = Depends(_principal),
                      led: PostgresLedger = Depends(_ledger)):
        # transport semantics live in the envelope; they never reach the agent
        env = TransportEnvelope(
            transport=msg.transport, account=msg.account, sender_id=msg.sender_id,
            reply_target=msg.reply_target or f"{msg.transport}:{msg.sender_id}",
            dedupe_key=msg.dedupe_key)
        event_id = await led.ingest_inbound(env, msg.body)
        jid = await led.submit_job(to_agent=msg.to_agent, prompt=msg.body,
                                   inbound_event_id=event_id, created_by=_API_NS + p.id)
        return JobOut(job_id=str(jid))

    @app.post("/jobs", response_model=JobOut, status_code=201)
    async def submit(req: SubmitIn, p: Principal = Depends(_principal),
                     led: PostgresLedger = Depends(_ledger)):
        jid = await led.submit_job(to_agent=req.to_agent, prompt=req.prompt,
                                   created_by=_API_NS + p.id)
        return JobOut(job_id=str(jid))

    @app.get("/jobs/{job_id}", response_model=JobStatusOut)
    async def job_status(job_id: UUID, p: Principal = Depends(_principal),
                         led: PostgresLedger = Depends(_ledger)):
        st = await led.get_status(job_id)
        # 404 (not 403) for missing OR not-yours: never leak which job ids exist.
        # ownership is the NAMESPACED api:<id>, so it can't alias a provenance sentinel.
        if not st or (p.role != "admin" and st.get("created_by") != _API_NS + p.id):
            raise HTTPException(status_code=404, detail="job not found")
        return st

    return app


# uvicorn entrypoint: `uvicorn runtime.api:app` (connects + loads keys from env)
app = create_app()
