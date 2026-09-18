"""Unit tests for the observe-only proposer READY notifier (runtime.proposersignal).

No DB, no SMTP: fetch_ready + send are monkeypatched and SEEN_PATH points at a temp file, so
this runs anywhere in ~10ms. Proves the dedup contract — a newly-ready class emails ONCE, a
repeat stays silent, only genuinely-new classes email, a FAILED send retries next tick, and a
class that leaves then returns to 'ready' re-notifies.

Run: PYTHONPATH=src python3 tests/test_proposer_signal.py
"""
import tempfile
from pathlib import Path

from runtime import proposersignal as ps


def _row(fp, tag="toctou-race", scope="myndaix-runtime", glob="*.py"):
    return {"fingerprint": fp, "rule_tag": tag, "repo_scope": scope,
            "path_glob": glob, "decline_count": 0}


def _tmp_seen():
    return Path(tempfile.mkdtemp(prefix="propsig-")) / "seen"


class _Harness:
    """Drives ps.main() with a controllable ready-set and a recording sender. Patches the two
    edges (fetch_ready, send) + SEEN_PATH on the module; restore() puts them back."""

    def __init__(self):
        self.rows = []
        self.sends = []          # (subject, body) per successful-or-attempted send
        self.send_ok = True
        self._orig = (ps.fetch_ready, ps.send, ps.SEEN_PATH)
        ps.SEEN_PATH = _tmp_seen()

        async def fake_fetch(dsn):
            return list(self.rows)

        def fake_send(subject, body):
            self.sends.append((subject, body))
            return self.send_ok

        ps.fetch_ready = fake_fetch
        ps.send = fake_send

    def tick(self):
        return ps.main(["proposersignal", "tick"])

    def restore(self):
        ps.fetch_ready, ps.send, ps.SEEN_PATH = self._orig


def test_new_class_notifies_once_then_dedups():
    h = _Harness()
    try:
        h.rows = [_row("fpA")]
        assert h.tick() == 0
        assert len(h.sends) == 1                       # emailed once
        assert "toctou-race" in h.sends[0][0]          # subject names the tag
        h.tick()                                        # identical ready-set
        assert len(h.sends) == 1                       # NOT re-emailed
    finally:
        h.restore()


def test_only_the_new_class_is_emailed():
    h = _Harness()
    try:
        h.rows = [_row("fpA", tag="toctou-race")]
        h.tick()
        assert len(h.sends) == 1
        h.rows = [_row("fpA", tag="toctou-race"),
                  _row("fpB", tag="missing-scoping", scope="FieldVision")]
        h.tick()
        assert len(h.sends) == 2                        # exactly one more send
        subj, body = h.sends[1]
        assert "missing-scoping" in body               # the new class is in it
        assert "toctou-race" not in subj               # the already-seen one is NOT
    finally:
        h.restore()


def test_failed_send_retries_next_tick():
    h = _Harness()
    try:
        h.rows = [_row("fpA")]
        h.send_ok = False
        h.tick()
        assert len(h.sends) == 1                        # attempted
        h.tick()
        assert len(h.sends) == 2                        # NOT suppressed — retried
        h.send_ok = True
        h.tick()
        assert len(h.sends) == 3                        # succeeds now
        h.tick()
        assert len(h.sends) == 3                        # thereafter suppressed
    finally:
        h.restore()


def test_left_ready_pruned_then_renotifies_on_return():
    h = _Harness()
    try:
        h.rows = [_row("fpA")]
        h.tick()
        assert len(h.sends) == 1
        h.rows = []                                     # class left 'ready'
        h.tick()
        assert len(h.sends) == 1                        # nothing to send
        h.rows = [_row("fpA")]                          # recurred -> re-ready
        h.tick()
        assert len(h.sends) == 2                        # re-notified (recurrence is news)
    finally:
        h.restore()


def test_next_seen_pure():
    # keep still-ready previously-seen; add new ONLY if emailed; drop left-ready
    assert ps.next_seen({"a", "b"}, {"a", "c"}, {"c"}, True) == {"a", "c"}
    assert ps.next_seen({"a", "b"}, {"a", "c"}, {"c"}, False) == {"a"}
    assert ps.next_seen(set(), {"x"}, {"x"}, True) == {"x"}


def test_seen_file_roundtrip_and_missing_is_empty():
    p = _tmp_seen()
    ps.commit_seen(p, {"f1", "f2"})
    assert ps.load_seen(p) == {"f1", "f2"}
    ps.commit_seen(p, set())                            # empty rewrite is valid
    assert ps.load_seen(p) == set()
    assert ps.load_seen(_tmp_seen()) == set()           # missing file = empty


if __name__ == "__main__":
    passed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print("PASS", _name)
            passed += 1
    print(f"ALL PASS ({passed})")
