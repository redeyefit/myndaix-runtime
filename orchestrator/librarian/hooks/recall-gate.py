#!/usr/bin/env python3
# recall-gate.py — PreToolUse Bash gate for the READ-ONLY recall LIBRARIAN session (piece C).
# The librarian is read-only: the ONLY Bash it may run is `mxr ask --scope research|fitness "<question>"`.
# NO dispatch, NO other program, NO other scope, NO shell operators/expansion. settings.json removes
# every non-Bash tool by canonical name, so this hook is the SOLE allow-er of Bash and MUST be airtight.
#
# FAIL-CLOSED (cross-family review r1, HIGH — confirmed vs Claude Code 2.1.x docs): under defaultMode
# `dontAsk`, a PreToolUse hook that emits NO decision FALLS THROUGH TO ALLOW for Bash (Bash cannot be in
# the deny list, or the hook could never allow a valid recall). So EVERY non-allow path emits an explicit
# "deny" — NEVER a bare return. The one correct carve-out is tool_name != "Bash" (a Bash-matcher hook must
# not opine on tools it doesn't gate; the deny-list covers those). A non-string command (e.g. an injected
# array `{"command":["rm","-rf","/"]}`) and unparseable/malformed payloads all deny.
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


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        emit("deny", "unparseable hook payload")              # FAIL-CLOSED (was a bare return = fail-open)
    if not isinstance(data, dict):
        emit("deny", "malformed hook payload (not an object)")
    if data.get("tool_name") != "Bash":
        return                                                # correct carve-out: don't gate other tools
    cmd = (data.get("tool_input") or {}).get("command", "")
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


main()
