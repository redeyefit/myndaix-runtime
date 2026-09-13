"""The S7 proposer — turns `ready` capture_candidate classes into skill-draft PRs (auto-capture's
acting rung). A separate, flag-gated launchd job (`ai.myndaix.proposer`); ONE bounded tick then
exits (not a daemon). NEVER promotes — it opens a `--draft` PR that rides the unchanged human-merge
gate (skills/** is automerge-denylisted); the human authoring the body IS the promotion.

Run one tick:
    MYNDAIX_DSN=... GH_TOKEN=... PYTHONPATH=src python3 -m runtime.proposer tick
Dry-run (decide + log, mutate NOTHING — no DB, no git, no gh):
    MYNDAIX_PROPOSER_DRY_RUN=1 ... python3 -m runtime.proposer tick

DESIGN: docs/auto-capture-design.md (v0.6 — cross-family hardened). The pure render/slug/path core
is runtime.capture; the S6 state-machine verbs are ledger.postgres_store. This file is the gh/git
side-effect driver (S5/S7). Safety properties, each earned by a cross-family finding:
  - A2  repo_scope resolved by EXACT-KEY lookup in the trusted repos.json MAP; the scope string is
        NEVER passed to a git/gh argv (a polluted/hostile scope → skip, never a wrong-repo PR).
  - K1  recovery/adoption is by BRANCH + authenticated BOT-AUTHOR identity, NEVER by content hash
        (hash-verify-before-adopt infinite-loops once a human edits the stub — oracle).
  - K2  adoption validates the ACTUAL PR (our repo, not a fork; single match) and the diff is the
        one permitted path (assert_only_skill_path on the real staged diff).
  - K3  the SKILL.md write is O_EXCL + O_NOFOLLOW with symlinked-ancestor rejection (a post-write
        assert cannot stop a symlink escape DURING the write).
  - A5  RECONCILE acts ONLY on definitive merged/closed; gh-unknown (None) → DEFER (never mis-decline).
  - A7  TTL close re-reads LIVE PR state first; merged always wins; a close is never mis-recorded.
  - A6  proposer-owned worktree GC at tick start (flock proves the prior tick is dead).
  - A9  DRY_RUN suppresses EVERY mutation (DB + git + gh) across all three passes.
  - K4  every un-authored draft carries capture.STUB_MARKER; the controller index + CI refuse it.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from runtime import capture
from runtime.ledger.postgres_store import PostgresLedger

# -- config (all knobs via env; STRICT digit-only parse so a bad launchd value defaults, not crash) --
DSN = os.environ.get("MYNDAIX_DSN", "postgresql://localhost/runtime")
HOME = Path(os.environ.get("HOME", str(Path.home())))
ORCH = HOME / ".myndaix" / "orchestrator"
STATE = ORCH / "state"
REPOS_JSON = Path(os.environ.get("MYNDAIX_REPOS_JSON", str(ORCH / "repos.json")))
LOCK = ORCH / "proposer.lock"
ENABLED_FLAG = ORCH / "PROPOSER_ENABLED"
WORKTREE_ROOT = Path(os.environ.get("MYNDAIX_PROPOSER_WORKTREES", str(STATE / "proposer-worktrees")))
BOT_NAME = os.environ.get("MYNDAIX_PROPOSER_BOT_NAME", "myndaix-proposer")
BOT_EMAIL = os.environ.get("MYNDAIX_PROPOSER_BOT_EMAIL", "proposer@myndaix.local")


def _int_env(name: str, default: int) -> int:
    """STRICT digit-only env knob (mirrors automerge/controller): a malformed launchd value defaults
    rather than crashing the service at import; caps to avoid Python's int-str limit."""
    val = os.environ.get(name, "")
    if not re.fullmatch(r"[0-9]+", val):
        return default
    val = val.lstrip("0") or "0"
    return 2**31 - 1 if len(val) > 10 else min(int(val), 2**31 - 1)


def _th(name: str) -> int:
    """A capture threshold from $CAPTURE_<NAME>, else the pure-core default (config, never a rewrite)."""
    return _int_env(f"CAPTURE_{name}", capture.DEFAULTS[name])


