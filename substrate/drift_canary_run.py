"""Serialize and bound a canary tick using macOS-compatible stdlib primitives."""

import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys


def main():
    state = Path(os.environ.get("MYNDAIX_HOME") or Path.home() / ".myndaix") / "state"
    state.mkdir(parents=True, exist_ok=True)
    # Never unlink: every invocation must lock the same inode. The child inherits
    # the lock too, so killing only the supervisor cannot admit a concurrent tick.
    with (state / "drift-canary.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("ALARM: canary already running; retry after it finishes", flush=True)
            return 1

        child = subprocess.Popen(
            ["/bin/bash", sys.argv[1], "--supervised"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, pass_fds=(lock.fileno(),),
        )

        def interrupted(signum, frame):
            raise SystemExit(128 + signum)

        signal.signal(signal.SIGTERM, interrupted)
        signal.signal(signal.SIGINT, interrupted)
        try:
            try:
                output, errors = child.communicate(timeout=300)
            except subprocess.TimeoutExpired:
                print("ALARM: canary timed out after 300s", flush=True)
                return 124
            sys.stdout.buffer.write(output)
            sys.stdout.buffer.flush()
            sys.stderr.buffer.write(errors)
            sys.stderr.buffer.flush()
            if b"canary: no drift" not in output and b"DRIFT" not in output:
                print("ALARM: canary did not report a healthy verdict", flush=True)
                return child.returncode or 1
            return child.returncode
        finally:
            # Kill the entire session's process group, including fetch/SSH children
            # that could otherwise keep command-substitution output pipes open.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()


if __name__ == "__main__":
    sys.exit(main())
