#!/usr/bin/env python3
# dispatch-gate.py — the grammar logic for the Watch PreToolUse Bash hook (§3.2, HIGH-3).
# Reads the hook JSON on stdin, prints a permissionDecision JSON (or nothing = no decision).
# Invoked by dispatch-gate.sh (which sets PATH and execs this so stdin flows through).
#
# Verified vs current Claude Code docs (2.1.x, hooks/permissions): a PreToolUse Bash hook fires
# BEFORE the permission prompt, receives the FULL tool_input.command (every ; && | newline $()
# included), and can deny so the prompt never appears. This hook is the SOLE allow-er of the two
# read wrappers (settings.json has an EMPTY allow list + defaultMode dontAsk), so it must be
# airtight: any command whose program is a wrapper OR mxr but is not a STRICT safe invocation is
# DENIED explicitly — never left to fall through (a fall-through under dontAsk is a deny for Bash,
# but we deny loudly so the reason is legible and so we never depend on that ordering).
#
# CRITICAL-1 fix (code review r1): the old read-wrapper allow used `\S+`, so
# `read-inbox ;mxr${IFS}recon${IFS}go` (no whitespace after ';') fullmatched and got `allow`,
# then bash expanded ${IFS} and ran a metered dispatch. Now: metachars/operators on a wrapper
# command -> deny; only an exact safe-char invocation -> allow.
import json
import re
import sys

READ_WRAPPERS = {"read-inbox", "read-inbox.sh", "mxr-read", "mxr-read.sh"}
# operators + expansion that make a "single command" actually compound/expanding. Dangerous even
# inside our wrapper surface; any presence -> deny.
SHELL_META = set(";&|<>$`(){}[]\\\n\r*?~!")
FLAT_RATE_AGENTS = "kilabz|lobster|oracle|mini|mack|codex"     # never recon/higgsfield (metered)
# printable-ASCII task body: no double-quote, no shell-expansion chars ($ ` \), no metachars.
TASK = r"[A-Za-z0-9 ._,:/?!'@#%+=-]"
DISPATCH = re.compile(r'^mxr (' + FLAT_RATE_AGENTS + r') "(?!-)(' + TASK + r'+)"$')


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
        return  # unparseable -> no decision
    if data.get("tool_name") != "Bash":
        return
    cmd = (data.get("tool_input") or {}).get("command", "")
    if not isinstance(cmd, str) or not cmd.strip():
        return
    stripped = cmd.strip()

    # program name = first whitespace-delimited token; basename for wrapper/mxr identification.
    prog = re.match(r"\s*(\S+)", cmd).group(1)
    base = prog.rsplit("/", 1)[-1]

    # ---- read wrappers: STRICT or DENY (never allow-on-loose-match) ----
    if base in READ_WRAPPERS:
        if any(c in cmd for c in SHELL_META):
            emit("deny", "read wrapper with shell metacharacters/operators refused")
        if base.startswith("mxr-read"):
            if re.fullmatch(r"(?:\S*/)?mxr-read(?:\.sh)? [0-9a-fA-F-]{8,36}", stripped):
                emit("allow", "pre-approved ledger read")
            emit("deny", "malformed mxr-read (want: mxr-read <8-36 hex/hyphen id>)")
        else:  # read-inbox
            if re.fullmatch(r"(?:\S*/)?read-inbox(?:\.sh)?(?: [A-Za-z0-9./_-]+)?", stripped):
                emit("allow", "pre-approved inbox read")
            emit("deny", "malformed read-inbox (want: read-inbox [safe-path])")

    # ---- mxr dispatch: STRICT grammar or DENY ----
    if base == "mxr":
        if len(cmd.encode("utf-8", "surrogatepass")) > 160:
            emit("deny", "dispatch >160 bytes cannot be shown whole in the approval preview")
        m = DISPATCH.fullmatch(stripped)
        if m is None:
            emit("deny",
                 "dispatch rejected: must be exactly  mxr <flat-rate-agent> \"<printable-ascii "
                 "task, no shell metacharacters>\"  — one command, <=160 bytes. Metered agents "
                 "(recon/higgsfield), compound commands, flags, and --prompt-file are refused. "
                 "Long or special tasks: dispatch from a Mack terminal.")
        emit("ask", "mxr dispatch to %s — approve on phone" % m.group(1))

    # not a Watch surface -> no decision (settings dontAsk default-deny governs)
    return


if __name__ == "__main__":
    main()
