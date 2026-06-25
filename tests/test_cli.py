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


if __name__ == "__main__":
    passed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print("PASS", _name)
            passed += 1
    print(f"ALL PASS ({passed})")
