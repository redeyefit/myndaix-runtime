"""CLI flag parsing - the --image/--application flags populate Job.context, no DB
needed (cli.submit is stubbed so we assert only what main() threads through).

Run: PYTHONPATH=src python3 tests/test_cli.py
"""
from runtime import cli


def _capture_submit():
    """Replace cli.submit with an async stub that records its kwargs; returns
    (captured_dict, restore_fn)."""
    captured: dict = {}

    async def fake_submit(agent, task, *, context=None, timeout_s=180.0):
        captured.update(agent=agent, task=task, context=context)
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


if __name__ == "__main__":
    passed = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
            print("PASS", _name)
            passed += 1
    print(f"ALL PASS ({passed})")
