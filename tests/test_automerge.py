"""automerge gate proofs — the SECURITY-critical pure core (classify_diff + denylist)
hammered adversarially, plus the raw-diff parser. The merge/gh/git I/O is integration-
tested by the live DRY-RUN (test.sh); here we prove the un-gameable gate logic.

Run:  PYTHONPATH=src python3 tests/test_automerge.py
"""
import runtime.automerge as A

PASS = [0]
FAIL = [0]


def ok(cond, label):
    if cond:
        PASS[0] += 1
    else:
        FAIL[0] += 1
        print("  FAIL:", label)


def entry(status, omode, nmode, *paths):
    return {"status": status, "omode": omode, "nmode": nmode, "paths": list(paths)}


# -- classify_diff: the security gate truth-table -----------------------------
def test_classify_allows_plain_docs():
    ok(A.classify_diff([entry("M", "100644", "100644", "README.md")])[0], "modify .md allowed")
    ok(A.classify_diff([entry("A", "000000", "100644", "docs/new.md")])[0], "add .md allowed")
    ok(A.classify_diff([entry("D", "100644", "000000", "docs/old.md")])[0], "delete .md allowed")
    ok(A.classify_diff([entry("R100", "100644", "100644", "a.md", "b.md")])[0], "rename .md->.md allowed")
    ok(A.classify_diff([entry("M", "100644", "100644", "a.md"),
                        entry("A", "000000", "100644", "b.md")])[0], "multi .md allowed")


def test_classify_rejects_nondoc_and_modes():
    bad = [
        ("non-.md", [entry("M", "100644", "100644", "src/x.py")]),
        ("empty diff", []),
        ("symlink mode", [entry("A", "000000", "120000", "link.md")]),
        ("gitlink/submodule", [entry("A", "000000", "160000", "sub.md")]),
        ("executable .md", [entry("M", "100644", "100755", "run.md")]),
        ("rename code->md (old side .py)", [entry("R100", "100644", "100644", "evil.py", "evil.md")]),
        ("rename md->code (new side .py)", [entry("R100", "100644", "100644", "a.md", "evil.py")]),
        ("delete with non-doc old", [entry("D", "100755", "000000", "x.md")]),
        ("no extension", [entry("M", "100644", "100644", "Makefile")]),
        ("uppercase .MD (homoglyph/case strict)", [entry("M", "100644", "100644", "READ.MD")]),
        ("trailing dot .md.", [entry("M", "100644", "100644", "x.md.")]),
    ]
    for label, entries in bad:
        ok(not A.classify_diff(entries)[0], f"reject: {label}")


def test_classify_rejects_denylisted_docs():
    deny = ["CLAUDE.md", "AGENTS.md", "GEMINI.md", "dir/CLAUDE.md", ".cursorrules",
            "COPILOT-INSTRUCTIONS.md", "SECURITY.md", "CODEOWNERS",
            ".github/PULL_REQUEST_TEMPLATE.md", ".claude/skills/foo.md", ".codex/x.md",
            "any/rules/policy.md", "pkg/skills/s.md", "x/prompts/p.md", ".agents/a.md",
            "DESIGN.md", "docs/controller-loop-design.md", "docs/x-spec.md", "docs/OPERATING.md"]
    for p in deny:
        ok(not A.classify_diff([entry("M", "100644", "100644", p)])[0], f"deny instruction/ground-truth: {p}")
    # denylist applies to BOTH rename sides (can't rename CLAUDE.md to a benign name)
    ok(not A.classify_diff([entry("R100", "100644", "100644", "CLAUDE.md", "harmless.md")])[0],
       "rename FROM a denylisted file rejected")
    ok(not A.classify_diff([entry("R100", "100644", "100644", "notes.md", "AGENTS.md")])[0],
       "rename TO a denylisted file rejected")
    # a normal doc is allowed (denylist is specific, not blanket)
    ok(A.classify_diff([entry("M", "100644", "100644", "README.md")])[0], "README.md still allowed")
    ok(A.classify_diff([entry("M", "100644", "100644", "docs/notes.md")])[0], "plain docs/notes.md allowed")


# -- parse_raw_z: git diff --raw -z -M parsing --------------------------------
def test_parse_raw_z():
    # modify + add (one path each), NUL-delimited, concatenated
    out = (b":100644 100644 aaa bbb M\x00README.md\x00"
           b":000000 100644 000 ccc A\x00docs/new.md\x00")
    es = A.parse_raw_z(out)
    ok(len(es) == 2, "parsed 2 entries")
    ok(es[0]["status"] == "M" and es[0]["paths"] == ["README.md"], "modify parsed")
    ok(es[1]["status"] == "A" and es[1]["nmode"] == "100644", "add parsed")
    # rename has TWO paths
    out2 = b":100644 100644 aaa aaa R100\x00old.md\x00new.md\x00"
    es2 = A.parse_raw_z(out2)
    ok(len(es2) == 1 and es2[0]["paths"] == ["old.md", "new.md"], "rename two-path parsed")
    # end-to-end: a code rename to .md is parsed AND rejected
    out3 = b":100644 100644 aaa aaa R100\x00evil.py\x00evil.md\x00"
    ok(not A.classify_diff(A.parse_raw_z(out3))[0], "parsed code->md rename rejected")
    ok(A.parse_raw_z(b"") == [], "empty diff -> no entries")


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
    print(f"ALL PASS ({PASS[0]} checks)" if FAIL[0] == 0 else f"FAILED ({FAIL[0]})")
    raise SystemExit(1 if FAIL[0] else 0)


if __name__ == "__main__":
    main()
