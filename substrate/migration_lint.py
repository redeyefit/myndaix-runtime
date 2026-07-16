#!/usr/bin/env python3
"""Reject NON-ADDITIVE migration DDL — the precondition for safe auto-revert (design §2.8).

serve re-applies EVERY migrations/*.sql on every boot (no applied-migrations table — see
postgres_store.migrate), so a contraction in ANY shipped migration actually executes. The deploy is
auto-revertible: on a health failure reconcile resets CODE to the last-good SHA. That is only safe if
every migration is ADDITIVE (expand-contract) — the schema only ever GROWS, so old code always runs
against a compatible superset. A destructive change (drop/rename/retype/tighten a column or
constraint) breaks the old code against the new schema and can't be undone by a code-revert.

Matching is done on COMMENT- and STRING-stripped, whitespace-collapsed text split per statement, so a
keyword split across a newline (`DROP\\n TABLE`) is caught and a string literal containing "DROP TABLE"
is NOT a false positive.

reconcile lints only the migrations ADDED-OR-MODIFIED in a deploy (the prev_good..HEAD delta,
--diff-filter=AMR), NOT the full history — historical migrations that already ran may contain
legitimate one-time contractions (0006 uses DROP-in-a-string, 0007/0010 drop old tables/views).

Usage:  migration_lint.py <file.sql|dir> ...
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_ADD_COL_RE = re.compile(r"\bADD\s+COLUMN\b", re.IGNORECASE)
_NOT_NULL_RE = re.compile(r"\bNOT\s+NULL\b", re.IGNORECASE)
_DEFAULT_RE = re.compile(r"\bDEFAULT\b", re.IGNORECASE)
_ALTER_TABLE_RE = re.compile(r"\bALTER\s+TABLE\b", re.IGNORECASE)
_NOT_VALID_RE = re.compile(r"\bNOT\s+VALID\b", re.IGNORECASE)


def _add_column_not_null_no_default(stmt: str) -> bool:
    # ADD COLUMN ... NOT NULL without a DEFAULT is not additive (breaks old INSERTs / fails on a
    # populated table). Check PER COMMA-CLAUSE: a DEFAULT on a DIFFERENT added column must not spoof
    # the check (cross-family review #4 — `ADD COLUMN a NOT NULL, ADD COLUMN b DEFAULT 0`).
    if not _ADD_COL_RE.search(stmt):
        return False
    return any(_ADD_COL_RE.search(seg) and _NOT_NULL_RE.search(seg) and not _DEFAULT_RE.search(seg)
               for seg in stmt.split(","))


def _drop_column(stmt: str) -> bool:
    # ALTER TABLE ... DROP [COLUMN] <ident> — the COLUMN keyword is OPTIONAL in Postgres (cross-family
    # review #3). Exclude the additive DROP DEFAULT / DROP NOT NULL and the separately-ruled
    # DROP CONSTRAINT / DROP TABLE|VIEW|... .
    return bool(_ALTER_TABLE_RE.search(stmt) and re.search(
        r"\bDROP\s+(?:COLUMN\s+)?(?!TABLE\b|VIEW\b|MATERIALIZED\b|SEQUENCE\b|SCHEMA\b|TYPE\b|"
        r"CONSTRAINT\b|DEFAULT\b|NOT\s+NULL\b|IF\b|EXPRESSION\b|IDENTITY\b)\"?[A-Za-z_]", stmt, re.IGNORECASE))


def _add_constraint_tightening(stmt: str) -> bool:
    # ALTER TABLE ... ADD CONSTRAINT (CHECK/UNIQUE/FK/PK) tightens a guarantee old code doesn't honor
    # after a revert. NOT VALID is the additive escape (doesn't validate existing rows). (review #3)
    return bool(_ALTER_TABLE_RE.search(stmt) and re.search(r"\bADD\s+CONSTRAINT\b", stmt, re.IGNORECASE)
                and not _NOT_VALID_RE.search(stmt))


# (checker, why). A checker is a compiled regex (matched with .search) or a callable(stmt)->bool.
_RULES = [
    (re.compile(r"\bDROP\s+TABLE\b", re.I),                          "DROP TABLE — old code reads a table that's gone"),
    (re.compile(r"\bDROP\s+(MATERIALIZED\s+VIEW|VIEW)\b", re.I),     "DROP VIEW — old code reads a relation that's gone"),
    (re.compile(r"\bDROP\s+(SEQUENCE|SCHEMA|TYPE)\b", re.I),         "DROP SEQUENCE/SCHEMA/TYPE — old code depends on it"),
    (re.compile(r"\bDROP\s+CONSTRAINT\b", re.I),                     "DROP CONSTRAINT — relaxes/changes a guarantee old code assumes"),
    (_drop_column,                                                   "DROP COLUMN (optional COLUMN kw) — old code reads a column that's gone"),
    (re.compile(r"\bRENAME\s+(?:COLUMN\s+|CONSTRAINT\s+)?\S+\s+TO\b", re.I), "RENAME COLUMN/CONSTRAINT — old code reads the old name"),
    (re.compile(r"\bRENAME\s+TO\b", re.I),                           "RENAME TABLE — old code reads the old name"),
    (re.compile(r"\bALTER\s+(?:COLUMN\s+)?\S+\s+(SET\s+DATA\s+)?TYPE\b", re.I), "column TYPE change (optional COLUMN kw) — old code mis-reads the type"),
    (re.compile(r"\bSET\s+NOT\s+NULL\b", re.I),                      "SET NOT NULL — breaks old writes that omit the column"),
    (_add_constraint_tightening,                                    "ADD CONSTRAINT (not NOT VALID) — tightens a guarantee old code doesn't honor"),
    (_add_column_not_null_no_default,                               "ADD COLUMN NOT NULL without DEFAULT — breaks old INSERTs / fails on a populated table"),
]


def _normalize(sql: str) -> str:
    """Strip comments + string/dollar-quoted bodies, collapse whitespace — so a keyword split across a
    newline is caught and a string literal is never a false positive."""
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)          # block comments
    sql = re.sub(r"--[^\n]*", " ", sql)                            # line comments
    sql = re.sub(r"\$([A-Za-z_]*)\$.*?\$\1\$", " '' ", sql, flags=re.DOTALL)  # dollar-quoted bodies
    sql = re.sub(r"'(?:[^']|'')*'", " '' ", sql)                   # single-quoted (with '' escapes)
    return re.sub(r"\s+", " ", sql)


def lint_file(path: Path) -> list[str]:
    out: list[str] = []
    for stmt in _normalize(path.read_text()).split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        for checker, why in _RULES:
            hit = checker.search(stmt) if isinstance(checker, re.Pattern) else checker(stmt)
            if hit:
                out.append(f"{path.name}: NON-ADDITIVE — {why}\n    {stmt[:140]}")
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
                         "two-release change — set RECONCILE_ALLOW_CONTRACTION=1 to override a blessed one.\n")
        return 1
    print(f"migration_lint: {len(files)} migration(s) additive-only — OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
