#!/usr/bin/env python3
"""Reject NON-ADDITIVE migration DDL — the precondition for safe auto-revert (design §2.8).

serve re-applies EVERY migrations/*.sql on every boot (no applied-migrations table — see
postgres_store.migrate), so a contraction in ANY shipped migration actually executes. The deploy is
auto-revertible: on a health failure reconcile resets CODE to the last-good SHA. That is only safe if
every migration is ADDITIVE (expand-contract) — the schema only ever GROWS, so old code always runs
against a compatible superset. A destructive change (drop/rename/retype/tighten a column or
constraint) breaks the old code against the new schema and can't be undone by a code-revert.

Matching runs on text whose comments, string/dollar-quoted bodies, and double-quoted identifiers are
neutralized in ONE interleaving-aware pass (a comment delimiter inside a string, or a quote inside a
quoted identifier, can no longer swallow real DDL — the sequential-regex bug cross-family review found),
then whitespace-collapsed and split per statement. So a keyword split across a newline (`DROP\\n TABLE`)
is caught, a string literal containing "DROP TABLE" is NOT a false positive, and a quoted identifier
cannot smuggle a keyword or hide a space/digit past a rule. It FAILS CLOSED on constructs it cannot
reason about: DO/EXECUTE/CALL and CREATE FUNCTION/PROCEDURE (dynamic/opaque bodies the linter can't
inspect) are rejected outright, and NOT VALID is NOT an additive escape (Postgres still enforces the
constraint on new writes). A blessed contraction is a deliberate, human-gated two-release change via
RECONCILE_ALLOW_CONTRACTION=1; a blessed additive ROUTINE (a trigger/util function the opaque-body rule
would otherwise block) has a NARROWER escape, RECONCILE_ALLOW_ROUTINE=1 (`--allow-routine`), which drops
ONLY the CREATE/DROP-routine rules and keeps every other contraction check active.

Regex is not a SQL parser: this is a conservative gate, not a proof. Its bias is fail-closed — an
ambiguous construct is rejected (a false positive costs a human sign-off), never silently passed.
Documented residuals (accepted): a migration that CALLs a PRE-EXISTING opaque function via a bare
`SELECT f()` (the function is not defined in this migration, so nothing to inspect), and `DROP NOT NULL`
treated as additive (a null written by new code during the pre-revert window could surface to old code).

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
# A GENERATED column (stored expr, or GENERATED ... AS IDENTITY) supplies its own value on old INSERTs,
# so `ADD COLUMN c ... NOT NULL GENERATED ...` is additive even without a DEFAULT keyword (r5 FP-6).
_GENERATED_RE = re.compile(r"\bGENERATED\b", re.IGNORECASE)
_ALTER_TABLE_RE = re.compile(r"\bALTER\s+TABLE\b", re.IGNORECASE)
# ADD [CONSTRAINT <name>] (CHECK|UNIQUE|PRIMARY KEY|FOREIGN KEY|EXCLUDE) — named OR unnamed (r2 #5d).
_ADD_TIGHTENING_RE = re.compile(
    r"\bADD\s+(?:CONSTRAINT\s+\S+\s+)?(?:CHECK|UNIQUE|PRIMARY\s+KEY|FOREIGN\s+KEY|EXCLUDE)\b", re.IGNORECASE)
# Code the linter cannot inspect is fail-closed. A statement that BEGINS with DO/EXECUTE/CALL runs a body
# `_normalize` strips (r3 #1). But that anchor alone was bypassable: a `CREATE FUNCTION f() AS $$ DROP
# TABLE x $$` (body stripped) invoked by a later `SELECT f()` starts with CREATE/SELECT, not DO/CALL
# (r4 CRIT-2). So ALSO reject CREATE FUNCTION/PROCEDURE outright — its body is opaque and can run DDL
# when invoked — and treat DROP FUNCTION/PROCEDURE as a contraction (old code may depend on it).
_DYNAMIC_DDL_RE = re.compile(r"^(?:DO|EXECUTE|CALL)\b", re.IGNORECASE)
_CREATE_ROUTINE_RE = re.compile(r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:FUNCTION|PROCEDURE)\b", re.IGNORECASE)


def _top_level_clauses(stmt: str) -> list[str]:
    """Split on TOP-LEVEL commas only (paren-depth 0) so a comma inside a type/args — numeric(10,2),
    CHECK(a,b) — does not break a clause (cross-family r2 #5c). Strings are already stripped upstream."""
    out: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in stmt:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def _add_column_not_null_no_default(stmt: str) -> bool:
    # ADD COLUMN ... NOT NULL without a DEFAULT is not additive. PER top-level clause so a DEFAULT on a
    # DIFFERENT added column can't spoof it (r2 #4), paren-aware so numeric(10,2) doesn't mis-split (r2 #5c).
    # A GENERATED column is exempt — Postgres computes its value for old INSERTs, so it's additive (r5 FP-6).
    if not _ADD_COL_RE.search(stmt):
        return False
    return any(_ADD_COL_RE.search(c) and _NOT_NULL_RE.search(c)
               and not _DEFAULT_RE.search(c) and not _GENERATED_RE.search(c)
               for c in _top_level_clauses(stmt))


def _drop_column(stmt: str) -> bool:
    # ALTER TABLE column drop (old code reads a column that's gone). Two cases:
    #   (a) the COLUMN keyword is present -> unambiguously a column drop, WHATEVER the column is named —
    #       incl. a column named `constraint`/`default`/`type`; the old spelling-based exclusions were a
    #       bypass (r2 #5b, r3 #8). `DROP COLUMN [IF EXISTS] <ident>`.
    #   (b) no COLUMN keyword: `DROP [IF EXISTS] <ident>` is still a column drop. Only DROP NOT NULL is
    #       excluded — it is the one genuinely-additive property drop (relaxes nullability). Everything
    #       else is caught: DROP CONSTRAINT / DROP DEFAULT are contractions ruled elsewhere too (a double
    #       flag is harmless), and a column literally NAMED constraint/default/expression/identity is a
    #       real drop that the old per-keyword exclusions silently let through (r3 #8, r4 HIGH-3). The
    #       rare genuinely-additive DROP EXPRESSION/IDENTITY now flags — a deliberate fail-closed tradeoff
    #       (use RECONCILE_ALLOW_CONTRACTION for a blessed generated-column cleanup).
    # Quoted identifiers are already neutralized to `qi` by _normalize, so a digit-leading or space-
    # containing quoted column name can't slip past the anchor (r3 #6/#7).
    if not _ALTER_TABLE_RE.search(stmt):
        return False
    if re.search(r"\bDROP\s+COLUMN\b", stmt, re.IGNORECASE):
        return True
    return bool(re.search(
        r"\bDROP\s+(?:IF\s+EXISTS\s+)?(?!NOT\s+NULL\b)[A-Za-z_]", stmt, re.IGNORECASE))


def _drop_default(stmt: str) -> bool:
    # ALTER TABLE ... ALTER COLUMN ... DROP DEFAULT looks additive but a NOT NULL column with no default
    # breaks old INSERTs that omitted it (relying on the default) after a revert (r3 #5). The linter can't
    # know the column's nullability, so fail closed — a blessed drop uses RECONCILE_ALLOW_CONTRACTION.
    return bool(_ALTER_TABLE_RE.search(stmt) and re.search(r"\bDROP\s+DEFAULT\b", stmt, re.IGNORECASE))


def _add_constraint_tightening(stmt: str) -> bool:
    # ALTER TABLE ... ADD [CONSTRAINT] CHECK/UNIQUE/PK/FK/EXCLUDE tightens a guarantee old code doesn't
    # honor after a revert. NOT VALID is NOT an additive escape (r3 #2): Postgres skips validating
    # EXISTING rows but still enforces the constraint on every new/updated row immediately, so reverted
    # old writes can fail. PER top-level clause (paren-aware) so a later clause can't exempt an earlier one.
    if not _ALTER_TABLE_RE.search(stmt):
        return False
    return any(_ADD_TIGHTENING_RE.search(c) for c in _top_level_clauses(stmt))


# DROP FUNCTION|PROCEDURE|ROUTINE (PG's ROUTINE drops either — r5 HIGH-4). Named so the narrow
# RECONCILE_ALLOW_ROUTINE escape can filter the routine rules without disabling the whole lint.
_DROP_ROUTINE_RE = re.compile(r"\bDROP\s+(FUNCTION|PROCEDURE|ROUTINE)\b", re.I)
_ROUTINE_CHECKERS = (_CREATE_ROUTINE_RE, _DROP_ROUTINE_RE)

# (checker, why). A checker is a compiled regex (matched with .search) or a callable(stmt)->bool.
_RULES = [
    (_DYNAMIC_DDL_RE,                                                "DO/EXECUTE/CALL — dynamic DDL the linter can't inspect (a contraction can hide in the body); fail-closed"),
    (_CREATE_ROUTINE_RE,                                             "CREATE FUNCTION/PROCEDURE — opaque body can run DDL when invoked (a stripped body defeats inspection); fail-closed"),
    (_DROP_ROUTINE_RE,                                               "DROP FUNCTION/PROCEDURE/ROUTINE — old code may depend on it"),
    (re.compile(r"\bDROP\s+TABLE\b", re.I),                          "DROP TABLE — old code reads a table that's gone"),
    (re.compile(r"\bDROP\s+(MATERIALIZED\s+VIEW|VIEW)\b", re.I),     "DROP VIEW — old code reads a relation that's gone"),
    (re.compile(r"\bDROP\s+(SEQUENCE|SCHEMA|TYPE)\b", re.I),         "DROP SEQUENCE/SCHEMA/TYPE — old code depends on it"),
    (re.compile(r"\bDROP\s+CONSTRAINT\b", re.I),                     "DROP CONSTRAINT — relaxes/changes a guarantee old code assumes"),
    (_drop_column,                                                   "DROP COLUMN (optional COLUMN kw) — old code reads a column that's gone"),
    (_drop_default,                                                  "DROP DEFAULT — a NOT NULL column with no default breaks old INSERTs that omit it after a revert"),
    (re.compile(r"\bSET\s+DEFAULT\s+\(?\s*NULL\b", re.I),            "SET DEFAULT NULL — removes a column's effective default (like DROP DEFAULT); breaks old INSERTs on a NOT NULL column after a revert"),
    (re.compile(r"\bRENAME\s+(?:COLUMN\s+|CONSTRAINT\s+)?\S+\s+TO\b", re.I), "RENAME COLUMN/CONSTRAINT — old code reads the old name"),
    (re.compile(r"\bRENAME\s+TO\b", re.I),                           "RENAME TABLE — old code reads the old name"),
    (re.compile(r"\bALTER\s+(?:COLUMN\s+)?\S+\s+(SET\s+DATA\s+)?TYPE\b", re.I), "column TYPE change (optional COLUMN kw) — old code mis-reads the type"),
    (re.compile(r"\bSET\s+NOT\s+NULL\b", re.I),                      "SET NOT NULL — breaks old writes that omit the column"),
    (_add_constraint_tightening,                                    "ADD CONSTRAINT (not NOT VALID) — tightens a guarantee old code doesn't honor"),
    (_add_column_not_null_no_default,                               "ADD COLUMN NOT NULL without DEFAULT — breaks old INSERTs / fails on a populated table"),
]


# SINGLE interleaving-aware token scan (cross-family r4 CRIT-1/-3). SEQUENTIAL re.subs mis-parse
# overlapping boundaries: a `/*` or `--` INSIDE a '...' string opened a comment match that swallowed
# real DDL between it and a later delimiter; a `'` inside a "..." identifier unbalanced the single-quote
# pass the same way. One left-to-right alternation consumes each lexical token WHOLE, so a delimiter that
# lives inside an already-open token is never re-interpreted. Order matters only for a tie at the SAME
# position; these openers can't tie (each starts with a distinct char), so any order is safe here.
_TOKEN_RE = re.compile(
    r"""
      (?P<block>/\*.*?\*/)                                  # /* block comment */  (non-greedy)
    | (?P<line>--[^\n]*)                                    # -- line comment (to EOL)
    | (?P<dollar>(?<![A-Za-z0-9_$])\$(?P<tag>(?:[A-Za-z_][A-Za-z0-9_]*)?)\$.*?\$(?P=tag)\$)  # $tag$ body $tag$
    | (?P<sq>'(?:[^']|'')*')                                # 'single-quoted' string ('' escapes)
    | (?P<dq>"(?:[^"]|"")*")                                # "double-quoted" identifier ("" escapes)
    """,
    re.DOTALL | re.VERBOSE,
)


def _neutralize_token(m: "re.Match[str]") -> str:
    # A "..." token is always a NAME, never a keyword -> a safe bareword `qi` (keeps identifier shape so
    # rules still see "a column here", but a quoted name can't smuggle a keyword or hide a space/digit).
    if m.group("dq") is not None:
        return " qi "
    # Comments vanish (whitespace); strings and dollar-quoted bodies collapse to an empty-string literal.
    if m.group("block") is not None or m.group("line") is not None:
        return " "
    return " '' "


def _normalize(sql: str) -> str:
    """Neutralize comments, string/dollar-quoted bodies, and double-quoted identifiers in ONE pass, then
    collapse whitespace — so a keyword split across a newline is caught, a delimiter hidden inside a
    string/quoted-ident is never mis-acted-upon (r4), and a quoted name can't smuggle a keyword."""
    return re.sub(r"\s+", " ", _TOKEN_RE.sub(_neutralize_token, sql))


def lint_file(path: Path, allow_routine: bool = False) -> list[str]:
    # allow_routine (operator-gated via RECONCILE_ALLOW_ROUTINE) drops ONLY the CREATE/DROP-routine rules
    # — a narrow escape for a genuinely-additive trigger/util function, without the blanket
    # RECONCILE_ALLOW_CONTRACTION that suppresses EVERY contraction check (r5 FP-7).
    rules = [(c, w) for (c, w) in _RULES if not (allow_routine and c in _ROUTINE_CHECKERS)]
    out: list[str] = []
    for stmt in _normalize(path.read_text()).split(";"):
        stmt = stmt.strip()
        if not stmt:
            continue
        for checker, why in rules:
            hit = checker.search(stmt) if isinstance(checker, re.Pattern) else checker(stmt)
            if hit:
                out.append(f"{path.name}: NON-ADDITIVE — {why}\n    {stmt[:140]}")
    return out


def main(argv: list[str]) -> int:
    args = argv[1:]
    allow_routine = "--allow-routine" in args
    args = [a for a in args if a != "--allow-routine"]
    if not args:
        sys.stderr.write("usage: migration_lint.py [--allow-routine] <file.sql|dir> ...\n")
        return 2
    files: list[Path] = []
    for a in args:
        p = Path(a)
        if p.is_dir():
            files.extend(sorted(p.glob("*.sql")))
        elif p.suffix == ".sql":
            files.append(p)
    violations: list[str] = []
    for sql in files:
        try:
            violations.extend(lint_file(sql, allow_routine=allow_routine))
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
