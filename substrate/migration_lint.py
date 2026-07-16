#!/usr/bin/env python3
"""Reject NON-ADDITIVE migration DDL — the precondition for safe auto-revert (design §2.8).

serve re-applies EVERY migrations/*.sql on every boot (no applied-migrations table — see
postgres_store.migrate), so a contraction in ANY shipped migration actually executes. The deploy is
auto-revertible: on a health failure reconcile resets CODE to the last-good SHA. That is only safe if
every migration is ADDITIVE (expand-contract) — the schema only ever GROWS, so old code always runs
against a compatible superset. A destructive change (drop/rename/retype/tighten a column or
constraint) breaks the old code against the new schema and can't be undone by a code-revert.

TWO layers:

1. A hand-written character SCANNER (`_normalize`) neutralizes comments, string/dollar-quoted bodies, and
   double-quoted identifiers by modeling Postgres's lexer exactly (NOT a regex — a regex can't count
   nested block-comment depth, and kept diverging on E'' escapes, Unicode identifiers, and CR line-endings
   across review rounds). So a keyword split across a newline is caught, a delimiter hidden inside a
   string/comment/quoted-ident is never mis-acted-upon, and a quoted name can't smuggle a keyword.

2. An ALLOWLIST (`_is_additive`) is the gate: a statement passes ONLY if it matches a provably-additive
   shape (CREATE a brand-new object; ALTER TABLE whose every clause is ADD COLUMN / ATTACH PARTITION /
   INHERIT; ALTER TYPE ADD VALUE|ATTRIBUTE; COMMENT; INSERT). EVERYTHING ELSE is rejected fail-closed. The
   earlier DENYLIST ("reject known-bad DDL") was fail-OPEN by default and could not converge — Postgres's
   non-additive surface is large and grows every release, so each review round found one more un-rejected
   contraction. For a gate protecting an autonomous factory a false-NEGATIVE (a hidden contraction) is far
   worse than a false-POSITIVE (a human sign-off), so the default is "not proven additive => reject".

A blessed contraction is a deliberate, human-gated two-release change via RECONCILE_ALLOW_CONTRACTION=1; a
blessed routine/behavioral object (a trigger/util function) has a NARROWER escape RECONCILE_ALLOW_ROUTINE=1
(`--allow-routine`) that only blesses CREATE/DROP of a function/procedure/rule/trigger — a DROP TABLE
alongside is a separate statement and still fails.

This is a conservative gate, not a proof. Its bias is fail-closed — an unrecognized construct is rejected
(a false positive costs a human sign-off), never silently passed. Documented residual (accepted): a plain
INSERT is treated additive; UPDATE/DELETE/TRUNCATE are rejected (they can strand old code with mutated or
destroyed data).

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
    # `\S` (any non-whitespace token start), NOT `[A-Za-z_]`: an UNQUOTED Unicode column name (`DROP é`)
    # is a valid destructive drop that an ASCII-only anchor missed (cross-family r7 CRIT). Quoted names are
    # already neutralized to `qi` upstream; the NOT NULL lookahead still exempts the additive relaxation.
    return bool(re.search(
        r"\bDROP\s+(?:IF\s+EXISTS\s+)?(?!NOT\s+NULL\b)\S", stmt, re.IGNORECASE))


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


# Opaque BEHAVIORAL objects. A RULE rewrites DML — a live-Postgres check proved `CREATE RULE ... DO
# INSTEAD NOTHING` silently turns every DELETE into a no-op, and a code-revert CAN'T drop a pg_rewrite
# rule (cross-family r8 attack-fleet CRITICAL, verified on PG 16). A TRIGGER changes write behavior via
# an opaque function. Treat CREATE/DROP of RULE/TRIGGER like routines: reject, but escapable via the
# narrow RECONCILE_ALLOW_ROUTINE for a blessed additive one (e.g. an updated_at trigger).
_CREATE_BEHAVIORAL_RE = re.compile(r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:CONSTRAINT\s+)?(?:RULE|TRIGGER)\b", re.I)
# DROP FUNCTION|PROCEDURE|ROUTINE|RULE|TRIGGER (PG's ROUTINE drops a function/procedure — r5 HIGH-4).
_DROP_ROUTINE_RE = re.compile(r"\bDROP\s+(FUNCTION|PROCEDURE|ROUTINE|RULE|TRIGGER)\b", re.I)
# The routine/behavioral rules the narrow RECONCILE_ALLOW_ROUTINE escape filters (every OTHER check stays).
_ROUTINE_CHECKERS = (_CREATE_ROUTINE_RE, _CREATE_BEHAVIORAL_RE, _DROP_ROUTINE_RE)

# (checker, why). A checker is a compiled regex (matched with .search) or a callable(stmt)->bool.
_RULES = [
    (_DYNAMIC_DDL_RE,                                                "DO/EXECUTE/CALL — dynamic DDL the linter can't inspect (a contraction can hide in the body); fail-closed"),
    (_CREATE_ROUTINE_RE,                                             "CREATE FUNCTION/PROCEDURE — opaque body can run DDL when invoked (a stripped body defeats inspection); fail-closed"),
    (_CREATE_BEHAVIORAL_RE,                                          "CREATE RULE/TRIGGER — rewrites/changes DML behavior opaquely and survives a code-revert; fail-closed"),
    (_DROP_ROUTINE_RE,                                               "DROP FUNCTION/PROCEDURE/ROUTINE/RULE/TRIGGER — old code may depend on it"),
    (re.compile(r"\bTRUNCATE\b", re.I),                              "TRUNCATE — destroys rows a code-revert cannot restore (old code is stranded with an empty table)"),
    (re.compile(r"\bDROP\s+TABLE\b", re.I),                          "DROP TABLE — old code reads a table that's gone"),
    (re.compile(r"\bDROP\s+(MATERIALIZED\s+VIEW|VIEW)\b", re.I),     "DROP VIEW — old code reads a relation that's gone"),
    (re.compile(r"\bDROP\s+(SEQUENCE|SCHEMA|TYPE)\b", re.I),         "DROP SEQUENCE/SCHEMA/TYPE — old code depends on it"),
    (re.compile(r"\bDROP\s+CONSTRAINT\b", re.I),                     "DROP CONSTRAINT — relaxes/changes a guarantee old code assumes"),
    (re.compile(r"\bDROP\s+ATTRIBUTE\b", re.I),                      "ALTER TYPE DROP ATTRIBUTE — old code reads a composite-type field that's gone (r8, live-PG confirmed)"),
    (_drop_column,                                                   "DROP COLUMN (optional COLUMN kw) — old code reads a column that's gone"),
    (_drop_default,                                                  "DROP DEFAULT — a NOT NULL column with no default breaks old INSERTs that omit it after a revert"),
    (re.compile(r"\bDETACH\s+PARTITION\b", re.I),                    "DETACH PARTITION — removes rows from the parent table old code queries"),
    (re.compile(r"\bNO\s+INHERIT\b", re.I),                          "NO INHERIT — removes a child's rows from the parent old code queries"),
    (re.compile(r"\bALTER\s+SEQUENCE\b.*\bRESTART\b", re.I),         "ALTER SEQUENCE RESTART — reissued IDs can collide (PK violation) with rows old code inserted (kilabz HIGH; oracle deems acceptable — kept per gate)"),
    (re.compile(r"\bCREATE\s+UNIQUE\s+INDEX\b", re.I),               "CREATE UNIQUE INDEX — tightens uniqueness; reverted old code can insert a duplicate and violate it"),
    (re.compile(r"\bSET\s+DEFAULT\s+\(?\s*NULL\b", re.I),            "SET DEFAULT NULL — removes a column's effective default (like DROP DEFAULT); breaks old INSERTs on a NOT NULL column after a revert"),
    (re.compile(r"\bSET\s+GENERATED\s+ALWAYS\b", re.I),             "SET GENERATED ALWAYS — rejects the explicit-value INSERTs old code issues for the column"),
    (re.compile(r"\bRENAME\s+VALUE\b", re.I),                        "ALTER TYPE RENAME VALUE — an enum label old code still writes/compares is gone (r8/fleet, live-PG confirmed)"),
    (re.compile(r"\bRENAME\s+(?:COLUMN\s+|CONSTRAINT\s+|ATTRIBUTE\s+)?\S+\s+TO\b", re.I), "RENAME COLUMN/CONSTRAINT/ATTRIBUTE — old code reads the old name"),
    (re.compile(r"\bRENAME\s+TO\b", re.I),                           "RENAME TABLE — old code reads the old name"),
    (re.compile(r"\bALTER\s+(?:COLUMN\s+|ATTRIBUTE\s+)?\S+\s+(SET\s+DATA\s+)?TYPE\b", re.I), "column/attribute TYPE change — old code mis-reads the type"),
    (re.compile(r"\bSET\s+NOT\s+NULL\b", re.I),                      "SET NOT NULL — breaks old writes that omit the column"),
    (_add_constraint_tightening,                                    "ADD CONSTRAINT (not NOT VALID) — tightens a guarantee old code doesn't honor"),
    (_add_column_not_null_no_default,                               "ADD COLUMN NOT NULL without DEFAULT — breaks old INSERTs / fails on a populated table"),
]


# A hand-written character scanner, NOT a regex (cross-family r4-r6). A regex cannot model Postgres's
# scanner: block comments NEST (not a regular language — a regex can't count depth, r6 CRIT), E'' strings
# take backslash escapes while plain '' strings don't (r6 CRIT), identifiers include Unicode chars (r6
# CRIT), and `--` ends at CR or LF (r6 HIGH). Three review rounds of "one more regex edge" is the tell.
# This scanner consumes each lexical token exactly as Postgres does, so a delimiter INSIDE an open token
# is never re-interpreted and the linter's token boundaries match the engine that actually runs the SQL.
#
# Fail-closed direction: for WELL-FORMED SQL the scanner matches Postgres exactly. The only divergence is
# on malformed input (an unclosed token) — which Postgres also rejects, so nothing executes — and the
# safe direction on any doubt is to UNDER-consume (expose text to the rules → at worst a false positive),
# never OVER-consume (which could swallow a real DROP). A tag/prefix the scanner doesn't recognize is left
# as ordinary text, so its contents stay visible to the rules.
# $tag$ open; tag = a PG dollar-quote tag or empty. PG's scan.l dolq tag is [A-Za-z\200-\377_][...0-9]*
# — the start is a letter/underscore OR ANY high-bit byte; the rest adds digits. Python `\w`/`[^\W\d]`
# alone narrows the high-bit range (a single-char high-bit tag like `$§$` or an NFD `$cafe◌́$` was
# rejected → a false positive on an additive migration, cross-family r7/r8). Add `[^\x00-\x7f]` (any
# non-ASCII char) to both classes to mirror PG. Under-consume direction only: a mis-tokenized dollar
# exposes its body to the rules (a false positive), it never hides a DROP.
_DOLLAR_OPEN = re.compile(r"\$((?:[^\W\d]|[^\x00-\x7f])(?:\w|[^\x00-\x7f])*|)\$")


def _ident_char(c: str) -> bool:
    # Postgres ident_cont is [A-Za-z\200-\377_0-9$] (scan.l) — ASCII alnum/_/$ OR ANY non-ASCII byte
    # (every high-bit char: Unicode letters AND combining marks). str.isalnum() is NARROWER — it is False
    # for a combining mark like U+0301 — which let a fake dollar-quote open mid-identifier and hide a DROP
    # (cross-family r7 CRIT). Mirror PG's byte rule directly: any non-ASCII char is an identifier char.
    return (not c.isascii()) or c.isalnum() or c == "_" or c == "$"


def _scan_quoted(sql: str, j: int, n: int, escape: bool) -> int:
    # From the OPENING quote at j, return the index just past the CLOSING quote. `''` always escapes a
    # quote; a backslash escapes the next char only in an E'' string (escape=True).
    k = j + 1
    while k < n:
        ch = sql[k]
        if escape and ch == "\\":
            k += 2
        elif ch == "'":
            if k + 1 < n and sql[k + 1] == "'":
                k += 2
            else:
                return k + 1
        else:
            k += 1
    return n   # unterminated (Postgres would reject too)


def _normalize(sql: str) -> str:
    """Neutralize comments, string/dollar-quoted bodies, and double-quoted identifiers by scanning the SQL
    exactly as Postgres's lexer would, then collapse whitespace. Comments -> ' ', strings/dollar bodies ->
    "''", double-quoted identifiers -> 'qi' (a safe bareword: a "..." token is always a NAME, never a
    keyword, so it can't smuggle a keyword or hide a space/digit past a rule)."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        two = sql[i:i + 2]
        if two == "--":                                   # line comment -> end at CR or LF (r6 HIGH)
            j = i + 2
            while j < n and sql[j] not in "\r\n":
                j += 1
            out.append(" "); i = j
        elif two == "/*":                                 # block comment -> NESTED depth count (r6 CRIT)
            depth, j = 1, i + 2
            while j < n and depth > 0:
                pair = sql[j:j + 2]
                if pair == "/*":
                    depth += 1; j += 2
                elif pair == "*/":
                    depth -= 1; j += 2
                else:
                    j += 1
            out.append(" "); i = j
        elif c == "$" and not (i > 0 and _ident_char(sql[i - 1])):   # $tag$ dollar body (not mid-ident)
            m = _DOLLAR_OPEN.match(sql, i)
            close = ("$" + m.group(1) + "$") if m else ""
            k = sql.find(close, m.end()) if m else -1
            if m and k != -1:
                out.append(" '' "); i = k + len(close)
            else:
                out.append(c); i += 1                     # not a real dollar-quote -> literal $, keep scanning
        elif c in "Ee" and i + 1 < n and sql[i + 1] == "'" and not (i > 0 and _ident_char(sql[i - 1])):
            out.append(" '' "); i = _scan_quoted(sql, i + 1, n, escape=True)    # E'' string (\ escapes)
        elif c == "'":
            out.append(" '' "); i = _scan_quoted(sql, i, n, escape=False)       # plain '' string
        elif c == '"':                                    # "..." identifier ("" escapes) -> safe bareword
            j = i + 1
            while j < n:
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"':
                        j += 2
                    else:
                        j += 1; break
                else:
                    j += 1
            out.append(" qi "); i = j
        else:
            out.append(c); i += 1
    return re.sub(r"\s+", " ", "".join(out))


# =====================================================================================================
# ALLOWLIST — the actual gate (design §2.8; cross-family review r9). A DENYLIST ("reject known-bad DDL")
# is fail-OPEN by default and cannot converge: Postgres's non-additive DDL surface is large and grows
# every release, so each review round found "one more" un-rejected contraction (SET SCHEMA, RLS, DROP
# EXTENSION/DOMAIN/POLICY/OWNED, REPLICA IDENTITY, ADD GENERATED ALWAYS, inline column constraints, …).
# For a gate protecting an autonomous factory a false-NEGATIVE (a hidden contraction slips through) is far
# worse than a false-POSITIVE (an extra human sign-off), so INVERT it: a statement passes ONLY if it
# matches a provably-ADDITIVE shape; everything else is rejected fail-closed. New/obscure non-additive DDL
# now needs NO new rule — it simply isn't additive. The _RULES denylist below is kept ONLY to annotate a
# rejection with a specific reason (else a generic one).

# CREATE of a brand-NEW standalone object is additive: old code never referenced it. Excludes `OR REPLACE`
# (mutates an existing object — a VIEW replace can drop a column), `UNIQUE INDEX` (tightens an existing
# table), and every opaque/behavioral CREATE (FUNCTION/PROCEDURE/RULE/TRIGGER/POLICY) by listing only the
# safe new-object kinds. `CREATE TABLE ... PARTITION OF` / `... AS SELECT` are still new tables (additive).
_ADDITIVE_CREATE_RE = re.compile(
    r"^CREATE\s+(?:GLOBAL\s+|LOCAL\s+|TEMP(?:ORARY)?\s+|UNLOGGED\s+)*"
    r"(?:TABLE|SEQUENCE|SCHEMA|VIEW|MATERIALIZED\s+VIEW|EXTENSION|DOMAIN|TYPE|COLLATION|STATISTICS|INDEX)\b",
    re.IGNORECASE)


def _additive_alter_table_clause(c: str) -> bool:
    # An ALTER TABLE is additive only if EVERY top-level clause is: ADD COLUMN (nullable-or-defaulted, no
    # inline tightening constraint), ATTACH PARTITION, or INHERIT. Anything else (DROP/ALTER/RENAME/SET/
    # ENABLE/DISABLE/OWNER/DETACH/NO INHERIT/VALIDATE/…) makes the statement non-additive.
    c = c.strip()
    if re.match(r"^ADD\s+COLUMN\b", c, re.IGNORECASE):
        # inline PRIMARY KEY/UNIQUE/CHECK/REFERENCES on the new column tightens writes old code still does
        # (a PK/UNIQUE the old omitted-column INSERT violates; a CHECK it can't satisfy) — cross-family r9 HIGH-2.
        if re.search(r"\b(PRIMARY\s+KEY|UNIQUE|CHECK|REFERENCES)\b", c, re.IGNORECASE):
            return False
        # NOT NULL without a DEFAULT breaks old INSERTs; a GENERATED column supplies its own value (r5 FP-6).
        if _NOT_NULL_RE.search(c) and not _DEFAULT_RE.search(c) and not _GENERATED_RE.search(c):
            return False
        return True
    if re.match(r"^ATTACH\s+PARTITION\b", c, re.IGNORECASE):
        return True
    if re.match(r"^INHERIT\b", c, re.IGNORECASE):   # NOT "NO INHERIT" (that clause starts with NO)
        return True
    # SET DEFAULT <non-null value> is additive: old INSERTs that omit the column now get the value instead
    # of erroring; a code-revert keeps the default (harmless). SET DEFAULT NULL is the contraction (a NOT
    # NULL column loses its effective default) and is NOT allowed here.
    if re.search(r"\bSET\s+DEFAULT\b", c, re.IGNORECASE) and not re.search(r"\bSET\s+DEFAULT\s+\(?\s*NULL\b", c, re.IGNORECASE):
        return True
    return False


_CREATE_TABLE_NAME_RE = re.compile(
    r"^CREATE\s+(?:GLOBAL\s+|LOCAL\s+|TEMP(?:ORARY)?\s+|UNLOGGED\s+)*TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([^\s(]+)",
    re.IGNORECASE)


def _created_table_names(statements: list[str]) -> frozenset[str]:
    # Names of tables CREATEd in THIS migration file. Any op on such a table is additive — old code has no
    # knowledge of a table born this migration (r9: a UNIQUE INDEX on a same-migration table is additive).
    # `qi` (a quoted name) is EXCLUDED: neutralization collapses every quoted name to `qi`, so trusting a
    # `qi` match would let an op on an EXISTING quoted table masquerade as same-migration (fail-closed).
    names = set()
    for s in statements:
        m = _CREATE_TABLE_NAME_RE.match(s.strip())
        if m and m.group(1) != "qi":
            names.add(m.group(1))
    return frozenset(names)


def _target_after_on(s: str) -> str | None:
    m = re.search(r"\bON\s+(?:ONLY\s+)?([^\s(]+)", s, re.IGNORECASE)
    return m.group(1) if m else None


def _is_additive(stmt: str, allow_routine: bool = False, created: frozenset[str] = frozenset()) -> bool:
    """True iff `stmt` (already _normalize'd) is a provably-additive migration statement — the allowlist."""
    s = stmt.strip()
    if not s:
        return True
    # Operator-blessed routine/behavioral object (RECONCILE_ALLOW_ROUTINE): a CREATE/DROP of a
    # function/procedure/rule/trigger the operator vouched for. Still NARROW — a DROP TABLE alongside is a
    # separate statement and is judged on its own (not additive), so the file still fails.
    if allow_routine and (_CREATE_ROUTINE_RE.search(s) or _CREATE_BEHAVIORAL_RE.search(s)
                          or _DROP_ROUTINE_RE.search(s)):
        return True
    if re.match(r"^CREATE\b", s, re.IGNORECASE):
        if re.match(r"^CREATE\s+OR\s+REPLACE\b", s, re.IGNORECASE):
            # Only a VIEW / MATERIALIZED VIEW is re-derivable + data-lossless: its definition lives in the
            # migration, so a code-revert re-runs prev_good's migration and restores the old view. CREATE OR
            # REPLACE FUNCTION stays rejected (opaque body can run DDL) — escapable via --allow-routine.
            return bool(re.match(r"^CREATE\s+OR\s+REPLACE\s+(?:MATERIALIZED\s+)?VIEW\b", s, re.IGNORECASE))
        if re.search(r"^CREATE\s+(?:\w+\s+)*?UNIQUE\s+INDEX\b", s, re.IGNORECASE):
            tgt = _target_after_on(s)                      # additive ONLY on a table born this migration
            return tgt is not None and tgt != "qi" and tgt in created
        return bool(_ADDITIVE_CREATE_RE.match(s))          # the safe brand-new object kinds
    if re.match(r"^ALTER\s+TABLE\b", s, re.IGNORECASE):
        m = re.match(r"^ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?([^\s(]+)\s+", s, re.IGNORECASE)
        if not m:
            return False
        if m.group(1) != "qi" and m.group(1) in created:
            return True                                    # any op on a same-migration new table is additive
        return all(_additive_alter_table_clause(c) for c in _top_level_clauses(s[m.end():]))
    if re.match(r"^ALTER\s+TYPE\b", s, re.IGNORECASE):     # enum ADD VALUE / composite ADD ATTRIBUTE only
        m = re.match(r"^ALTER\s+TYPE\s+(?:IF\s+EXISTS\s+)?\S+\s+", s, re.IGNORECASE)
        return bool(m and re.match(r"^ADD\s+(?:VALUE|ATTRIBUTE)\b", s[m.end():], re.IGNORECASE))
    if re.match(r"^COMMENT\s+ON\b", s, re.IGNORECASE):     # metadata only
        return True
    # Schema-NEUTRAL data DML is out of scope for a SCHEMA-additivity gate (the schema is unchanged, so old
    # code still runs). Real idempotent backfills use INSERT ... ON CONFLICT, UPDATE, and SELECT ... FOR
    # UPDATE. TRUNCATE and DELETE (bulk row destruction that strands old code) are NOT allowed.
    if re.match(r"^(?:INSERT\s+INTO|UPDATE)\b", s, re.IGNORECASE):
        return True
    if re.match(r"^SELECT\b", s, re.IGNORECASE):
        # A read query (has FROM) is schema-neutral. A bare `SELECT f()` (no FROM) is a function INVOCATION
        # that could run DDL (the r4 opaque-call vector) -> reject fail-closed; real migrations lock via
        # SELECT ... FROM ... FOR UPDATE. Residual: a DDL-running function in a FROM-query's target list.
        return bool(re.search(r"\bFROM\b", s, re.IGNORECASE))
    return False                                           # fail-closed: unrecognized => not provably additive


def _reject_reason(stmt: str, allow_routine: bool = False) -> str:
    # A rejected statement gets a SPECIFIC message if the denylist recognizes the contraction, else generic.
    for checker, why in _RULES:
        if allow_routine and checker in _ROUTINE_CHECKERS:
            continue
        hit = checker.search(stmt) if isinstance(checker, re.Pattern) else checker(stmt)
        if hit:
            return why
    return ("not a provably-additive statement (allowlist gate, fail-closed): only CREATE-of-a-new-object, "
            "CREATE OR REPLACE VIEW, ALTER TABLE ADD COLUMN / ATTACH PARTITION / INHERIT, ALTER TYPE ADD "
            "VALUE|ATTRIBUTE, COMMENT, and INSERT/UPDATE/SELECT data-DML auto-pass; else needs "
            "RECONCILE_ALLOW_CONTRACTION (or --allow-routine for a blessed function/trigger)")


def lint_file(path: Path, allow_routine: bool = False) -> list[str]:
    # ALLOWLIST gate: reject any statement that is not provably additive (fail-closed). allow_routine
    # (operator-gated via RECONCILE_ALLOW_ROUTINE) additionally blesses a routine/behavioral CREATE/DROP —
    # narrow: a DROP TABLE alongside is its own statement and still fails (r5 FP-7).
    statements = [s.strip() for s in _normalize(path.read_text()).split(";")]
    created = _created_table_names(statements)   # tables born THIS migration -> any op on them is additive
    out: list[str] = []
    for stmt in statements:
        if not stmt or _is_additive(stmt, allow_routine, created):
            continue
        out.append(f"{path.name}: NON-ADDITIVE — {_reject_reason(stmt, allow_routine)}\n    {stmt[:140]}")
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
