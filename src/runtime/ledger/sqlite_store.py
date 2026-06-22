"""SQLite-backed ledger for the runnable DEMO (zero external deps).

Production target is Postgres + asyncpg (DESIGN.md C2). The Command API is the
contract; persistence is swappable behind it - this SQLite store and the
Postgres one implement the same verbs. Demo store is sync + minimal: just the
verbs the end-to-end path exercises.
"""
from __future__ import annotations

import sqlite3
import uuid
from typing import Optional

from runtime.contracts import Result

_SCHEMA = """
CREATE TABLE IF NOT EXISTS job (
  id TEXT PRIMARY KEY, parent_id TEXT, to_agent TEXT NOT NULL, body TEXT NOT NULL,
  reply_target TEXT NOT NULL DEFAULT 'demo', repo_id TEXT, base_ref TEXT, artifact_ref TEXT,
  status TEXT NOT NULL DEFAULT 'queued', created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS attempt (
  id TEXT PRIMARY KEY, job_id TEXT NOT NULL, worker_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open', result TEXT, error_class TEXT
);
CREATE TABLE IF NOT EXISTS outbound (
  id TEXT PRIMARY KEY, job_id TEXT NOT NULL, reply_target TEXT NOT NULL,
  body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending'
);
"""


def _id() -> str:
    return str(uuid.uuid4())


class Ledger:
    """The demo ledger - the same Command-API verbs the spine uses, on SQLite."""

    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(_SCHEMA)

    # -- dispatch --
    def submit_job(self, to_agent: str, prompt: str, *, reply_target: str = "demo",
                   parent_id: Optional[str] = None, repo_id: Optional[str] = None,
                   base_ref: Optional[str] = None) -> str:
        jid = _id()
        self.db.execute(
            "INSERT INTO job(id,parent_id,to_agent,body,reply_target,repo_id,base_ref) "
            "VALUES(?,?,?,?,?,?,?)",
            (jid, parent_id, to_agent, prompt, reply_target, repo_id, base_ref))
        self.db.commit()
        return jid

    # -- worker lifecycle --
    def lease_job(self, worker_id: str) -> Optional[sqlite3.Row]:
        row = self.db.execute(
            "SELECT * FROM job WHERE status='queued' ORDER BY created_at, rowid LIMIT 1").fetchone()
        if row is None:
            return None
        aid = _id()
        self.db.execute("UPDATE job SET status='leased' WHERE id=?", (row["id"],))
        self.db.execute("INSERT INTO attempt(id,job_id,worker_id) VALUES(?,?,?)",
                        (aid, row["id"], worker_id))
        self.db.commit()
        return self.db.execute(
            "SELECT j.*, ? AS attempt_id FROM job j WHERE j.id=?", (aid, row["id"])).fetchone()

    def complete_attempt(self, job_id: str, attempt_id: str, result: Result) -> None:
        self.db.execute("UPDATE attempt SET status='ok', result=? WHERE id=?",
                        (result.model_dump_json(), attempt_id))
        self.db.execute("UPDATE job SET status='done', artifact_ref=? WHERE id=?",
                        (result.artifact_ref, job_id))
        rt = self.db.execute("SELECT reply_target FROM job WHERE id=?", (job_id,)).fetchone()
        self.db.execute("INSERT INTO outbound(id,job_id,reply_target,body) VALUES(?,?,?,?)",
                        (_id(), job_id, rt["reply_target"], result.text))
        self.db.commit()

    def fail_attempt(self, job_id: str, attempt_id: str, result: Result) -> None:
        ec = result.error_class.value if result.error_class else None
        self.db.execute("UPDATE attempt SET status='failed', result=?, error_class=? WHERE id=?",
                        (result.model_dump_json(), ec, attempt_id))
        # workspace-actors never auto-retry (C4); the demo marks the job failed.
        self.db.execute("UPDATE job SET status='failed' WHERE id=?", (job_id,))
        self.db.commit()

    # -- outbox / status --
    def pending_outbound(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM outbound WHERE status='pending'").fetchall()

    def mark_sent(self, outbound_id: str) -> None:
        self.db.execute("UPDATE outbound SET status='sent' WHERE id=?", (outbound_id,))
        self.db.commit()

    def status(self, job_id: str) -> Optional[sqlite3.Row]:
        return self.db.execute("SELECT * FROM job WHERE id=?", (job_id,)).fetchone()
