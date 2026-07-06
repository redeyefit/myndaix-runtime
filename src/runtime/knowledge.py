"""knowledge.py — the curator rung's PURE core (no DB, no LLM, no subprocess): corpus walk +
parse + canonical-path policy for ingest, and the deterministic validation grammar the curate
guard uses at promote time. Design: docs/curator-design.md v0.4.

Files on disk are the SOURCE OF TRUTH; the knowledge_doc table is a derived, rebuildable index.
Everything here is deliberately deterministic and unit-testable without Postgres — the I/O verbs
live in knowledgerecord.py, the guard in curate.py.
"""
from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

# ---- scope -> root: a STATIC allowlist (design: never derive a path from a scope string) -------
# Extra scopes come from $MYNDAIX_KNOWLEDGE_SCOPES ("name=/abs/path,name2=/abs/path2") — an
# operator/deploy decision, never caller input. Unknown scope = hard error at every verb
# (fail-closed: misconfiguration must never read as "no knowledge").
_ENV_SCOPES = "MYNDAIX_KNOWLEDGE_SCOPES"


def known_scopes() -> dict[str, Path]:
    scopes = {"research": Path(os.environ.get("HOME", str(Path.home()))) / "research"}
    for entry in os.environ.get(_ENV_SCOPES, "").split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        name, _, root = entry.partition("=")
        name, root = name.strip(), root.strip()
        # the scope NAME is interpolated into lock keys/labels — keep it path-safe; the ROOT must
        # be absolute (a relative root would silently depend on the caller's cwd).
        if re.fullmatch(r"[a-z0-9][a-z0-9._-]*", name) and root.startswith("/"):
            scopes[name] = Path(root)
    return scopes


def resolve_scope(scope: str) -> Path:
    """The scope's corpus root, or ValueError (HARD error — all verbs fail closed on it)."""
    roots = known_scopes()
    if scope not in roots:
        raise ValueError(f"unknown scope {scope!r} (known: {', '.join(sorted(roots))})")
    return roots[scope]


# ---- corpus walk (ingest + stage-in share this file-set definition) ----------------------------
# Directory names pruned at any depth; secrets-bearing session logs (.playwright-mcp) and
# machine noise never reach the index OR the staged workspace (the read boundary IS the copy).
NOISE_DIRS = {"__pycache__", ".playwright-mcp", ".claude", ".git", "node_modules"}
_NOISE_DIR_PREFIXES = (".venv",)          # .venv, .venv-higgsfield, ...

BODY_CAP_BYTES = 900_000                  # tsvector hard limit is 1MB; cap with margin
TRUNCATION_MARKER = "\n\n[truncated for index — full file on disk]\n"

_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})-")
_FM_DATE_RE = re.compile(r"^date:\s*['\"]?(\d{4}-\d{2}-\d{2})", re.MULTILINE)
_FM_TAGS_RE = re.compile(r"^tags:\s*(.+)$", re.MULTILINE)
_HEADING_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
_CTRL_IN_NAME_RE = re.compile(r"[\x00-\x1f\x7f]")


@dataclass
class DocRecord:
    path: str                 # relative, NFC-normalized
    title: str
    tags: str
    doc_date: str | None      # ISO YYYY-MM-DD or None
    body: str
    content_sha: str
    lossy: bool


