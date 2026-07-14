#!/usr/bin/env python3
# watch-scan.py — injection-pattern scanner for sanitize_untrusted (watch-lib.sh, §3.8).
# Reads untrusted body bytes on stdin; exit 0 = a pattern matched (caller DROPS), exit 1 = clean.
#
# Why Python, not grep: grep's locale handling is a trap — a UTF-8 locale silently SKIPS a line
# with an invalid byte (r2), while LC_ALL=C makes [[:space:]] ASCII-only so an NBSP-spaced payload
# evades every anchor (r3). Python decodes with errors='replace' (no line skip) + NFKC-normalizes
# (NBSP / compatibility forms -> ASCII) + \s is Unicode-aware.
#
# Conservative by design (security.md scanner rule): anchored instruction verbs, not bare
# keywords, to avoid dropping legitimate technical text. Fence markers (===BEGIN/END, VERDICT) are
# NOT scanned here — legitimate verdict drops carry them; the caller DEFANGS those instead.
import re
import sys
import unicodedata

PATTERNS = [
    r"(^|\s)(ignore|disregard|forget|override)\s+([a-z]+\s+)?"
    r"(previous|prior|above|earlier|preceding|your|these|those|my)\s+"
    r"(instructions|prompt|rules|context)",
    r"(^|\s)you\s+are\s+now\s+",
    r"new\s+(system\s+)?(instructions|directive|persona)\s*:",
    r"<\s*/\s*(system|assistant|user|task_content|user_input)\s*>",
    r"(disregard|bypass|skip|ignore)\s+(the\s+)?(fence|guard|approval|permission)",
]

text = unicodedata.normalize("NFKC", sys.stdin.buffer.read().decode("utf-8", "replace"))
sys.exit(0 if any(re.search(p, text, re.I) for p in PATTERNS) else 1)
