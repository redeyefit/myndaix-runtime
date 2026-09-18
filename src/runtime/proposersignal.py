"""proposersignal — the observe-only "a skill class is READY" email notifier.

Reads the capture_candidate rows the S7 proposer *would* act on (state='ready') and emails
Jefe ONCE per newly-ready class — WITHOUT opening any PR, running the proposer, or touching
the review path. The proposer stays OFF; this is the push-signal that says "there is now
something worth proposing," so the decision to promote a class to a real skill PR stays human.

    MYNDAIX_DSN=... PYTHONPATH=src python3 -m runtime.proposersignal tick

Wiring: orchestrator/proposer-signal.sh (launchd entry) sources the send credential and sets
the env, then execs this. See docs/proposer-ready-signal-design.md.

Safety properties (each load-bearing):
  - READ-ONLY on the ledger: list_ready_candidates is a SELECT; no state mutation, no CAS,
    no worktree, no gh. The proposer's OFF-ness is preserved (this never claims a candidate).
  - Dedup by FINGERPRINT via an atomic-rewrite seen-file: exactly one email per class until
    it leaves 'ready'. A fingerprint that leaves then re-enters 'ready' re-notifies — correct:
    the class recurred again after being cleared, which is worth knowing.
  - The seen-file is advanced to include a new class ONLY after a SUCCESSFUL send; a failed
    send leaves it out so the next tick retries rather than silently dropping the signal.
  - Sender is INJECTABLE via $NOTIFY_SENDER so the test never sends real mail; the default is
    Gmail SMTP over STARTTLS reading SMTP_USER/SMTP_PASS/NOTIFY_TO from the sourced credential.
  - Fail-OPEN on read (a ledger error logs loudly and exits 0 — a notifier must never wedge
    launchd or mask the pipeline); fail-CLOSED on delivery (no credential -> no send).

The asyncpg-dependent ledger read is imported INSIDE fetch_ready so importing this module (and
its pure dedup logic) needs no DB driver — the unit test runs anywhere.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

DSN = os.environ.get("MYNDAIX_DSN") or "postgresql://127.0.0.1/runtime"
SEEN_PATH = Path(
    os.environ.get("PROPOSER_SIGNAL_SEEN")
    or (Path.home() / ".myndaix" / "orchestrator" / "proposer-signal.seen")
)


def _log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [proposer-signal] {msg}", file=sys.stderr, flush=True)


def load_seen(path: Path) -> set:
    """The set of fingerprints already emailed. Missing file = empty set (first run)."""
    try:
        return {ln.strip() for ln in path.read_text().splitlines() if ln.strip()}
    except FileNotFoundError:
        return set()


def commit_seen(path: Path, fingerprints: set) -> None:
    """Atomic rewrite (temp + os.replace — rename is atomic on APFS) of the WHOLE set, so a
    fingerprint that left 'ready' is naturally forgotten and re-notifies if it ever returns."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".seen-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(sorted(fingerprints)))
            if fingerprints:
                f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def compose(rows: list) -> tuple:
    """(subject, body) for the newly-ready classes. rows are capture_candidate dicts."""
    tags = sorted({r["rule_tag"] for r in rows})
    n = len(rows)
    subject = f"[myndaix] {n} skill class{'' if n == 1 else 'es'} ready: {', '.join(tags)}"[:160]
    lines = [
        "The auto-capture pipeline has newly-READY skill class(es) — a recurring, cross-family-",
        "agreed finding that crossed the recurrence bar. The proposer is OFF: nothing was opened.",
        "Decide whether to promote any of these to a skill PR.",
        "",
    ]
    for r in sorted(rows, key=lambda r: (r["repo_scope"], r["rule_tag"])):
        glob = r.get("path_glob") or "any path"
        lines.append(f"  • {r['rule_tag']}   in {r['repo_scope']}   ({glob})")
    lines += [
        "",
        "To inspect what the proposer would open (no PR, no token):",
        "  MYNDAIX_PROPOSER_DRY_RUN=1 PYTHONPATH=src .venv/bin/python -m runtime.proposer tick",
        "To act, arm the proposer per docs/auto-capture-design.md.",
    ]
    return subject, "\n".join(lines)


def send(subject: str, body: str) -> bool:
    """Deliver the notification. $NOTIFY_SENDER (a command) overrides the default sender: it
    receives the subject as argv[1] and the body on stdin, and its exit code is the result —
    this is the seam the unit test stubs so it never touches SMTP. True iff delivered."""
    override = os.environ.get("NOTIFY_SENDER")
    if override:
        r = subprocess.run([override, subject], input=body, text=True)
        return r.returncode == 0
    return _smtp_send(subject, body)


def _smtp_send(subject: str, body: str) -> bool:
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_PASS")
    to = os.environ.get("NOTIFY_TO") or user
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "587"))
    if not (user and pw and to):
        _log("SMTP_USER/SMTP_PASS/NOTIFY_TO not all set — cannot send (is gmail-notify.env sourced?)")
        return False
    import smtplib
    from email.message import EmailMessage

    m = EmailMessage()
    m["From"] = user
    m["To"] = to
    m["Subject"] = subject
    m.set_content(body)
    try:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(user, pw)
            s.send_message(m)
        return True
    except Exception as e:                          # noqa: BLE001 — notifier fails soft, logs loud
        _log(f"SMTP send failed: {e}")
        return False


async def fetch_ready(dsn: str) -> list:
    """READ-ONLY: every state='ready' capture_candidate, paged via the keyset cursor. Import is
    local so the module (and its pure logic) loads without asyncpg."""
    from runtime.ledger.postgres_store import PostgresLedger

    led = await PostgresLedger.connect(dsn)
    try:
        out: list = []
        after = ""
        while True:
            batch = await led.list_ready_candidates(200, after=after)
            if not batch:
                break
            out.extend(batch)
            after = batch[-1]["fingerprint"]
            if len(batch) < 200:
                break
        return out
    finally:
        await led.close()


def next_seen(seen: set, current: set, new: set, emailed_ok: bool) -> set:
    """The seen-set to persist: keep still-ready previously-seen (suppress them), and add the
    newly-emailed ONLY if the send succeeded (a failed send stays un-seen so it retries).
    A fingerprint that left 'ready' (in seen, not in current) is dropped — pure, unit-tested."""
    return (seen & current) | (new if emailed_ok else set())


def main(argv: list) -> int:
    if len(argv) < 2 or argv[1] != "tick":
        print("usage: python -m runtime.proposersignal tick", file=sys.stderr)
        return 2
    _log("tick fire")
    import asyncio

    try:
        rows = asyncio.run(fetch_ready(DSN))
    except Exception as e:                          # noqa: BLE001 — fail-OPEN on read
        _log(f"ledger read failed: {e} — fail-open, exit 0")
        return 0

    by_fp = {r["fingerprint"]: r for r in rows}
    current = set(by_fp)
    seen = load_seen(SEEN_PATH)
    new = current - seen
    _log(f"ready={len(current)} seen={len(seen)} new={len(new)}")

    emailed_ok = True
    if new:
        subject, body = compose([by_fp[fp] for fp in sorted(new)])
        emailed_ok = send(subject, body)
        _log(f"send {'ok' if emailed_ok else 'FAILED'} for {len(new)} new class(es)")

    try:
        commit_seen(SEEN_PATH, next_seen(seen, current, new, emailed_ok))
    except Exception as e:                          # noqa: BLE001 — never wedge on a state write
        _log(f"seen-file write failed: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
