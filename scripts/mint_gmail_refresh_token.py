"""mint_gmail_refresh_token — one-time interactive OAuth mint for an Inbox Assistant account.

Run on the MacBook (needs a browser); the Mini consumes the token headless via 1Password.
Builds the client config in-memory and drives google_auth_oauthlib's local-server flow —
NOTHING is written to disk: no client_secret.json, no token cache. On success the account
and refresh token are printed to stdout (two lines, clearly prefixed); store the token at
op://<vault>/gmail-rt-<email>/refresh_token yourself (op CLI or the 1Password app).

Scopes are FROZEN at mint (gmail.readonly + gmail.labels + gmail.compose + drive.file).
Changing scopes later means re-minting. gmail.compose permits send, but the runtime is
drafts-only by code contract — no send method is ever invoked.

Usage:
    # secret piped/pasted on stdin (never argv — argv is visible in `ps`)
    op read 'op://Automation/gmail-oauth-client/client_secret' | \
        python3 scripts/mint_gmail_refresh_token.py --client-id X --client-secret-stdin \
        --account someone@x.com
    # or let the tool read the vault itself (needs `op` signed in)
    python3 scripts/mint_gmail_refresh_token.py --client-id X --op-vault Automation \
        --account someone@x.com --check
"""
from __future__ import annotations

import argparse
import subprocess
import sys

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/drive.file",
]


def _read_client_secret(args: argparse.Namespace) -> str:
    """Secret from `op read` (if --op-vault) or stdin. Never argv, never echoed, never logged."""
    if args.op_vault:
        ref = f"op://{args.op_vault}/gmail-oauth-client/client_secret"
        proc = subprocess.run(["op", "read", ref], capture_output=True, text=True)
        if proc.returncode != 0:
            print(f"FAIL: `op read {ref}` exited {proc.returncode} — is `op` signed in? "
                  f"{proc.stderr.strip()}", file=sys.stderr)
            sys.exit(2)
        secret = proc.stdout.strip()
    else:
        if sys.stdin.isatty():
            print("Paste the OAuth client secret, then Ctrl-D:", file=sys.stderr)
        secret = sys.stdin.read().strip()
    if not secret:
        print("FAIL: empty client secret", file=sys.stderr)
        sys.exit(2)
    return secret


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Mint a Gmail refresh token for one Inbox Assistant account "
                    "(interactive; run where a browser lives).")
    ap.add_argument("--client-id", required=True,
                    help="OAuth client id (not secret; argv is fine for this one)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--client-secret-stdin", action="store_true",
                     help="read the client secret from stdin (pipe, or paste + Ctrl-D)")
    src.add_argument("--op-vault", metavar="VAULT",
                     help="read the client secret from op://VAULT/gmail-oauth-client/client_secret")
    ap.add_argument("--account", required=True,
                    help="the Gmail address this token is minted for (authorize as THIS account)")
    ap.add_argument("--check", action="store_true",
                    help="after minting, verify the token can list labels before printing it")
    args = ap.parse_args()

    if "@" not in args.account:
        ap.error(f"--account {args.account!r} does not look like an email address")

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("FAIL: google-auth-oauthlib not installed — "
              "pip install google-auth-oauthlib google-api-python-client", file=sys.stderr)
        return 2

    client_secret = _read_client_secret(args)

    # In-memory client config — NEVER a client_secret.json on disk.
    client_config = {
        "installed": {
            "client_id": args.client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
    print(f"Opening browser — authorize as {args.account} (watch the account picker: the wrong "
          f"account mints a token for the wrong inbox).", file=sys.stderr)
    # port=0 grabs a free ephemeral port. access_type=offline + prompt=consent forces Google
    # to issue a refresh token even if this client was consented before (else it comes back None).
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

    if not creds.refresh_token:
        print("FAIL: Google returned no refresh token (re-run; prompt=consent should force one)",
              file=sys.stderr)
        return 1

    if args.check:
        try:
            from googleapiclient.discovery import build
        except ImportError:
            print("FAIL: --check needs google-api-python-client — "
                  "pip install google-api-python-client", file=sys.stderr)
            return 2
        try:
            gmail = build("gmail", "v1", credentials=creds)
            labels = gmail.users().labels().list(userId="me").execute().get("labels", [])
        except Exception as exc:  # HttpError et al — message is safe, credentials are not in it
            print(f"CHECK FAIL: labels.list — {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        print(f"CHECK OK: token lists {len(labels)} labels for {args.account}", file=sys.stderr)

    # Exactly two stdout lines — pipe-friendly. Everything else goes to stderr.
    print(f"account: {args.account}")
    print(f"refresh_token: {creds.refresh_token}")
    vault = args.op_vault or "<vault>"
    print(f"REMINDER: store it at op://{vault}/gmail-rt-{args.account}/refresh_token — "
          f"scopes are frozen at mint (re-mint to change them).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