@dataclass
class WalkResult:
    docs: list[DocRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # every eligible file (md or not) — the stage-in MANIFEST + "un-indexed artifact" lint input
    artifacts: list[str] = field(default_factory=list)


def _is_noise_dir(name: str) -> bool:
    return name in NOISE_DIRS or name.startswith(_NOISE_DIR_PREFIXES)


def _eligible_file(root: Path, p: Path, warnings: list[str]) -> bool:
    """The shared file gate for ingest AND stage-in (fail-closed on anything odd):
    regular, single-linked, non-hidden, sane name, really inside root."""
    name = p.name
    if name.startswith("."):
        return False
    if _CTRL_IN_NAME_RE.search(name):
        warnings.append(f"skipped {name!r}: control chars in filename")
        return False
    try:
        st = p.lstat()
    except OSError as e:
        warnings.append(f"skipped {name!r}: lstat failed ({e})")
        return False
    if p.is_symlink() or not p.is_file():
        warnings.append(f"skipped {name!r}: not a regular file")
        return False
    if st.st_nlink > 1:      # a hardlink under an allowed name can alias content outside the corpus
        warnings.append(f"skipped {name!r}: hardlinked (st_nlink={st.st_nlink})")
        return False
    try:                     # traversal belt: the resolved path must stay inside the resolved root
        p.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        warnings.append(f"skipped {name!r}: escapes the corpus root")
        return False
    return True


def parse_doc(rel_path: str, raw: bytes) -> DocRecord:
    """Deterministic parse of one markdown file. Never raises on content."""
    lossy = False
    body = raw.decode("utf-8", errors="replace")
    if "�" in body and b"\xef\xbf\xbd" not in raw:   # replacement chars we introduced
        lossy = True
    if "\x00" in body:
        body = body.replace("\x00", "")
        lossy = True
    if len(body.encode("utf-8", errors="replace")) > BODY_CAP_BYTES:
        body = body.encode("utf-8", errors="replace")[:BODY_CAP_BYTES] \
                   .decode("utf-8", errors="ignore") + TRUNCATION_MARKER
        lossy = True

    m = _HEADING_RE.search(body)
    title = (m.group(1).strip() if m else Path(rel_path).stem)[:300]
    tm = _FM_TAGS_RE.search(body[:2000])
    tags = re.sub(r"[\[\]'\"#]", " ", tm.group(1)).strip()[:300] if tm else ""

    fname_date = _DATE_PREFIX_RE.match(Path(rel_path).name)
    fm_date = _FM_DATE_RE.search(body[:2000])
    doc_date = fname_date.group(1) if fname_date else (fm_date.group(1) if fm_date else None)

    return DocRecord(path=rel_path, title=title, tags=tags, doc_date=doc_date, body=body,
                     content_sha=hashlib.sha256(raw).hexdigest(), lossy=lossy)


def date_disagreement(rel_path: str, body: str) -> tuple[str, str] | None:
    """(filename_date, frontmatter_date) when BOTH exist and differ — filename wins, ingest WARNs
    (citation dates are trust-bearing; silent precedence hides stale copies)."""
    f = _DATE_PREFIX_RE.match(Path(rel_path).name)
    fm = _FM_DATE_RE.search(body[:2000])
    if f and fm and f.group(1) != fm.group(1):
        return (f.group(1), fm.group(1))
    return None


def walk_corpus(root: Path) -> WalkResult:
    """Walk a corpus root: parse every eligible *.md, list every eligible artifact. Deterministic
    order; case-insensitive duplicate basenames WARN (APFS is case-insensitive — first wins)."""
    res = WalkResult()
    if not root.is_dir():
        raise ValueError(f"corpus root {root} is not a directory")
    seen_ci: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not _is_noise_dir(d) and not d.startswith("."))
        for name in sorted(filenames):
            p = Path(dirpath) / name
            if not _eligible_file(root, p, res.warnings):
                continue
            rel = unicodedata.normalize("NFC", str(p.relative_to(root)))
            ci = rel.lower()
            if ci in seen_ci:
                res.warnings.append(f"case-insensitive duplicate: {rel!r} vs {seen_ci[ci]!r} — first wins")
                continue
            seen_ci[ci] = rel
            res.artifacts.append(rel)
            if not name.lower().endswith(".md"):
                continue
            try:
                raw = p.read_bytes()
            except OSError as e:
                res.warnings.append(f"skipped {rel!r}: read failed ({e})")
                continue
            doc = parse_doc(rel, raw)
            dd = date_disagreement(rel, doc.body)
            if dd:
                res.warnings.append(f"{rel}: filename date {dd[0]} != frontmatter date {dd[1]} — filename wins")
            res.docs.append(doc)
    return res


# ---- promote-side validation grammar (the guard's deterministic rules) -------------------------
# New curator files: top-level, .md, conservative charset, no dot-segments, no hidden files.
NEW_FILE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*\.md$")
PROMOTE_FILE_CAP_BYTES = 256_000

