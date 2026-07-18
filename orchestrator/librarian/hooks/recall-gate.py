#!/usr/bin/env python3
# recall-gate.py — PreToolUse ALLOWLIST gate for the READ-ONLY recall LIBRARIAN session (piece C).
# The librarian is read-only: the ONLY thing it may run is `mxr ask --scope research|fitness "<question>"`
# (Bash). NO dispatch, NO other program, NO other scope, NO shell operators/expansion, NO other TOOL.
#
# ALLOWLIST MODEL (keepalive review r2, HIGH-1/HIGH-2). settings.json is registered with matcher "*", so
# this hook fires for EVERY tool call and is the SOLE decider: it ALLOWS only a valid `mxr ask` (Bash) and
# emits an explicit "deny" for everything else — every other Bash command AND every non-Bash tool. This is
# fail-closed by CONSTRUCTION and does NOT depend on the settings deny-list staying exhaustive as Claude
# Code adds new tools (the enumeration that let a live write-capable tool, DesignSync, slip the deny-list).
# The deny-list remains as belt (deny-first precedence), but the gate no longer relies on it.
#
# FAIL-CLOSED (also cross-family review r1, HIGH — vs Claude Code 2.1.x docs): under defaultMode `dontAsk`,
# a PreToolUse hook that emits NO decision FALLS THROUGH TO ALLOW for (read-only-looking) Bash — so Bash
# cannot be in the deny list and EVERY non-allow path here MUST emit an explicit "deny", never a bare
# return. A non-string command (e.g. an injected array `{"command":["rm","-rf","/"]}`) and unparseable /
# malformed payloads all deny. There is now NO silent path: non-Bash tools deny explicitly too.
#
# The injection guard is the regex fullmatch + the QUERY charset (excludes "  $ ` \  -> nothing closes the
# double-quoted arg early or expands in-quote, and no trailing `; rm -rf` survives a fullmatch). A natural
# "?" in a question is fine (literal inside the quotes).
import json
import re
import sys
from typing import NoReturn

# EXPLICIT scope allowlist (review r1 MEDIUM): ONLY these are phone-queryable, regardless of what
# MYNDAIX_KNOWLEDGE_SCOPES holds — a future sensitive scope (e.g. personal) must NOT auto-become
# reachable from the phone. Add a scope here (and register it in the runtime) to widen, deliberately.
SCOPES = "research|fitness"
QUERY = r"[A-Za-z0-9 ._,:/?!'@#%+=()-]"
# ONLY `mxr ask` (review r1 MEDIUM: raw `mxr recall` snippets are UNFENCED and could carry injection into
# the outer session; `ask` returns a synthesized answer, and the RC gate bounds any injection in it anyway).
RECALL = re.compile(
    r'^mxr ask --scope (' + SCOPES + r') "(?!-)(' + QUERY + r'+)"(?: -k [0-9]{1,2})?$'
)


def emit(decision: str, reason: str) -> NoReturn:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,          # "deny" | "ask" | "allow"
        "permissionDecisionReason": reason,
    }}))
    sys.exit(0)


def _decide(data) -> None:
    if not isinstance(data, dict):
        emit("deny", "malformed hook payload (not an object)")
    tool = data.get("tool_name")
    if tool != "Bash":
        # ALLOWLIST (keepalive review r2 HIGH): the gate fires for EVERY tool (matcher "*") and denies
        # every non-Bash tool structurally — no dependency on the settings deny-list being exhaustive.
        emit("deny", f"only `mxr ask` (Bash) is permitted; tool '{tool!r}' denied")
    ti = data.get("tool_input")
    if not isinstance(ti, dict):
        emit("deny", "malformed tool_input (not an object)")  # review r2 HIGH: a truthy non-dict crashed here
    for k in ("env", "cwd", "environment", "working_directory"):
        if k in ti:
            emit("deny", f"forbidden tool_input key '{k}' (no env/cwd override)")   # review r2 MED
    cmd = ti.get("command", "")
    if not isinstance(cmd, str) or not cmd.strip():
        emit("deny", "missing / non-string / empty Bash command")   # blocks array-command injection etc.
    stripped = cmd.strip()

    m = re.match(r"\s*(\S+)", cmd)
    if not m:
        emit("deny", "no program token")
    base = m.group(1).rsplit("/", 1)[-1]

    if base == "mxr":
        if len(cmd.encode("utf-8", "surrogatepass")) > 500:
            emit("deny", "recall command too long (>500 bytes)")
        if RECALL.fullmatch(stripped):
            emit("allow", "read-only recall (mxr ask, allowlisted scope)")
        emit("deny",
             'recall rejected: must be EXACTLY  mxr ask --scope research|fitness "<printable question, '
             'no $ ` \\ or double-quote>"  — no dispatch, no other scope, one command')

    emit("deny", f"'{base}' is not permitted — the librarian runs ONLY `mxr ask --scope research|fitness`")


def main() -> None:
    # CRASH-PROOF FAIL-CLOSED (review r2 HIGH): ANY unexpected error — unparseable JSON, a truthy
    # non-dict tool_input, anything — must DENY, never emit nothing (a no-decision hook falls through to
    # ALLOW for Bash under dontAsk). emit() raises SystemExit, which we re-raise so the printed decision
    # stands; every other exception denies. There is NO silent path now: every tool_name emits a decision.
    try:
        _decide(json.load(sys.stdin))
    except SystemExit:
        raise
    except Exception:
        emit("deny", "gate internal error — fail-closed")


main()
