"""S7 proposer driver — the safety properties earned by the cross-family review, tested WITHOUT a
real Postgres/gh/git (a FakeLedger records mutations; the gh/git helpers are monkeypatched). The
pure render/slug/stub-marker core is in test_capture.py; the ledger verbs in test_capture_verbs.py.

Run:  PYTHONPATH=src python3 tests/test_proposer.py
"""
import asyncio
import os
import tempfile
from pathlib import Path

import runtime.proposer as P

# Capture the REAL resolve_repo BEFORE any test monkeypatches the module global (bare harness has no
# fixture teardown; other tests stub P.resolve_repo and don't restore it — this test needs the real one).
_REAL_RESOLVE_REPO = P.resolve_repo

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
    async def list_ready_candidates(self, limit): return list(self._ready)[:limit]
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


# ---- K2: adopt only OUR single open PR; reject fork/ambiguous --------------------------------
def test_find_open_bot_pr_identity(monkeypatch=None):
    repo = {"nwo": "redeyefit/myndaix-runtime"}
    P._gh_json = lambda nwo, *a: [{"number": 5, "isCrossRepository": False, "headRefName": "skill/auto/fail-open"}]
    ok(P._find_open_bot_pr(repo, "skill/auto/fail-open") == 5, "single same-repo bot PR adopted")
    P._gh_json = lambda nwo, *a: [{"number": 6, "isCrossRepository": True, "headRefName": "skill/auto/fail-open"}]
    ok(P._find_open_bot_pr(repo, "skill/auto/fail-open") is None, "a FORK PR (cross-repo) is not adopted (K2)")
    P._gh_json = lambda nwo, *a: [{"number": 7, "isCrossRepository": False, "headRefName": "skill/auto/fail-open"},
                                  {"number": 8, "isCrossRepository": False, "headRefName": "skill/auto/fail-open"}]
    ok(P._find_open_bot_pr(repo, "skill/auto/fail-open") is None, "ambiguous multi-match -> None (never first-match)")
    P._gh_json = lambda nwo, *a: None
    ok(P._find_open_bot_pr(repo, "skill/auto/fail-open") is None, "gh failure -> None")


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
    P._find_open_bot_pr = lambda repo, branch: None        # no existing PR
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
    P._find_open_bot_pr = lambda repo, branch: 77          # K1: a bot PR already open (crash recovery)
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


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print(f"ALL PASS ({PASS[0]} checks)" if FAIL[0] == 0 else f"FAILED ({FAIL[0]})")
    raise SystemExit(1 if FAIL[0] else 0)


if __name__ == "__main__":
    main()