# [[name]] / [[name|label]] / [[name#section]]: existence checked on `name` vs .md basenames,
# case-insensitive, NFC, extension optional, #section ignored (design v0.4 grammar).
_WIKILINK_RE = re.compile(r"\[\[([^\[\]\n|#]+)(?:#[^\[\]\n|]*)?(?:\|[^\[\]\n]*)?\]\]")

# Secret patterns: a GUARDRAIL, not a proof (design v0.4 — the stage-in filter is the real
# boundary; this catches the obvious classes in agent-authored output).
_SECRET_RES = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                      # AWS access key id
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),            # GitHub tokens
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),          # Slack
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),                 # OpenAI-style
    re.compile(r"\beyJ[A-Za-z0-9_-]{40,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),  # JWT
]
# Fence-lookalike regions in a promoted file could forge the prompt-fencing protocol downstream.
_FENCE_LOOKALIKE_RE = re.compile(r"===(?:BEGIN|END)[^\n]*?===")


def valid_new_filename(name: str) -> bool:
    return bool(NEW_FILE_RE.fullmatch(unicodedata.normalize("NFC", name))) and ".." not in name


def wikilinks(text: str) -> list[str]:
    return [m.group(1).strip() for m in _WIKILINK_RE.finditer(text)]


def link_resolves(target: str, md_basenames: set[str]) -> bool:
    """md_basenames: lowercase NFC basenames WITHOUT extension."""
    t = unicodedata.normalize("NFC", target).strip().lower()
    t = t[:-3] if t.endswith(".md") else t
    return t in md_basenames


def content_violations(name: str, data: bytes, md_basenames: set[str]) -> list[str]:
    """Deterministic content checks for ONE promoted file. Empty list = clean."""
    out: list[str] = []
    if len(data) > PROMOTE_FILE_CAP_BYTES:
        out.append(f"{name}: exceeds {PROMOTE_FILE_CAP_BYTES}B promote cap")
        return out
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return [f"{name}: not valid UTF-8"]
    if "\x00" in text:
        return [f"{name}: contains NUL"]
    for pat in _SECRET_RES:
        if pat.search(text):
            out.append(f"{name}: secret-pattern match ({pat.pattern[:30]}…)")
    if _FENCE_LOOKALIKE_RE.search(text):
        out.append(f"{name}: nonce-fence-lookalike region")
    for target in wikilinks(text):
        if not link_resolves(target, md_basenames):
            out.append(f"{name}: ghost wikilink [[{target}]]")
    return out


def index_violations(index_text: str, md_files: list[str]) -> list[str]:
    """index.md structural validation: non-empty, every corpus .md mentioned (completeness — a
    0-byte/gutted 'edit' fails here), every wikilink resolves. Non-md artifacts are free text."""
    out: list[str] = []
    if len(index_text.strip()) < 20:
        return ["index.md: empty/gutted (fails completeness)"]
    low = unicodedata.normalize("NFC", index_text).lower()
    for f in md_files:
        base = Path(f).name.lower()
        if base != "index.md" and base not in low:
            out.append(f"index.md: missing entry for {f}")
    bases = {Path(f).name[:-3].lower() for f in md_files}
    for target in wikilinks(index_text):
        if not link_resolves(target, bases):
            out.append(f"index.md: ghost wikilink [[{target}]]")
    return out


# ---- recall query helpers (the ladder's deterministic pieces) -----------------------------------
QUERY_CAP_CHARS = 512
_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]*")


def prefix_tokens(query: str) -> list[str]:
    """Sanitized tokens for the to_tsquery('tok:* & …') rung. Empty list = skip the rung
    (all-stopword/punctuation-only queries must not error)."""
    return [t.lower() for t in _TOKEN_RE.findall(query[:QUERY_CAP_CHARS])][:8]


def ilike_pattern(query: str) -> str:
    """%-wrapped ILIKE pattern with %/_/\\ escaped (wildcards in the query are literal)."""
    q = query[:QUERY_CAP_CHARS].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{q}%"
