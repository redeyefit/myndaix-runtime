#!/usr/bin/env python3
# recall-gate.py — PreToolUse Bash gate for the READ-ONLY recall LIBRARIAN session (piece C).
# Adapted from the reviewed Watch dispatch-gate (design/watch-v0.3, §3.2 HIGH-3). The librarian is
# read-only: the ONLY Bash it may ever run is `mxr ask` / `mxr recall` (answer/search the corpus).
# NO dispatch (`mxr <agent> "task"`), NO other program, NO shell operators/expansion. The kit's
# settings.json removes Read/Grep/web/MCP/Agent/etc. by bare name, so this hook is the SOLE allow-er
# of Bash — it must be airtight: anything not an EXACT safe recall invocation is DENIED loudly.
#
# The guard is the fullmatch + the safe QUERY charset (no "  $ ` \  -> no in-double-quote expansion
# and no way to close the string early), NOT a blanket metachar scan — so a natural "?" in a question
# is fine, while `mxr ask --scope x "q"; rm -rf /` fails the fullmatch (trailing chars) and is denied.
import json
import re
import sys

# scope = the runtime's charset (knowledge.known_scopes: [a-z0-9][a-z0-9._-]*); the runtime ALSO
# fail-closes on an unregistered scope (exit 2), so this is belt.
SCOPE = r"[a-z0-9][a-z0-9._-]*"
# query body: printable ASCII MINUS the double-quote and the three in-quote-expansion chars ($ ` \).
# Question marks / apostrophes / slashes are safe inside the double-quoted arg.
QUERY = r"[A-Za-z0-9 ._,:/?!'@#%+=()-]"
RECALL = re.compile(
    r'^mxr (ask|recall) --scope (' + SCOPE + r') "(?!-)(' + QUERY + r'+)"'
    r'(?: -k [0-9]{1,2})?(?: --fenced)?$'
)


def emit(decision, reason):
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
        return                                   # unparseable -> no decision
    if data.get("tool_name") != "Bash":
        return
    cmd = (data.get("tool_input") or {}).get("command", "")
    if not isinstance(cmd, str) or not cmd.strip():
        return
    stripped = cmd.strip()

    m = re.match(r"\s*(\S+)", cmd)
    if not m:
        return
    base = m.group(1).rsplit("/", 1)[-1]

    if base == "mxr":
        if len(cmd.encode("utf-8", "surrogatepass")) > 500:
            emit("deny", "recall command too long (>500 bytes)")
        if RECALL.fullmatch(stripped):
            emit("allow", "read-only recall (mxr ask/recall)")
        emit("deny",
             'recall rejected: must be EXACTLY  mxr ask|recall --scope <scope> "<printable question, '
             'no $ ` \\ or double-quote>"  — no dispatch (mxr <agent>), one command, safe chars only')

    # any other program (cat/grep/ls/curl/python/…) -> deny loudly. read-only librarian.
    emit("deny", f"'{base}' is not permitted — the librarian may run ONLY `mxr ask` / `mxr recall`")


main()