MAX_OPEN = lambda: _th("MAX_OPEN")            # re-read per tick so a live env change takes effect
TTL_DAYS = lambda: _th("TTL_DAYS")
MAX_PER_TICK = _int_env("MYNDAIX_PROPOSER_MAX_TICK", 2)      # K6: bound proposals opened per tick
REAP_TIMEOUT_MIN = _int_env("MYNDAIX_PROPOSER_REAP_MIN", 30)  # release a 'proposing' row stuck this long
GH_TIMEOUT = _int_env("MYNDAIX_PROPOSER_GH_TIMEOUT", 60)
GIT_TIMEOUT = _int_env("MYNDAIX_PROPOSER_GIT_TIMEOUT", 120)
DRY_RUN = os.environ.get("MYNDAIX_PROPOSER_DRY_RUN") == "1"

_PR_URL_RE = re.compile(r"/pull/(\d+)\s*$")   # `gh pr create` prints the PR URL, NOT json (K6)


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [proposer]{' DRY' if DRY_RUN else ''} {msg}", file=sys.stderr, flush=True)


# =====================================================================================
# I/O: git + gh — all argv (never shell), output validated. Mirrors automerge's env scrub.
# =====================================================================================
def _git_env() -> dict:
    env = {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        "HOME": str(HOME),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "https:ssh:file",
    }
    for k in ("SSH_AUTH_SOCK", "TMPDIR", "LANG", "GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


# EVERY proposer git op runs with hooks DISABLED (kilabz BLOCKER + MAJOR, one fix): (a) a tracked
# checkout's hooks (core.hooksPath / .git/hooks) execute during worktree-add/commit/push and inherit
# GH_TOKEN — a hostile hook exfiltrates the bot credential or stages files AFTER the path assert;
# (b) the orchestrator's OWN pre-push hook would fire play-review from the ephemeral worktree path,
# which the proposer then deletes mid-review. /dev/null is not a directory, so git finds no hooks.
# The PR + human merge is the review gate for a proposal; the pre-push review is not wanted here.
_GIT_NOHOOKS = ("-c", "core.hooksPath=/dev/null")


def _git(cwd: Path, *args: str, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(cwd), "--no-pager", *_GIT_NOHOOKS, *args],
                          capture_output=True, text=True, env=_git_env(),
                          timeout=timeout or GIT_TIMEOUT, check=False)


def _gh_json(nwo: str, *args: str):
    """A gh command scoped to --repo <nwo> that prints JSON → parsed value, or None on ANY failure
    (nonzero exit, timeout, non-JSON). None means UNKNOWN — callers must DEFER, never decide (A5)."""
    try:
        r = subprocess.run(["gh", *args, "--repo", nwo], capture_output=True, text=True,
                           env=_git_env(), timeout=GH_TIMEOUT, check=False)
    except subprocess.SubprocessError as e:
        log(f"gh {' '.join(args)}: {e!r} — unknown"); return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except (json.JSONDecodeError, ValueError):
        return None


def _gh_close(nwo: str, pr_number: int) -> bool:
    """`gh pr close` prints NO JSON, so success = exit 0 (NOT _gh_json, whose None conflates a
    successful non-JSON mutation with failure — oracle code-review BLOCKER: a failed close that
    still records 'stale' orphans a live OPEN PR). Callers gate the DB flip on this."""
    try:
        r = subprocess.run(["gh", "pr", "close", str(int(pr_number)), "--repo", nwo],
                           capture_output=True, text=True, env=_git_env(),
                           timeout=GH_TIMEOUT, check=False)
    except subprocess.SubprocessError as e:
        log(f"gh pr close #{pr_number}: {e!r} — unknown"); return False
    if r.returncode != 0:
        log(f"gh pr close #{pr_number} failed: {r.stderr.strip()[:150]}")
    return r.returncode == 0


