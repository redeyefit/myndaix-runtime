"""HTTP Command-API (DESIGN.md S4b) - the runtime as a service.

FastAPI over the ledger's external verbs, so a transport (or any client) can
submit work and read status over the network while workers run as SEPARATE
processes against the same Postgres ledger. The Command API stays the SOLE ledger
writer - these endpoints just expose its verbs over HTTP and hold no state of
their own. The ledger is the only shared state, so you can run N API instances +
M worker instances behind one Postgres.

Serve:  LEDGER_DSN=postgresql://localhost/runtime \\
            uvicorn runtime.api:app --port 8080
Test:   inject a connected ledger via create_app(ledger) and drive it with httpx
        ASGITransport (no socket needed).
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Optional
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from runtime.contracts import TransportEnvelope
from runtime.ledger.postgres_store import PostgresLedger

_MAX_BODY = 100_000  # bound the payload so a huge body can't DoS the queue/storage


def _no_nul(v: str) -> str:
    # Postgres text can't hold NUL; reject it up front so it's a clean 422, not a 500
    if "\x00" in v:
        raise ValueError("NUL bytes are not allowed")
    return v


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


def _ledger(request: Request) -> PostgresLedger:
    return request.app.state.ledger


def create_app(ledger: Optional[PostgresLedger] = None, *, dsn: Optional[str] = None) -> FastAPI:
    """Build the app. Pass a connected `ledger` (tests) OR leave it None to connect
    on startup from `dsn`/$LEDGER_DSN (serving via uvicorn)."""
    if ledger is not None:
        app = FastAPI(title="MyndAIX Team Runtime", version="0.1.0")
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
        app = FastAPI(title="MyndAIX Team Runtime", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    async def health(led: PostgresLedger = Depends(_ledger)):
        return {"status": "ok", "queued": await led.count_queued()}

    @app.post("/inbound", response_model=JobOut, status_code=201)
    async def inbound(msg: InboundIn, led: PostgresLedger = Depends(_ledger)):
        # transport semantics live in the envelope; they never reach the agent
        env = TransportEnvelope(
            transport=msg.transport, account=msg.account, sender_id=msg.sender_id,
            reply_target=msg.reply_target or f"{msg.transport}:{msg.sender_id}",
            dedupe_key=msg.dedupe_key)
        event_id = await led.ingest_inbound(env, msg.body)
        jid = await led.submit_job(to_agent=msg.to_agent, prompt=msg.body,
                                   inbound_event_id=event_id)
        return JobOut(job_id=str(jid))

    @app.post("/jobs", response_model=JobOut, status_code=201)
    async def submit(req: SubmitIn, led: PostgresLedger = Depends(_ledger)):
        jid = await led.submit_job(to_agent=req.to_agent, prompt=req.prompt)
        return JobOut(job_id=str(jid))

    @app.get("/jobs/{job_id}")
    async def job_status(job_id: UUID, led: PostgresLedger = Depends(_ledger)):
        st = await led.get_status(job_id)
        if not st:
            raise HTTPException(status_code=404, detail="job not found")
        return st

    return app


# uvicorn entrypoint: `uvicorn runtime.api:app` (connects on startup from $LEDGER_DSN)
app = create_app()
