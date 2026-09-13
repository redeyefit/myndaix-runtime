"""S7 proposer driver — the safety properties earned by the cross-family review, tested WITHOUT a
real Postgres/gh/git (a FakeLedger records mutations; the gh/git helpers are monkeypatched). The
pure render/slug/stub-marker core is in test_capture.py; the ledger verbs in test_capture_verbs.py.

Run:  PYTHONPATH=src python3 tests/test_proposer.py
"""
import asyncio
import json
import os
import tempfile
from pathlib import Path

import runtime.proposer as P

# Capture the REAL functions BEFORE any test monkeypatches the module globals (bare harness has no
# fixture teardown; tests stub P.resolve_repo/P._git and don't restore them).
_REAL_RESOLVE_REPO = P.resolve_repo
_REAL_GIT = P._git
_REAL_FIND_PR = P._find_open_bot_pr

PASS = [0]
FAIL = [0]


def ok(cond, label):
    if cond:
        PASS[0] += 1
    else:
        FAIL[0] += 1
        print("  FAIL:", label)


class FakeLedger:
    """Records every mutating call so a test can assert DRY_RUN mutates NOTHING (A9) and the happy
    path mutates exactly right. Read methods return seeded data."""
    def __init__(self, ready=None, proposed=None, open_count=0, provenance=None):
        self._ready = ready or []
        self._proposed = proposed or []
        self._open = open_count
        self._prov = provenance or []
        self.mutations = []          # (verb, *args) for every state-changing call
        self.claim_ok = True
        self.mark_ok = True

    # -- reads --
    async def list_ready_candidates(self, limit, after=""):
        rows = sorted(self._ready, key=lambda r: r["fingerprint"])
        return [r for r in rows if r["fingerprint"] > after][:limit]
    async def list_proposed(self): return list(self._proposed)
    async def count_open_proposals(self): return self._open
    async def capture_provenance(self, fp, limit=8): return list(self._prov)

    # -- mutations (recorded) --
    async def claim_for_proposing(self, fp, branch, draft_sha):
        self.mutations.append(("claim", fp, branch)); return self.claim_ok
    async def mark_capture_proposed(self, fp, branch, draft_sha, pr):
        self.mutations.append(("mark", fp, pr)); return self.mark_ok
    async def release_proposing(self, fp, branch, draft_sha):
        self.mutations.append(("release", fp)); return True
    async def resolve_capture(self, fp, outcome):
        self.mutations.append(("resolve", fp, outcome)); return True
    async def reap_stuck_proposing(self, mins):
        self.mutations.append(("reap", mins)); return 0


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _reset(monkey_dry=False):
    P.DRY_RUN = monkey_dry
    # per-test cursor isolation: the fairness cursor is process state; point it at a fresh temp file
    P.CURSOR_FILE = Path(tempfile.mkdtemp(prefix="mdx-test-cursor.")) / "cursor"


# ---- K3: symlink-safe, creation-only SKILL.md write --------------------------------------
def test_safe_write_clean():
    with tempfile.TemporaryDirectory() as wt:
        ok(P._safe_write_skill(Path(wt), "fail-open", "hello"), "clean write into a fresh worktree")
        f = Path(wt) / "skills" / "fail-open" / "SKILL.md"
        ok(f.read_text() == "hello", "content written")


def test_safe_write_refuses_existing_target():
    with tempfile.TemporaryDirectory() as wt:
        d = Path(wt) / "skills" / "fail-open"; d.mkdir(parents=True)
        (d / "SKILL.md").write_text("prior")
        ok(not P._safe_write_skill(Path(wt), "fail-open", "new"),
           "existing target refused (creation-only; never edits an existing skill)")
        ok((d / "SKILL.md").read_text() == "prior", "existing content untouched")


def test_safe_write_refuses_symlinked_skills_dir():
    with tempfile.TemporaryDirectory() as wt, tempfile.TemporaryDirectory() as evil:
        os.symlink(evil, str(Path(wt) / "skills"))          # skills/ is a symlink -> escape vector
        ok(not P._safe_write_skill(Path(wt), "fail-open", "x"),
           "symlinked skills/ ancestor refused (K3: no write escape)")
        ok(not (Path(evil) / "fail-open" / "SKILL.md").exists(), "nothing written through the symlink")


