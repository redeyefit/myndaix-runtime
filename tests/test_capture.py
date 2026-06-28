"""auto-capture pure core — path->glob normalization + the deterministic recurrence fingerprint.
DB-free, adversarial, like test_skillselect.py.

Run:  PYTHONPATH=src python3 tests/test_capture.py
"""
import runtime.capture as C
import runtime.skillmatch as M

PASS = [0]
FAIL = [0]


def ok(cond, label):
    if cond:
        PASS[0] += 1
    else:
        FAIL[0] += 1
        print("  FAIL:", label)


def test_path_to_glob_generalizes_basename():
    ok(C.path_to_glob("src/runtime/ledger/migrations/0099_x.sql") == "src/runtime/ledger/migrations/*.sql",
       "migration path -> dir/*.sql")
    ok(C.path_to_glob("FieldVision/Views/Logs/NativeCameraView.swift") == "FieldVision/Views/Logs/*.swift",
       "nested swift path -> dir/*.swift")
    ok(C.path_to_glob("README.md") == "*.md", "top-level file -> *.ext")
    ok(C.path_to_glob("Makefile") == "Makefile", "extensionless basename kept literal")
    ok(C.path_to_glob(".gitignore") == ".gitignore", "leading-dot dotfile kept literal (no false ext)")
    ok(C.path_to_glob("a/b.c.d.py") == "a/*.py", "only the LAST extension generalized")
    ok(C.path_to_glob("") == "" and C.path_to_glob("///") == "", "empty/degenerate -> empty")


def test_glob_round_trips_through_seg_match():
    # the produced glob MUST actually match the file it came from (else a proposed skill never fires)
    for p in ["src/runtime/ledger/migrations/0099_x.sql", "a/b/c.swift", "top.md"]:
        g = C.path_to_glob(p)
        ok(M.seg_match(g, p), f"glob {g!r} seg-matches its origin {p!r}")
        ok(not M.is_banned_trigger(g), f"produced glob {g!r} is a usable (non-banned) trigger")


def test_candidate_glob_drops_unusable():
    ok(C.candidate_glob("src/runtime/migrations/0099.sql") == "src/runtime/migrations/*.sql",
       "usable path -> its glob")
    ok(C.candidate_glob("") is None, "empty path -> None (fail-closed)")
    ok(C.candidate_glob("   ") is None, "whitespace path -> None")
    # whatever it returns is ALWAYS a non-banned trigger (never proposes a skill lint would reject)
    for p in ["x.py", "a/b/c.sql", "Dockerfile", "deep/nested/dir/file.ts"]:
        g = C.candidate_glob(p)
        ok(g is None or not M.is_banned_trigger(g), f"candidate_glob({p!r}) is None or non-banned")


def test_fingerprint_is_deterministic_and_scoped():
    f1 = C.fingerprint("repoA", "src/*.py")
    ok(f1 == C.fingerprint("repoA", "src/*.py"), "same (repo, glob) -> same fingerprint")
    ok(f1 != C.fingerprint("repoB", "src/*.py"), "different repo -> different fingerprint")
    ok(f1 != C.fingerprint("repoA", "lib/*.py"), "different glob -> different fingerprint")
    # NUL-separation prevents a boundary collision: ('a','b/c') vs ('a/b','c')
    ok(C.fingerprint("a", "b/c") != C.fingerprint("a/b", "c"), "no repo/glob boundary collision")
    ok(len(f1) == 64, "fingerprint is a sha256 hex digest")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print(f"ALL PASS ({PASS[0]} checks)" if FAIL[0] == 0 else f"FAILED ({FAIL[0]})")
    raise SystemExit(1 if FAIL[0] else 0)


if __name__ == "__main__":
    main()
