"""CLI flag parsing - the --image/--application flags populate Job.context, no DB
needed (cli.submit is stubbed so we assert only what main() threads through).

Run: PYTHONPATH=src python3 tests/test_cli.py
"""
from runtime import cli


def _capture_submit():
    """Replace cli.submit with an async stub that records its kwargs; returns
    (captured_dict, restore_fn)."""
    captured: dict = {}

    async def fake_submit(agent, task, *, context=None, repo_id=None,
                          base_ref=None, timeout_s=180.0):
        captured.update(agent=agent, task=task, context=context,
                        repo_id=repo_id, base_ref=base_ref)
        return 0

    orig = cli.submit
    cli.submit = fake_submit
    return captured, (lambda: setattr(cli, "submit", orig))


def test_cli_image_and_application_build_context():
    captured, restore = _capture_submit()
    try:
        rc = cli.main(["higgsfield", "gentle push-in",
                       "--image", "http://example.com/cat.png",
                       "--application", "/higgsfield-ai/dop/lite"])
        assert rc == 0
        assert captured["agent"] == "higgsfield" and captured["task"] == "gentle push-in"
        assert captured["context"] == {"image_url": "http://example.com/cat.png",
                                       "application": "/higgsfield-ai/dop/lite"}
    finally:
        restore()


def test_cli_no_media_flags_gives_empty_context():
    captured, restore = _capture_submit()
    try:
        rc = cli.main(["recon", "what is rust"])
        assert rc == 0 and captured["context"] == {}      # empty, not None
    finally:
        restore()


def test_cli_image_only_omits_application_key():
    captured, restore = _capture_submit()
    try:
        rc = cli.main(["higgsfield", "spin", "--image", "http://example.com/a.png"])
        assert rc == 0 and captured["context"] == {"image_url": "http://example.com/a.png"}
    finally:
        restore()


def test_cli_repo_and_base_ref_thread_through():
    captured, restore = _capture_submit()
    try:
        rc = cli.main(["kilabz", "review this", "--repo", "fieldvision",
                       "--base-ref", "abc123"])
        assert rc == 0
        assert captured["repo_id"] == "fieldvision" and captured["base_ref"] == "abc123"
    finally:
        restore()


def test_cli_no_scope_flags_are_none():
    # omitted -> None -> NULL repo_id -> cap-exempt (never a shared bucket)
    captured, restore = _capture_submit()
    try:
        rc = cli.main(["recon", "what is rust"])
        assert rc == 0 and captured["repo_id"] is None and captured["base_ref"] is None
    finally:
        restore()


def test_cli_get_routes_to_get_job():
    # `mxr get <id>` is special-cased above the flat submit parser (D1 artifact read)
    captured = {}

    async def fake_get(job_id):
        captured["job_id"] = job_id
        return 0

    orig = cli.get_job
    cli.get_job = fake_get
    try:
        rc = cli.main(["get", "11111111-2222-3333-4444-555555555555"])
        assert rc == 0
        assert captured["job_id"] == "11111111-2222-3333-4444-555555555555"
    finally:
        cli.get_job = orig


def test_cli_get_rejects_non_uuid_before_db():
    # a malformed id fails closed (rc 2) at parse time — never touches the ledger
    import asyncio
    assert asyncio.run(cli.get_job("not-a-uuid")) == 2


# ---- `mxr get` short-id prefix resolver (PR-3 quick win 1) -----------------

class _FakeLedger:
    """Stands in for PostgresLedger past the parse gate: resolve_job_prefix over a
    fixed id list (same hyphen-stripped LIKE semantics as the SQL), get_status a
    canned dict."""
    def __init__(self, ids):
        self.ids = ids
        self.prefix_seen = None
        self.status_asked = None

    async def resolve_job_prefix(self, prefix):
        self.prefix_seen = prefix
        return [i for i in self.ids if i.replace("-", "").startswith(prefix)]

    async def get_status(self, jid):
        self.status_asked = str(jid)
        return {"id": str(jid), "status": "done", "to_agent": "recon"}

    async def close(self):
        pass


def _patch_ledger(fake):
    class _FakePL:
        @staticmethod
        async def connect(dsn):
            return fake
    orig = cli.PostgresLedger
    cli.PostgresLedger = _FakePL
    return lambda: setattr(cli, "PostgresLedger", orig)


