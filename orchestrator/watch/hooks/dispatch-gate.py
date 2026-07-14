#!/usr/bin/env python3
# dispatch-gate.py — the grammar logic for the Watch PreToolUse Bash hook (§3.2, HIGH-3).
# Reads the hook JSON on stdin, prints a permissionDecision JSON (or nothing = no decision).
# Invoked by dispatch-gate.sh (which sets PATH and execs this so stdin flows through).
#
# Verified vs current Claude Code docs (2.1.x, https://code.claude.com/docs/en/hooks): a
# PreToolUse Bash hook fires BEFORE the permission prompt, receives the FULL tool_input.command
# (every ; && | newline $() included), and can deny so the prompt never appears. So a compound
# line like  mxr x "ok"; curl evil  is denied WHOLE (closes oracle HIGH-3a).
import json
import re
import sys


def emit(decision, reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,       # "deny" | "ask" | "allow"
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return  # unparseable -> no decision, let the normal flow handle it

    if data.get("tool_name") != "Bash":
        return
    cmd = (data.get("tool_input") or {}).get("command", "")
    if not isinstance(cmd, str):
        return

    stripped = cmd.strip()

    # pre-approved read wrappers pass straight through (argument-shape checked).
    if re.fullmatch(r"(?:\S*/)?mxr-read(?:\.sh)? [0-9a-fA-F-]{8,36}", stripped) or \
       re.fullmatch(r"(?:\S*/)?read-inbox(?:\.sh)?(?: \S+)?", stripped):
        emit("allow", "pre-approved read wrapper")

    # is this an mxr DISPATCH attempt? (mxr as a word, not mxr-read/mxr-safe.)
    looks_like_mxr = re.search(r"(^|[^\w./-])mxr(\s|$)", cmd) is not None
    if not looks_like_mxr:
        return  # not a dispatch attempt; settings default-deny governs

    # byte cap on the SERIALIZED command (HIGH-3b: <180 codepoints can still overflow the
    # ~200-char approval preview after serialization).
    if len(cmd.encode("utf-8", "surrogatepass")) > 160:
        emit("deny", "dispatch too long: >160 bytes cannot be shown whole in the approval preview")

    AGENTS = "kilabz|lobster|oracle|mini|mack|codex"
    # printable-ASCII task, no shell metacharacters, no quotes, no leading dash. One command only.
    TASK = r"[A-Za-z0-9 ._,:/?!'@#%+=-]"
    grammar = re.compile(r'^mxr (' + AGENTS + r') "(?!-)(' + TASK + r'+)"$')

    m = grammar.fullmatch(stripped)
    if m is None:
        emit("deny",
             "dispatch rejected: must be exactly  mxr <flat-rate-agent> \"<printable-ascii task, "
             "no shell metacharacters>\"  — one command, <=160 bytes. Metered agents "
             "(recon/higgsfield), compound commands, flags, and --prompt-file are refused. Long "
             "or special tasks: dispatch from a Mack terminal.")

    emit("ask", "mxr dispatch to %s — approve on phone" % m.group(1))


if __name__ == "__main__":
    main()