# -- the trusted repo allowlist MAP (A2): repo_scope → {path, nwo}, exact-key only ---------------
def resolve_repo(repo_scope: str) -> Optional[dict]:
    """EXACT-KEY lookup of `repo_scope` in repos.json → the local path + the nwo resolved FROM THAT
    PATH (never from repo_scope). A scope not on the list, or whose path is not a real git repo, or
    whose nwo can't be resolved → None (fail-closed: a polluted/hostile scope can never open a PR
    against an unintended repo). repo_scope NEVER reaches a git/gh argv."""
    try:
        raw = json.loads(REPOS_JSON.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        log(f"repos.json unreadable ({e})"); return None
    entry = raw.get(repo_scope) if isinstance(raw, dict) else None
    if not isinstance(entry, dict) or repo_scope.startswith("_"):
        return None                                  # unknown / non-repo scope → skip
    p = entry.get("path")
    if not p:
        return None
    path = Path(p).expanduser().resolve()
    if not (path.is_dir() and (path / ".git").exists()):
        return None
    # ONE comma-joined --json field list (kilabz BLOCKER: as two argv items, the second becomes
    # gh's positional REPOSITORY argument — dref never returns and every repo resolves to None).
    info = _gh_json_path(path, "repo", "view", "--json", "nameWithOwner,defaultBranchRef")
    nwo = info.get("nameWithOwner") if isinstance(info, dict) else None
    dref = (info.get("defaultBranchRef") or {}).get("name") if isinstance(info, dict) else None
    if not (isinstance(nwo, str) and "/" in nwo and isinstance(dref, str) and dref):
        log(f"{repo_scope}: cannot resolve nwo/defaultBranch via gh — skip"); return None
    return {"path": path, "nwo": nwo, "default_branch": dref}


def _gh_json_path(path: Path, *args: str):
    """gh JSON run with cwd=<repo path> (for `repo view` which infers the repo from cwd)."""
    try:
        r = subprocess.run(["gh", *args], cwd=str(path), capture_output=True, text=True,
                           env=_git_env(), timeout=GH_TIMEOUT, check=False)
    except subprocess.SubprocessError:
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except (json.JSONDecodeError, ValueError):
        return None


# -- single-instance flock (mirror automerge) -----------------------------------------------------
_LOCK_FD: Optional[int] = None


def acquire_lock() -> bool:
    global _LOCK_FD
    ORCH.mkdir(parents=True, exist_ok=True)
    if LOCK.is_dir():
        shutil.rmtree(LOCK, ignore_errors=True)
    fd = os.open(str(LOCK), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd); log("another tick holds the lock — exiting"); return False
    _LOCK_FD = fd
    return True


def release_lock() -> None:
    global _LOCK_FD
    if _LOCK_FD is not None:
        try:
            fcntl.flock(_LOCK_FD, fcntl.LOCK_UN); os.close(_LOCK_FD)
        except OSError:
            pass
        _LOCK_FD = None


# -- worktree GC (A6): the flock proves the prior tick is dead, so any leftover worktree is safe to
#    remove. rmtree the dirs; a per-repo `git worktree prune` (in _make_worktree) clears the stale
#    admin entries a bare rmtree leaves behind (kilabz/oracle: remove via git, not just rmtree).
def gc_worktrees() -> None:
    if DRY_RUN:
        return
    shutil.rmtree(WORKTREE_ROOT, ignore_errors=True)


# -- K3: symlink-safe, creation-only write of skills/<slug>/SKILL.md inside the worktree ----------
def _safe_write_skill(wt: Path, slug: str, rendered: str) -> bool:
    """Write ONLY skills/<slug>/SKILL.md via DIRECTORY DESCRIPTORS with O_NOFOLLOW at EVERY
    component (K3 + kilabz race fix: a static pre-check can be swapped for a symlink between the
    check and the open — O_NOFOLLOW on the final component alone doesn't cover ancestors; openat
    against a pinned dir-fd does). An existing slug dir/target is refused (creation-only — enforces
    the v1 'never edit an existing skill' promise). Returns True on a clean write."""
    fds: list[int] = []
    try:
        try:
            wt_fd = os.open(str(wt), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as e:
            log(f"open worktree dir failed ({e}) — refuse"); return False
        fds.append(wt_fd)
        try:
            os.mkdir("skills", dir_fd=wt_fd)
        except FileExistsError:
            pass                                   # tracked skills/ dir already in the checkout
        except OSError as e:
            log(f"mkdir skills/ failed ({e}) — refuse"); return False
        try:                                       # ELOOP here = skills/ is a symlink → refuse
            skills_fd = os.open("skills", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=wt_fd)
        except OSError as e:
            log(f"skills/ is not a real dir in the worktree ({e}) — refuse write"); return False
        fds.append(skills_fd)
        try:
            os.mkdir(slug, dir_fd=skills_fd)       # creation-only: an existing slug dir refuses
        except OSError as e:
            log(f"skills/{slug} already exists or mkdir failed ({e}) — refuse (creation-only)"); return False
        try:
            slug_fd = os.open(slug, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=skills_fd)
        except OSError as e:
            log(f"skills/{slug} vanished/replaced after mkdir ({e}) — refuse"); return False
        fds.append(slug_fd)
        try:
            fd = os.open("SKILL.md", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o644, dir_fd=slug_fd)
        except OSError as e:
            log(f"O_EXCL open of SKILL.md failed ({e}) — refuse"); return False
        try:
            os.write(fd, rendered.encode())
        finally:
            os.close(fd)
        return True
    finally:
        for f in fds:
            try:
                os.close(f)
            except OSError:
                pass


def _staged_paths(wt: Path) -> list[str]:
    """The NUL-delimited names git actually staged (A10: assert on git's report, not the intended
    string). rename-detection is off (diff --name-only doesn't rename-detect by default here)."""
    r = _git(wt, "diff", "--cached", "--name-only", "-z")
    if r.returncode != 0:
        return []
    return [p for p in r.stdout.split("\0") if p]


def _make_proposal_commit(repo: dict, wt_name: str, slug: str, rendered: str) -> Optional[Path]:
    """git worktree add off the default branch, safe-write the SKILL.md, assert ONLY that path
    changed (S1/K2), commit. Returns the worktree path on success (caller pushes + opens the PR),
    else None (caller releases the claim)."""
    path = repo["path"]
    _git(path, "worktree", "prune")                              # clear stale admin entries (A6)
    fetch = _git(path, "fetch", "--no-tags", "origin", repo["default_branch"], timeout=GIT_TIMEOUT)
    if fetch.returncode != 0:                                    # a silent stale base is a wrong-diff PR
        log(f"fetch origin/{repo['default_branch']} failed: {fetch.stderr.strip()[:150]}"); return None
    wt = WORKTREE_ROOT / wt_name
    shutil.rmtree(wt, ignore_errors=True)
    WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)
    add = _git(path, "worktree", "add", "--detach", str(wt), f"origin/{repo['default_branch']}")
    if add.returncode != 0:
        log(f"worktree add failed: {add.stderr.strip()[:200]}"); return None
    ok = _safe_write_skill(wt, slug, rendered)
    if not ok:
        _git(path, "worktree", "remove", "--force", str(wt)); return None
    _git(wt, "add", "--", capture.skill_path(slug))
    staged = _staged_paths(wt)
    if not capture.assert_only_skill_path(staged, slug):          # S1/K2: exactly skills/<slug>/SKILL.md
        log(f"staged diff is not exactly {capture.skill_path(slug)} (got {staged}) — abort")
        _git(path, "worktree", "remove", "--force", str(wt)); return None
    commit = _git(wt, "-c", f"user.name={BOT_NAME}", "-c", f"user.email={BOT_EMAIL}",
                  "-c", "commit.gpgsign=false",
                  "commit", "-m", f"skill(auto): propose {slug} (unauthored stub — review before merge)")
    if commit.returncode != 0:
        log(f"commit failed: {commit.stderr.strip()[:200]}"); _git(path, "worktree", "remove", "--force", str(wt)); return None
    # belt: verify the FINAL COMMITTED change set, not just the pre-commit staging (kilabz: with
    # hooks disabled this should never differ, but the push gate must not rest on that assumption)
    show = _git(wt, "show", "--name-only", "--format=", "-z", "HEAD")
    committed = [p for p in show.stdout.split("\0") if p]
    if show.returncode != 0 or not capture.assert_only_skill_path(committed, slug):
        log(f"COMMITTED change set is not exactly {capture.skill_path(slug)} (got {committed}) — abort")
        _git(path, "worktree", "remove", "--force", str(wt)); return None
    return wt


def _push_and_open_pr(repo: dict, wt: Path, branch: str, slug: str) -> Optional[int]:
    """Push the branch (own namespace, creation-only → --force is safe over a crashed prior push,
    oracle MINOR) and open a --draft PR. Returns the PR number parsed from `gh pr create`'s URL
    (K6: create prints a URL, not JSON), or None on failure/unknown."""
    push = _git(wt, "push", "--force", "origin", f"HEAD:refs/heads/{branch}")
    if push.returncode != 0:
        log(f"push failed: {push.stderr.strip()[:200]}"); return None
    title = f"skill(auto): {slug} — recurring review finding (review before merge)"
    body = (f"Auto-proposed by the S7 proposer from a recurring `rule:{slug}` finding. This is an "
            f"UNAUTHORED STUB — fill in the real lesson (see the provenance commits in the diff) and "
            f"delete the `{capture.STUB_MARKER}` line before merging. Not indexed until authored.")
    try:
        r = subprocess.run(["gh", "pr", "create", "--repo", repo["nwo"], "--draft",
                            "--base", repo["default_branch"], "--head", branch,
                            "--title", title, "--body", body],
                           capture_output=True, text=True, env=_git_env(), timeout=GH_TIMEOUT, check=False)
    except subprocess.SubprocessError as e:
        log(f"gh pr create raised ({e!r}) — outcome UNKNOWN, leave for recovery"); return None
    if r.returncode != 0:
        log(f"gh pr create failed: {r.stderr.strip()[:200]} — outcome unknown"); return None
    m = _PR_URL_RE.search((r.stdout or "").strip())
    if not m:
        log(f"gh pr create gave no parseable PR URL ({r.stdout.strip()[:120]}) — unknown"); return None
    return int(m.group(1))


def _find_open_bot_pr(repo: dict, branch: str) -> tuple[str, Optional[int]]:
    """K1/K2 recovery: is there already an OPEN PR that WE (the bot) opened for this exact branch in
    OUR repo (not a fork)? Scoped by `--author @me`; validates head is same-repo. TRI-STATE return
    (kilabz MAJOR: a single None conflated lookup-failure/ambiguity/absence, and the caller treated
    all three as permission to create — after a crash-before-mark, a transient lookup failure would
    force-push a fresh stub OVER a human-edited PR branch):
      ("found", n)     — exactly one same-repo bot PR: adopt it.
      ("none", None)   — DEFINITIVE absence (successful empty lookup): creating is safe.
      ("unknown", None)— lookup failed (gh down/rate-limited): DEFER, never create.
      ("ambiguous", None) — multi-match: DEFER for a human, never pick-first, never create."""
    rows = _gh_json(repo["nwo"], "pr", "list", "--head", branch, "--state", "open",
                    "--author", "@me", "--json", "number,isCrossRepository,headRefName")
    if not isinstance(rows, list):
        return ("unknown", None)
    mine = [r for r in rows if isinstance(r, dict) and r.get("isCrossRepository") is False
            and r.get("headRefName") == branch and isinstance(r.get("number"), int)]
    if len(mine) == 1:
        return ("found", mine[0]["number"])
    if len(mine) > 1:
        log(f"ambiguous open bot PRs for {branch} ({[r['number'] for r in mine]}) — defer")
        return ("ambiguous", None)
    return ("none", None)


# =====================================================================================
# the three passes
# =====================================================================================
async def reconcile(led) -> None:
    """proposed → promoted|declined|stale by RE-READING each PR's LIVE state (A5/A7). gh-unknown →
    DEFER. merged always wins. TTL close only for a still-OPEN PR past TTL, and only after the read.
    Per-candidate exception boundary (oracle code-review MAJOR): one bad row must defer, never abort
    the loop and starve the propose() phase behind it."""
    for c in await led.list_proposed():
        try:
            repo = resolve_repo(c["repo_scope"])
            if repo is None:
                log(f"proposed {c['rule_tag']} scope {c['repo_scope']!r} unresolvable — defer"); continue
            pr = _gh_json(repo["nwo"], "pr", "view", str(c["pr_number"]), "--json", "state,mergedAt")
            if not isinstance(pr, dict):
                continue                                          # A5: unknown → defer
            merged = bool(pr.get("mergedAt")) or pr.get("state") == "MERGED"
            outcome = None
            if merged:
                outcome = "promoted"
            elif pr.get("state") == "CLOSED":
                outcome = "declined"
            elif pr.get("state") == "OPEN" and _age_days(c.get("proposed_at")) > TTL_DAYS():
                if DRY_RUN:
                    log(f"would TTL-close+stale PR#{c['pr_number']} ({c['rule_tag']})"); continue
                # the close must be CONFIRMED before the DB flip (oracle BLOCKER: an unchecked
                # failed close + 'stale' write orphans a live OPEN PR the ledger no longer tracks).
                # `gh pr close` prints no JSON, so _gh_close's success = exit 0.
                if not _gh_close(repo["nwo"], c["pr_number"]):
                    log(f"TTL close of PR#{c['pr_number']} failed — defer to next tick"); continue
                outcome = "stale"
            if outcome is None:
                continue
            if DRY_RUN:
                log(f"would resolve {c['rule_tag']} → {outcome} (PR#{c['pr_number']})"); continue
            await led.resolve_capture(c["fingerprint"], outcome)
            log(f"resolved {c['rule_tag']} → {outcome} (PR#{c['pr_number']})")
        except Exception as e:                                    # defer this row, keep the tick alive
            log(f"reconcile {c.get('rule_tag')} raised ({e!r}) — defer + continue")


def _age_days(ts) -> float:
    if ts is None:
        return 0.0
    try:
        import datetime as _dt
        now = _dt.datetime.now(tz=ts.tzinfo) if getattr(ts, "tzinfo", None) else _dt.datetime.now()
        return (now - ts).total_seconds() / 86400.0
    except Exception:
        return 0.0


async def propose(led) -> None:
    """While under MAX_OPEN, take each ready class AT MOST ONCE (K6): resolve repo (A2), render the
    stub (K4 marker), claim (CAS), then adopt an existing bot PR (K1) or create one, then mark.
    `attempts` counts CREATE ATTEMPTS (not confirmed opens): an unknown gh outcome may have opened
    a PR whose response was lost, so the budget must burn on the attempt (kilabz MAJOR: counting
    only confirmed marks allowed 6 create attempts against a 2-per-tick budget)."""
    n_open = await led.count_open_proposals()
    opened = 0
    attempts = 0
    seen: set = set()
    pruned: set = set()
    # limit 100, not MAX_OPEN*2 (kilabz MAJOR: a small fixed window re-fetches the same oldest rows
    # every tick, so a handful of persistently-skipped candidates — unlisted scope, render reject —
    # STARVE every valid candidate behind them forever; visit-once + budgets still bound the tick)
    for c in await led.list_ready_candidates(100):
        if n_open >= MAX_OPEN() or opened >= MAX_PER_TICK or attempts >= MAX_PER_TICK:
            break
        fp = c["fingerprint"]
        if fp in seen:
            continue
        seen.add(fp)                                              # K6: visit-once, no infinite retry
        # the exception boundary wraps the WHOLE candidate body (oracle code-review MAJOR: a raise
        # in provenance/render BEFORE the claim escaped the loop, crashed the tick, and — the row
        # still 'ready' — re-crashed every subsequent tick: a poison-pill halt). claimed tracks
        # whether the except-arm owes a release.
        branch = draft_sha = None
        claimed = False
        try:
            repo = resolve_repo(c["repo_scope"])
            if repo is None:
                log(f"ready {c['rule_tag']} scope {c['repo_scope']!r} not in repo allowlist — skip"); continue
            if repo["path"] not in pruned:                        # clear stale worktree admin entries
                _git(repo["path"], "worktree", "prune"); pruned.add(repo["path"])
            slug = capture.slug(c["rule_tag"])
            if slug is None:
                log(f"ready {c['rule_tag']} → no safe slug — skip"); continue
            prov = await led.capture_provenance(fp)
            rendered = capture.render_skill_md(slug, c["rule_tag"], c["path_glob"] or "src/**",
                                               "", "", finding_ids=prov, origin_repo=c["repo_scope"])
            if rendered is None:
                log(f"ready {c['rule_tag']} → render fail-closed — skip"); continue
            branch = capture.skill_branch(slug)
            draft_sha = capture.draft_hash(rendered)
            if DRY_RUN:
                log(f"would propose {c['rule_tag']} → {branch} for {repo['nwo']} (draft_sha {draft_sha[:8]})")
                n_open += 1; opened += 1; continue                # A9: log BEFORE any DB verb
            if not await led.claim_for_proposing(fp, branch, draft_sha):
                continue                                          # lost the CAS / not ready
            claimed = True
            status, existing = _find_open_bot_pr(repo, branch)    # K1: adopt by identity, not hash
            if status == "found":
                if await led.mark_capture_proposed(fp, branch, draft_sha, existing):
                    log(f"adopted existing PR#{existing} for {branch}"); n_open += 1; opened += 1
                else:
                    await led.release_proposing(fp, branch, draft_sha)
                continue
            if status != "none":                                  # unknown/ambiguous: NEVER create
                log(f"recovery lookup {status} for {branch} — release + defer (no create)")
                await led.release_proposing(fp, branch, draft_sha); continue
            # worktree named by fingerprint, not repo_scope (kilabz MINOR: keep the scope string out
            # of every git argv, including the worktree path — A2's boundary stated fully)
            wt = _make_proposal_commit(repo, f"wt-{fp[:16]}", slug, rendered)
            if wt is None:
                await led.release_proposing(fp, branch, draft_sha); continue
            attempts += 1                                         # burn budget on the ATTEMPT
            try:
                pr_number = _push_and_open_pr(repo, wt, branch, slug)
            finally:
                _git(repo["path"], "worktree", "remove", "--force", str(wt))
            if pr_number is None:
                await led.release_proposing(fp, branch, draft_sha); continue
            if await led.mark_capture_proposed(fp, branch, draft_sha, pr_number):
                log(f"opened draft PR#{pr_number} for {branch} ({repo['nwo']})"); n_open += 1; opened += 1
            else:
                log(f"mark_proposed fenced out (PR#{pr_number}) — closing our PR")
                if not _gh_close(repo["nwo"], pr_number):
                    log(f"close of fenced-out PR#{pr_number} FAILED — orphan open PR, close it by hand")
        except Exception as e:                                    # never let one class wedge the tick
            log(f"propose {c['rule_tag']} raised ({e!r}) — release + continue")
            if claimed and branch is not None:
                try:
                    await led.release_proposing(fp, branch, draft_sha)
                except Exception:
                    pass


async def _amain() -> int:
    if not ENABLED_FLAG.exists():
        log("PROPOSER_ENABLED absent — off; exiting"); return 0
    if not acquire_lock():
        return 0
    led = None
    try:
        gc_worktrees()                                            # A6: tick-start GC (flock ⇒ prior tick dead)
        led = await PostgresLedger.connect(DSN)
        # reap crashed 'proposing' claims back to 'ready' (skip under DRY_RUN — it's a mutation, A9)
        if not DRY_RUN:
            reaped = await led.reap_stuck_proposing(REAP_TIMEOUT_MIN)
            if reaped:
                log(f"reaped {reaped} stuck proposing row(s)")
        await reconcile(led)
        await propose(led)
        return 0
    finally:
        if led is not None:
            try:
                await led.close()
            except Exception:
                pass
        release_lock()


def main(argv: list) -> int:
    if len(argv) < 2 or argv[1] != "tick":
        print("usage: python -m runtime.proposer tick", file=sys.stderr)
        return 2
    # liveness-fire: one unconditional line per fire (mirrors the tick wrappers)
    log("tick fire")
    import asyncio
    return asyncio.run(_amain())


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