def test_cli_get_short_prefix_too_short_rejected_before_db():
    # <8 hex chars fails closed at parse time — the ledger is never dialed
    import asyncio

    class _Boom:
        @staticmethod
        async def connect(dsn):
            raise AssertionError("must not touch the ledger for a too-short prefix")
    orig = cli.PostgresLedger
    cli.PostgresLedger = _Boom
    try:
        assert asyncio.run(cli.get_job("abc123")) == 2       # 6 hex chars
        assert asyncio.run(cli.get_job("abcd-12")) == 2      # 6 after hyphen strip
    finally:
        cli.PostgresLedger = orig


def test_cli_get_unique_prefix_resolves():
    import asyncio
    full = "deadbeef-0000-4000-8000-000000000001"
    fake = _FakeLedger([full, "11111111-2222-3333-4444-555555555555"])
    restore = _patch_ledger(fake)
    try:
        assert asyncio.run(cli.get_job("deadbeef")) == 0
        assert fake.prefix_seen == "deadbeef"
        assert fake.status_asked == full                     # resolved to the FULL id
    finally:
        restore()


def test_cli_get_prefix_hyphens_and_case_stripped():
    # a hyphen-spanning, mixed-case slice of the full JOB_ID works (12 hex after strip)
    import asyncio
    full = "deadbeef-0000-4000-8000-000000000001"
    fake = _FakeLedger([full])
    restore = _patch_ledger(fake)
    try:
        assert asyncio.run(cli.get_job("DEAD-beef-0000")) == 0
        assert fake.prefix_seen == "deadbeef0000"
        assert fake.status_asked == full
    finally:
        restore()


def test_cli_get_ambiguous_prefix_fails_closed():
    import asyncio
    fake = _FakeLedger(["deadbeef-0000-4000-8000-000000000001",
                        "deadbeef-1111-4111-8111-000000000002"])
    restore = _patch_ledger(fake)
    try:
        assert asyncio.run(cli.get_job("deadbeef")) == 2     # refuse, don't guess
        assert fake.status_asked is None                     # no status read happened
    finally:
        restore()


def test_cli_get_prefix_no_match():
    import asyncio
    fake = _FakeLedger(["deadbeef-0000-4000-8000-000000000001"])
    restore = _patch_ledger(fake)
    try:
        assert asyncio.run(cli.get_job("abcd1234")) == 1     # same rc as unknown full id
    finally:
        restore()


def test_cli_get_full_uuid_skips_resolver():
    import asyncio
    full = "11111111-2222-3333-4444-555555555555"
    fake = _FakeLedger([full])
    restore = _patch_ledger(fake)
    try:
        assert asyncio.run(cli.get_job(full)) == 0
        assert fake.prefix_seen is None                      # resolver never consulted
        assert fake.status_asked == full
    finally:
        restore()


# ---- sync-wait derivation (PR-3 quick win 2) --------------------------------

def _with_env(key, value, fn):
    import os
    had, orig = key in os.environ, os.environ.get(key)
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value
    try:
        return fn()
    finally:
        if had:
            os.environ[key] = orig
        else:
            os.environ.pop(key, None)


def test_profile_sync_wait_derived_from_timeout():
    from runtime.contracts import Profile
    assert Profile().sync_wait() == 360.0                    # 300 default + 60 margin
    assert Profile(timeout_s=900).sync_wait() == 960.0       # kilabz: scales, not flat 180
    assert Profile(timeout_s=900, sync_wait_s=42).sync_wait() == 42.0   # explicit pin wins


def test_resolve_sync_wait_env_always_wins():
    assert _with_env("MXR_TIMEOUT_S", "77", lambda: cli._resolve_sync_wait("kilabz")) == 77.0


def test_resolve_sync_wait_unset_env_uses_profile():
    # kilabz profile timeout_s=900 -> 960; the 180s flat default that stranded a DONE
    # reply behind a 900s exec cap is gone for registry agents
    assert _with_env("MXR_TIMEOUT_S", None, lambda: cli._resolve_sync_wait("kilabz")) == 960.0
    assert _with_env("MXR_TIMEOUT_S", None, lambda: cli._resolve_sync_wait("recon")) >= 180.0


def test_resolve_sync_wait_no_profile_falls_back_180():
    # defensive: an agent missing from the registry (submit rejects it earlier anyway)
    assert _with_env("MXR_TIMEOUT_S", None,
                     lambda: cli._resolve_sync_wait("no-such-agent")) == 180.0


def test_resolve_sync_wait_malformed_env_falls_to_profile():
    # a broken export must not crash NOR silently pin 180 under a 900s exec cap
    assert _with_env("MXR_TIMEOUT_S", "not-a-number",
                     lambda: cli._resolve_sync_wait("kilabz")) == 960.0


if __name__ == "__main__":
    passed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print("PASS", _name)
            passed += 1
    print(f"ALL PASS ({passed})")