def test_safe_write_refuses_symlinked_slugdir():
    with tempfile.TemporaryDirectory() as wt, tempfile.TemporaryDirectory() as evil:
        (Path(wt) / "skills").mkdir()
        os.symlink(evil, str(Path(wt) / "skills" / "fail-open"))
        ok(not P._safe_write_skill(Path(wt), "fail-open", "x"), "symlinked slug dir refused (K3)")


# ---- K6: gh pr create prints a URL, not JSON — parse the number ------------------------------
def test_pr_url_parse():
    m = P._PR_URL_RE.search("https://github.com/redeyefit/myndaix-runtime/pull/142\n")
    ok(m is not None and m.group(1) == "142", "PR number parsed from the create URL")
    ok(P._PR_URL_RE.search("no url here") is None, "no false match on non-URL output")


# ---- A2: resolve_repo is fail-closed against the exact-key allowlist MAP ---------------------
def test_resolve_repo_fail_closed(monkeypatch=None):
    with tempfile.TemporaryDirectory() as d:
        rj = Path(d) / "repos.json"
        rj.write_text('{"_comment":"x","myndaix-runtime":{"path":"' + d + '"}}')
        P.REPOS_JSON = rj
        # even a listed key needs a resolvable git repo + nwo; stub gh to a known nwo, and make the
        # path look like a git repo
        (Path(d) / ".git").mkdir()
        P._gh_json_path = lambda path, *a: {"nameWithOwner": "redeyefit/myndaix-runtime",
                                            "defaultBranchRef": {"name": "main"}}
        ok(_REAL_RESOLVE_REPO("myndaix-runtime") is not None, "listed repo with a real path resolves")
        ok(_REAL_RESOLVE_REPO("feat-sync-phase1") is None, "UNLISTED scope (branch name) -> None (A2 fail-closed)")
        ok(_REAL_RESOLVE_REPO("wf_80d11feb-cb4-3") is None, "workflow-id scope -> None")
        ok(_REAL_RESOLVE_REPO("_comment") is None, "an underscore/meta key -> None")


# ---- K2/K1: tri-state recovery lookup (found / none / unknown / ambiguous) -------------------
def test_find_open_bot_pr_identity(monkeypatch=None):
    repo = {"nwo": "redeyefit/myndaix-runtime"}
    P._gh_json = lambda nwo, *a: [{"number": 5, "isCrossRepository": False, "headRefName": "skill/auto/fail-open"}]
    ok(_REAL_FIND_PR(repo, "skill/auto/fail-open") == ("found", 5), "single same-repo bot PR -> found")
    P._gh_json = lambda nwo, *a: [{"number": 6, "isCrossRepository": True, "headRefName": "skill/auto/fail-open"}]
    ok(_REAL_FIND_PR(repo, "skill/auto/fail-open") == ("none", None),
       "a FORK PR (cross-repo) is not ours -> definitive absence (K2)")
    P._gh_json = lambda nwo, *a: [{"number": 7, "isCrossRepository": False, "headRefName": "skill/auto/fail-open"},
                                  {"number": 8, "isCrossRepository": False, "headRefName": "skill/auto/fail-open"}]
    ok(_REAL_FIND_PR(repo, "skill/auto/fail-open")[0] == "ambiguous",
       "multi-match -> ambiguous (never first-match)")
    P._gh_json = lambda nwo, *a: None
    ok(_REAL_FIND_PR(repo, "skill/auto/fail-open")[0] == "unknown",
       "gh failure -> unknown (NOT permission to create — kilabz #3)")


def test_unknown_lookup_never_creates():
    # kilabz MAJOR: after a crash-before-mark, a transient lookup failure must NOT force-push a
    # fresh stub over a (possibly human-edited) PR branch — release + defer, never create.
    _reset(False)
    led = FakeLedger(ready=[{"fingerprint": "fp1", "repo_scope": "myndaix-runtime",
                             "rule_tag": "fail-open", "path_glob": "src/*.py", "decline_count": 0}])
    P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
    P._git = lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    P._find_open_bot_pr = lambda repo, branch: ("unknown", None)
    created = {"n": 0}
    def _create(*a, **k):
        created["n"] += 1; return Path("/tmp/wt")
    P._make_proposal_commit = _create
    _run(P.propose(led))
    ok(created["n"] == 0, "unknown lookup -> NO worktree/commit/push (defer)")
    ok(("release", "fp1") in led.mutations, "the claim is released back to ready")


