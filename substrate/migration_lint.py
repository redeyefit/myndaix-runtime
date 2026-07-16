#!/usr/bin/env python3
"""Reject NON-ADDITIVE migration DDL — the precondition for safe auto-revert (design §2.8).

serve applies migrations on boot and the deploy is auto-revertible: on a post-restart health
failure reconcile resets the CODE to the last-good SHA. That is only safe if every migration is
ADDITIVE (expand-contract / ParallelChange) — the schema only ever GROWS, so the old code always
runs against a compatible superset. A destructive change (drop/rename/retype a column, tighten a
constraint) would make the old code break against the new schema. A genuine contraction is a
deliberate, human-gated two-release dance — never an auto-deploy.

Scans the given .sql FILES (comments stripped) and FAILS (exit 1) on any non-additive statement.
Additive DDL — CREATE ... IF NOT EXISTS, ADD COLUMN (incl. NOT NULL *with* DEFAULT), CREATE INDEX,
DROP INDEX, inserts/updates — is allowed.

IMPORTANT: lint only the migrations ADDED in a given deploy (the prev_good..HEAD delta), NOT the full
history — historical migrations that already ran may contain legitimate one-time contractions
(0007/0010 drop old tables/views). reconcile passes only the delta files.

Usage:  migration_lint.py <file.sql|dir> ...     (a dir expands to its *.sql)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# (pattern, why) — matched case-insensitively against comment-stripped SQL.
_RULES = [
    (r"\bDROP\s+TABLE\b",                       "DROP TABLE — old code reads a table that's gone"),
    (r"\bDROP\s+(MATERIALIZED\s+VIEW|VIEW)\b",  "DROP VIEW — old code reads a relation that's gone"),
    (r"\bDROP\s+(SEQUENCE|SCHEMA|TYPE)\b",      "DROP SEQUENCE/SCHEMA/TYPE — old code depends on it"),
    (r"\bDROP\s+COLUMN\b",                      "DROP COLUMN — old code reads a column that's gone"),
    (r"\bRENAME\s+(COLUMN|TO)\b",               "RENAME — old code reads the old name"),
    (r"\bALTER\s+COLUMN\s+\S+\s+(SET\s+DATA\s+)?TYPE\b", "column TYPE change — old code mis-reads the type"),
    (r"\bSET\s+NOT\s+NULL\b",                   "SET NOT NULL — breaks old writes that omit the column"),
]
_COMPILED = [(re.compile(p, re.IGNORECASE), why) for p, why in _RULES]


def _strip_sql_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)   # block comments
    sql = re.sub(r"--[^\n]*", " ", sql)                     # line comments
    return sql


def lint_file(path: Path) -> list[str]:
    text = _strip_sql_comments(path.read_text())
    out: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for rx, why in _COMPILED:
            if rx.search(line):
                out.append(f"{path.name}:{lineno}: NON-ADDITIVE — {why}\n    {line.strip()[:120]}")
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        sys.stderr.write("usage: migration_lint.py <file.sql|dir> ...\n")
        return 2
    files: list[Path] = []
    for a in argv[1:]:
        p = Path(a)
        if p.is_dir():
            files.extend(sorted(p.glob("*.sql")))
        elif p.suffix == ".sql":
            files.append(p)
        # silently ignore non-.sql paths (a delta may include non-migration files)
    violations: list[str] = []
    for sql in files:
        try:
            violations.extend(lint_file(sql))
        except OSError as e:
            sys.stderr.write(f"migration_lint: cannot read {sql}: {e}\n")
            return 2
    if violations:
        sys.stderr.write("migration_lint: NON-ADDITIVE migration DDL rejected (design §2.8):\n")
        for v in violations:
            sys.stderr.write("  " + v + "\n")
        sys.stderr.write("A contraction (drop/rename/retype/tighten) must be a deliberate, human-gated\n"
                         "two-release change — it must not auto-deploy to the always-on FACTORY.\n")
        return 1
    print(f"migration_lint: {len(files)} migration(s) additive-only — OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