def test_attempt_budget_burns_on_unknown_outcome():
    # kilabz MAJOR: an unknown create outcome may have opened a PR whose response was lost — the
    # per-tick budget must burn on the ATTEMPT, not only on a confirmed mark (else 3 ready
    # candidates x unknown outcomes = 3+ create attempts against a 2-per-tick budget).
    _reset(False)
    ready = [{"fingerprint": f"fp{i}", "repo_scope": "myndaix-runtime", "rule_tag": t,
              "path_glob": "src/*.py", "decline_count": 0}
             for i, t in enumerate(["fail-open", "toctou-race", "missing-scoping"])]
    led = FakeLedger(ready=ready, provenance=["deadbeef"])
    P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
    P._git = lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    P._find_open_bot_pr = lambda repo, branch: ("none", None)
    P._make_proposal_commit = lambda repo, wtn, slug, rendered: Path("/tmp/wt")
    calls = {"n": 0}
    def _unknown_create(repo, wt, branch, slug):
        calls["n"] += 1; return None                              # outcome unknown every time
    P._push_and_open_pr = _unknown_create
    _run(P.propose(led))
    ok(calls["n"] <= P.MAX_PER_TICK,
       f"create ATTEMPTS bounded by MAX_PER_TICK ({calls['n']} <= {P.MAX_PER_TICK})")


def test_resolver_json_argv_is_one_field_list():
    # kilabz BLOCKER unmask: `--json A B` makes B gh's positional REPO argument; the field list
    # must be ONE comma-joined argv element. The old resolver test stubbed the helper and masked it.
    seen = {}
    def _record(path, *args):
        seen["args"] = args
        return {"nameWithOwner": "o/r", "defaultBranchRef": {"name": "main"}}
    saved = P._gh_json_path
    P._gh_json_path = _record
    try:
        with tempfile.TemporaryDirectory() as d:
            rj = Path(d) / "repos.json"
            rj.write_text('{"myndaix-runtime":{"path":"' + d + '"}}')
            P.REPOS_JSON = rj
            (Path(d) / ".git").mkdir()
            _REAL_RESOLVE_REPO("myndaix-runtime")
        args = seen.get("args", ())
        ji = args.index("--json") if "--json" in args else -1
        ok(ji >= 0 and args[ji + 1] == "nameWithOwner,defaultBranchRef" and len(args) == ji + 2,
           f"--json takes ONE comma-joined field list as the FINAL arg (got {args})")
    finally:
        P._gh_json_path = saved


def test_dry_run_makes_zero_git_calls():
    # kilabz r2 #1: the per-candidate prune ran BEFORE the DRY_RUN guard — a dry tick mutated git.
    # Prune now lives in gc_worktrees (guarded). Assert a dry propose makes ZERO _git calls.
    _reset(monkey_dry=True)
    led = FakeLedger(ready=[{"fingerprint": "fp1", "repo_scope": "myndaix-runtime",
                             "rule_tag": "fail-open", "path_glob": "src/*.py", "decline_count": 0}])
    P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
    calls = {"n": 0}
    def _rec_git(*a, **k):
        calls["n"] += 1; return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    P._git = _rec_git
    _run(P.propose(led))
    P.gc_worktrees()                                   # DRY_RUN=True → must be a no-op too
    ok(calls["n"] == 0, f"A9: dry-run makes ZERO git calls incl. prune/GC (got {calls['n']})")
    ok(not P.CURSOR_FILE.exists(), "dry-run does not advance the fairness cursor")
    _reset(False)


def test_cursor_fairness_rotates_past_skipped():
    # kilabz r2 #7 + r3 MINOR: the test must be DISCRIMINATING — >100 skipped rows so the valid
    # candidate sits BEYOND the first batch (reaching it REQUIRES the persisted cursor), and the
    # wrap assertion checks the cursor's VALUE (which a no-op wrap cannot produce). kilabz proved
    # the old version passed with the cursor disabled entirely.
    _reset(False)
    skipped = [{"fingerprint": f"fp{i:03d}", "repo_scope": "unlisted", "rule_tag": "fail-open",
                "path_glob": "src/*.py", "decline_count": 0} for i in range(101)]
    ready = skipped + [{"fingerprint": "fpzz-valid", "repo_scope": "myndaix-runtime",
                        "rule_tag": "toctou-race", "path_glob": "src/*.py", "decline_count": 0}]
    P.resolve_repo = lambda s: (None if s == "unlisted" else
                                {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"})
    P._git = lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    P._find_open_bot_pr = lambda repo, branch: ("none", None)
    P._make_proposal_commit = lambda repo, wtn, slug, rendered: Path("/tmp/wt")
    P._push_and_open_pr = lambda repo, wt, branch, slug: 150
    # tick 1: only the first 100 skips fit the batch — the valid row is NOT reachable this tick
    led = FakeLedger(ready=ready, provenance=["deadbeef"])
    _run(P.propose(led))
    ok(led.mutations == [], "tick 1 visits only the first 100 skips (valid row beyond the batch)")
    ok(P.CURSOR_FILE.read_text() == "fp099", "cursor persisted at the last visited fingerprint")
    # tick 2: the scan RESUMES after fp099 — reaching fpzz-valid REQUIRES the cursor
    led2 = FakeLedger(ready=ready, provenance=["deadbeef"])
    _run(P.propose(led2))
    ok(("mark", "fpzz-valid", 150) in led2.mutations,
       "tick 2 reaches the valid candidate PAST 101 skips (cursor-driven rotation)")
    ok(P.CURSOR_FILE.read_text() == "fpzz-valid", "cursor at the tail after tick 2")
    # tick 3: tail exhausted → WRAP to the start; the cursor VALUE proves rows were re-visited
    led3 = FakeLedger(ready=skipped, provenance=["deadbeef"])
    _run(P.propose(led3))
    ok(led3.mutations == [], "wrapped scan revisits skips without claiming")
    ok(P.CURSOR_FILE.read_text() == "fp099",
       "wrap PROVEN: cursor moved from the tail back to the first batch's last fingerprint")


def test_gc_survives_bad_allowlist_entry():
    # kilabz r3 MAJOR: one malformed repos.json entry (non-string path / raising prune) must not
    # abort the whole tick — the sweep logs + continues, and healthy repos still get pruned.
    _reset(False)
    with tempfile.TemporaryDirectory() as d:
        good = Path(d) / "goodrepo"; (good / ".git").mkdir(parents=True)
        rj = Path(d) / "repos.json"
        rj.write_text(json.dumps({
            "bad-type": {"path": 123},                       # TypeError-shaped
            "bad-missing": {"path": d + "/nope"},
            "good": {"path": str(good)},
        }))
        P.REPOS_JSON = rj
        pruned = []
        def _rec_git(cwd, *a, **k):
            pruned.append(str(cwd)); return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        P._git = _rec_git
        try:
            P.gc_worktrees()
        except Exception as e:
            ok(False, f"gc_worktrees raised through a bad entry: {e!r}")
        else:
            ok(any(str(good) in p for p in pruned), "the healthy repo is still pruned")
        # a RAISING prune on one repo must not stop the sweep either
        def _raise_git(cwd, *a, **k):
            raise RuntimeError("wedged prune")
        P._git = _raise_git
        try:
            P.gc_worktrees()
            ok(True, "a raising prune is caught per-entry (sweep + tick continue)")
        except Exception as e:
            ok(False, f"raising prune escaped gc_worktrees: {e!r}")


def test_git_hooks_disabled_on_every_git_op():
    # kilabz BLOCKER: tracked hooks must never run during worktree/commit/push (they inherit
    # GH_TOKEN + can stage files post-assert + fire the orchestrator pre-push reviewer).
    ok(P._GIT_NOHOOKS == ("-c", "core.hooksPath=/dev/null"), "hook-disable config pair defined")
    r = _REAL_GIT(Path("/tmp"), "version")
    ok(r.returncode == 0, "_git still executes with the no-hooks config injected")


# ---- A9: DRY_RUN mutates NOTHING across propose + reconcile ----------------------------------
def test_dry_run_propose_no_mutation():
    _reset(monkey_dry=True)
    led = FakeLedger(ready=[{"fingerprint": "fp1", "repo_scope": "myndaix-runtime",
                             "rule_tag": "fail-open", "path_glob": "src/*.py", "decline_count": 0}])
    P.resolve_repo = lambda scope: {"path": Path("/tmp/x"), "nwo": "redeyefit/myndaix-runtime",
                                    "default_branch": "main"}
    _run(P.propose(led))
    ok(led.mutations == [], f"DRY_RUN propose opened/claimed NOTHING (got {led.mutations})")
    _reset(False)


def test_dry_run_reconcile_no_mutation():
    _reset(monkey_dry=True)
    led = FakeLedger(proposed=[{"fingerprint": "fp1", "repo_scope": "myndaix-runtime",
                                "rule_tag": "fail-open", "pr_number": 9, "branch": "skill/auto/fail-open",
                                "proposed_at": None}])
    P.resolve_repo = lambda scope: {"nwo": "redeyefit/myndaix-runtime", "path": Path("/tmp/x"),
                                    "default_branch": "main"}
    P._gh_json = lambda nwo, *a: {"state": "MERGED", "mergedAt": "2026-09-13T00:00:00Z"}
    _run(P.reconcile(led))
    ok(led.mutations == [], "DRY_RUN reconcile resolved NOTHING even for a merged PR (A9)")
    _reset(False)


# ---- A5/A7: reconcile acts only on definitive states -----------------------------------------
def _proposed_one():
    return [{"fingerprint": "fp1", "repo_scope": "myndaix-runtime", "rule_tag": "fail-open",
             "pr_number": 9, "branch": "skill/auto/fail-open", "proposed_at": None}]


def test_reconcile_defers_on_gh_unknown():
    _reset(False)
    led = FakeLedger(proposed=_proposed_one())
    P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
    P._gh_json = lambda nwo, *a: None                      # gh down / rate-limited
    _run(P.reconcile(led))
    ok(led.mutations == [], "A5: gh-unknown (None) -> DEFER, never resolve")


def test_reconcile_promotes_and_declines():
    _reset(False)
    P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
    led = FakeLedger(proposed=_proposed_one())
    P._gh_json = lambda nwo, *a: {"state": "MERGED", "mergedAt": "2026-09-13T00:00:00Z"}
    _run(P.reconcile(led))
    ok(("resolve", "fp1", "promoted") in led.mutations, "merged PR -> promoted")
    led = FakeLedger(proposed=_proposed_one())
    P._gh_json = lambda nwo, *a: {"state": "CLOSED", "mergedAt": None}
    _run(P.reconcile(led))
    ok(("resolve", "fp1", "declined") in led.mutations, "closed-unmerged PR -> declined")
    led = FakeLedger(proposed=_proposed_one())
    P._gh_json = lambda nwo, *a: {"state": "OPEN", "mergedAt": None}
    _run(P.reconcile(led))
    ok(led.mutations == [], "an OPEN PR within TTL -> no resolution")


# ---- happy path: propose claims, opens, marks ------------------------------------------------
def test_propose_happy_path_marks_pr():
    _reset(False)
    led = FakeLedger(ready=[{"fingerprint": "fp1", "repo_scope": "myndaix-runtime",
                             "rule_tag": "fail-open", "path_glob": "src/*.py", "decline_count": 0}],
                     provenance=["deadbeef"])
    P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
    P._find_open_bot_pr = lambda repo, branch: ("none", None)   # definitive absence
    P._make_proposal_commit = lambda repo, wtn, slug, rendered: Path("/tmp/wt")
    P._push_and_open_pr = lambda repo, wt, branch, slug: 142
    P._git = lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()  # worktree remove no-op
    _run(P.propose(led))
    verbs = [m[0] for m in led.mutations]
    ok("claim" in verbs, "claimed the ready class")
    ok(("mark", "fp1", 142) in led.mutations, "marked proposed with the opened PR number")


def test_propose_adopts_existing_pr():
    _reset(False)
    led = FakeLedger(ready=[{"fingerprint": "fp1", "repo_scope": "myndaix-runtime",
                             "rule_tag": "fail-open", "path_glob": "src/*.py", "decline_count": 0}])
    P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
    P._find_open_bot_pr = lambda repo, branch: ("found", 77)  # K1: a bot PR already open (crash recovery)
    opened = {"made": False}
    def _boom(*a, **k):
        opened["made"] = True; return Path("/tmp/wt")
    P._make_proposal_commit = _boom
    _run(P.propose(led))
    ok(("mark", "fp1", 77) in led.mutations, "adopted the existing PR (marked with its number)")
    ok(not opened["made"], "K1: did NOT create a second PR when one already exists")


def test_propose_skips_unlisted_scope():
    _reset(False)
    led = FakeLedger(ready=[{"fingerprint": "fp1", "repo_scope": "wf_80d11feb-cb4-3",
                             "rule_tag": "fail-open", "path_glob": "src/*.py", "decline_count": 0}])
    P.resolve_repo = lambda s: None                        # unlisted / polluted scope
    _run(P.propose(led))
    ok(led.mutations == [], "A2: a scope not in the allowlist is skipped before any claim/PR")


# ---- oracle code-review folds ------------------------------------------------------------------
def test_ttl_close_failure_defers_not_stale():
    # oracle BLOCKER: a FAILED `gh pr close` must DEFER — never record 'stale' and orphan a live PR.
    _reset(False)
    import datetime as dt
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
    led = FakeLedger(proposed=[{"fingerprint": "fp1", "repo_scope": "myndaix-runtime",
                                "rule_tag": "fail-open", "pr_number": 9,
                                "branch": "skill/auto/fail-open", "proposed_at": old}])
    P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
    P._gh_json = lambda nwo, *a: {"state": "OPEN", "mergedAt": None}     # over-TTL and still open
    P._gh_close = lambda nwo, pr: False                                   # the close FAILS
    _run(P.reconcile(led))
    ok(led.mutations == [], "failed TTL close -> defer (no 'stale' write, PR not orphaned)")
    P._gh_close = lambda nwo, pr: True                                    # the close succeeds
    _run(P.reconcile(led))
    ok(("resolve", "fp1", "stale") in led.mutations, "confirmed close -> stale recorded")


def test_propose_poison_pill_does_not_wedge_tick():
    # oracle MAJOR: a candidate that RAISES before the claim (provenance/render) must be skipped,
    # not crash the tick — and the NEXT candidate must still be processed.
    _reset(False)
    led = FakeLedger(ready=[{"fingerprint": "bad", "repo_scope": "myndaix-runtime",
                             "rule_tag": "fail-open", "path_glob": "src/*.py", "decline_count": 0},
                            {"fingerprint": "good", "repo_scope": "myndaix-runtime",
                             "rule_tag": "toctou-race", "path_glob": "src/*.py", "decline_count": 0}])
    async def _prov(fp, limit=8):
        if fp == "bad":
            raise RuntimeError("malformed provenance row")
        return ["deadbeef"]
    led.capture_provenance = _prov
    P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
    P._find_open_bot_pr = lambda repo, branch: ("none", None)
    P._make_proposal_commit = lambda repo, wtn, slug, rendered: Path("/tmp/wt")
    P._push_and_open_pr = lambda repo, wt, branch, slug: 143
    P._git = lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    _run(P.propose(led))
    ok(("mark", "good", 143) in led.mutations, "the candidate AFTER the poison pill still proposes")
    ok(not any(m[0] == "release" and m[1] == "bad" for m in led.mutations),
       "a pre-claim raise releases nothing (it never claimed)")


def test_reconcile_poison_pill_does_not_wedge_tick():
    # oracle MAJOR: a raising reconcile row must defer, and the NEXT row must still resolve.
    _reset(False)
    led = FakeLedger(proposed=[{"fingerprint": "bad", "repo_scope": "myndaix-runtime",
                                "rule_tag": "fail-open", "pr_number": 1,
                                "branch": "skill/auto/fail-open", "proposed_at": None},
                               {"fingerprint": "good", "repo_scope": "myndaix-runtime",
                                "rule_tag": "toctou-race", "pr_number": 2,
                                "branch": "skill/auto/toctou-race", "proposed_at": None}])
    async def _resolve(fp, outcome):
        if fp == "bad":
            raise RuntimeError("db hiccup")
        led.mutations.append(("resolve", fp, outcome)); return True
    led.resolve_capture = _resolve
    P.resolve_repo = lambda s: {"nwo": "o/r", "path": Path("/tmp/x"), "default_branch": "main"}
    P._gh_json = lambda nwo, *a: {"state": "MERGED", "mergedAt": "2026-09-13T00:00:00Z"}
    _run(P.reconcile(led))
    ok(("resolve", "good", "promoted") in led.mutations,
       "the row AFTER a raising reconcile row is still resolved (tick not wedged)")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print(f"ALL PASS ({PASS[0]} checks)" if FAIL[0] == 0 else f"FAILED ({FAIL[0]})")
    raise SystemExit(1 if FAIL[0] else 0)


if __name__ == "__main__":
    main()
