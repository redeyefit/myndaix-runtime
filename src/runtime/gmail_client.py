"""Gmail/Drive client for the Inbox Assistant — one instance per account.

Mints access tokens from a vault-held refresh token (google-auth auto-refreshes on the first
API call), pulls threads incrementally via the History API with a bounded messages.list
backfill when the cursor expires, applies labels, creates threaded reply drafts, and mirrors
the daily brief to Drive. Design: docs/inbox-assistant-design.md.

DRAFTS-ONLY CONTRACT (v1, load-bearing): the gmail.compose scope technically permits
sending, but no sending method is invoked or referenced anywhere in this module — grep it.
Sending is earned later, tap-approve first, per the design's autonomy ladder. Reviewers: any
diff introducing an outbound-mail call here is a contract violation, not a feature.

Error taxonomy the tick depends on (keep these paths distinct):
  * google.auth RefreshError -> GmailAuthError  — token revoked/expired (the password-change
    path); fail CLOSED for this account only, surface "needs re-auth", never retry as
    transient.
  * HTTP 404 from users.history.list ONLY -> CursorExpiredError — Google aged out the
    historyId; the tick falls back to pull_bounded_backfill. A 400 is NEVER this: that is a
    malformed request (a code bug) and must surface loudly, not silently trigger a rescan.
  * HTTP 429 -> exponential-backoff retry here (google-api-python-client does NOT retry rate
    limits for you), then raise.

Synchronous by design: the tick is a once-daily sequential batch; async buys nothing here.
"""
from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass
from email.message import EmailMessage

from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaInMemoryUpload

__all__ = [
    "GmailAuthError", "CursorExpiredError", "ThreadSummary", "PullResult", "GmailClient",
]

# Strictly minimal scopes (design §5): readonly + labels + compose + drive.file. NOT
# gmail.modify (permits trash/archive/mark-read) and NOT mail.google.com (full delete).
_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/drive.file",
]
_TOKEN_URI = "https://oauth2.googleapis.com/token"
_BRIEF_FOLDER = "Inbox Assistant Briefs"   # drive.file scope: we only ever see what we created
_BATCH_MODIFY_CAP = 1000                   # messages.batchModify hard limit per request
_MAX_TRIES = 5                             # 429 backoff: 1,2,4,8s then raise

# ALL C0 (incl. \t \n \r) + DEL. Email headers/snippets are hostile data headed for a prompt
# and a brief — control chars are stripped at ingest, before anything downstream sees them.
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


class GmailAuthError(Exception):
    """Refresh token invalid/revoked (google.auth RefreshError). Carries the account email."""


class CursorExpiredError(Exception):
    """Stored historyId aged out — raised ONLY on HTTP 404 from users.history.list."""


@dataclass
class ThreadSummary:
    """One changed thread, reduced to its NEWEST message's triage metadata (design §4:
    snippet-first — full bodies are fetched only when a draft needs them, never here)."""
    account: str
    thread_id: str
    last_message_id: str
    sender: str
    subject: str
    date: str
    snippet: str
    label_ids: list[str]


@dataclass
class PullResult:
    """Threads changed since the cursor + the new cursor to persist AFTER processing succeeds
    (the ledger advances it only on success — design §3 step 9)."""
    threads: list[ThreadSummary]
    new_history_id: str


def _clean(text: str | None) -> str:
    return _CTRL_RE.sub("", text or "")


def _header_map(message: dict) -> dict[str, str]:
    """payload.headers keyed by LOWERCASED name. Gmail preserves the sender MUA's casing —
    'Message-ID' vs 'Message-Id' both occur in the wild — so every lookup is case-insensitive."""
    return {h["name"].lower(): h["value"]
            for h in (message.get("payload") or {}).get("headers") or []}


def _status(err: HttpError) -> int:
    return getattr(err.resp, "status", 0) or 0


class GmailClient:
    def __init__(self, account: str, client_id: str, client_secret: str, refresh_token: str):
        self.account = account
        # token=None + refresh_token: the first execute() mints an access token; a revoked
        # token surfaces there as RefreshError and _execute maps it to GmailAuthError.
        self._creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri=_TOKEN_URI,
            client_id=client_id,
            client_secret=client_secret,
            scopes=_SCOPES,
        )
        self._gmail_svc = None
        self._drive_svc = None
        self._labels: dict[str, str] | None = None   # lowercased name -> label id, per instance
        self._folder_id: str | None = None

    # ---- services (lazy: most runs never touch Drive) -----------------------------------------

    @property
    def _gmail(self):
        if self._gmail_svc is None:
            self._gmail_svc = build("gmail", "v1", credentials=self._creds,
                                    cache_discovery=False)
        return self._gmail_svc

    @property
    def _drive(self):
        if self._drive_svc is None:
            self._drive_svc = build("drive", "v3", credentials=self._creds,
                                    cache_discovery=False)
        return self._drive_svc

    # ---- shared error policy -------------------------------------------------------------------

    def _execute(self, request):
        """Run one API request under the module's error taxonomy: RefreshError ->
        GmailAuthError; 429 -> exponential backoff then raise; everything else propagates."""
        delay = 1.0
        for attempt in range(_MAX_TRIES):
            try:
                return request.execute()
            except RefreshError as e:
                raise GmailAuthError(self.account) from e
            except HttpError as e:
                if _status(e) == 429 and attempt < _MAX_TRIES - 1:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise

    # ---- pull ------------------------------------------------------------------------------------

    def pull_since_history(self, start_history_id: str) -> PullResult:
        """Incremental pull: every thread changed since `start_history_id`. new_history_id is
        the historyId from the LAST history.list page (an empty response with no 'history' key
        is normal — quiet mailbox — and still carries historyId)."""
        thread_ids: dict[str, None] = {}   # insertion-ordered dedupe within the run
        new_history_id = str(start_history_id)
        page_token = None
        while True:
            req = self._gmail.users().history().list(
                userId="me", startHistoryId=start_history_id, pageToken=page_token)
            try:
                resp = self._execute(req)
            except HttpError as e:
                if _status(e) == 404:   # cursor aged out — the tick backfills to re-establish
                    raise CursorExpiredError(self.account) from e
                raise                   # 400 = malformed startHistoryId = code bug, never expiry
            new_history_id = str(resp.get("historyId", new_history_id))
            for record in resp.get("history") or []:
                for msg in record.get("messages") or []:
                    if msg.get("threadId"):
                        thread_ids.setdefault(msg["threadId"], None)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return PullResult(threads=self._thread_summaries(list(thread_ids)),
                          new_history_id=new_history_id)

    def pull_bounded_backfill(self, days: int) -> PullResult:
        """First run / expired cursor: bounded time-query rescan. CHECKPOINT-BEFORE-SCAN:
        getProfile FIRST so mail arriving mid-scan lands after the checkpoint and is re-seen
        by the next incremental pull. Overlap is safe (threads dedupe naturally); the reverse
        order loses mail."""
        profile = self._execute(self._gmail.users().getProfile(userId="me"))
        new_history_id = str(profile["historyId"])
        since = int(time.time()) - days * 86400   # Gmail 'after:' accepts epoch seconds
        thread_ids: dict[str, None] = {}
        page_token = None
        while True:
            resp = self._execute(self._gmail.users().messages().list(
                userId="me", q=f"after:{since}", maxResults=500, pageToken=page_token))
            for msg in resp.get("messages") or []:
                if msg.get("threadId"):
                    thread_ids.setdefault(msg["threadId"], None)
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return PullResult(threads=self._thread_summaries(list(thread_ids)),
                          new_history_id=new_history_id)

    def _thread_summaries(self, thread_ids: list[str]) -> list[ThreadSummary]:
        """metadata-only threads.get per changed thread; summarize the newest message."""
        out: list[ThreadSummary] = []
        for tid in thread_ids:
            try:
                thread = self._execute(self._gmail.users().threads().get(
                    userId="me", id=tid, format="metadata",
                    metadataHeaders=["From", "Subject", "Date"]))
            except HttpError as e:
                if _status(e) == 404:   # deleted between history.list and fetch — gone is gone
                    continue
                raise
            msgs = thread.get("messages") or []
            if not msgs:
                continue
            newest = msgs[-1]           # thread messages arrive oldest-first
            headers = _header_map(newest)
            out.append(ThreadSummary(
                account=self.account,
                thread_id=tid,
                last_message_id=newest["id"],
                sender=_clean(headers.get("from")),
                subject=_clean(headers.get("subject")),
                date=_clean(headers.get("date")),
                snippet=_clean(newest.get("snippet")),
                label_ids=list(newest.get("labelIds") or []),
            ))
        return out

    # ---- act (reversible only: label + draft — nothing outbound exists in this module) ----------

    def apply_label(self, message_ids: list[str], label_name: str) -> None:
        """Get-or-create the label by name, then batchModify in <=1000-id chunks."""
        if not message_ids:
            return
        label_id = self._label_id(label_name)
        for i in range(0, len(message_ids), _BATCH_MODIFY_CAP):
            self._execute(self._gmail.users().messages().batchModify(
                userId="me", body={"ids": message_ids[i:i + _BATCH_MODIFY_CAP],
                                   "addLabelIds": [label_id]}))

    def _label_id(self, name: str) -> str:
        if self._labels is None:   # one labels.list per client instance
            resp = self._execute(self._gmail.users().labels().list(userId="me"))
            self._labels = {lb["name"].lower(): lb["id"] for lb in resp.get("labels") or []}
        key = name.lower()         # Gmail rejects creates that differ only by case — match likewise
        if key not in self._labels:
            created = self._execute(self._gmail.users().labels().create(
                userId="me", body={"name": name,
                                   "labelListVisibility": "labelShow",
                                   "messageListVisibility": "show"}))
            self._labels[key] = created["id"]
        return self._labels[key]

    def create_reply_draft(self, parent_message_id: str, body_text: str) -> str:
        """Create a correctly-THREADED reply draft; returns the draft id. Threading needs all
        three of threadId + In-Reply-To/References + subject — drop any one and strict clients
        show a disjoint new mail. Headers ride in the raw MIME: for raw-format messages the
        JSON payload.headers field is silently ignored."""
        parent = self._execute(self._gmail.users().messages().get(
            userId="me", id=parent_message_id, format="metadata",
            metadataHeaders=["Message-ID", "References", "Subject", "From", "Reply-To"]))
        headers = _header_map(parent)
        parent_msg_id = headers.get("message-id", "").strip()
        subject = headers.get("subject", "").strip()

        reply = EmailMessage()
        reply["To"] = headers.get("reply-to") or headers.get("from", "")
        reply["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        if parent_msg_id:
            reply["In-Reply-To"] = parent_msg_id
            # RFC 5322 §3.6.4: References = parent's chain + parent's Message-ID. The
            # Message-ID alone breaks threading in strict clients once a thread grows.
            refs = headers.get("references", "").strip()
            reply["References"] = f"{refs} {parent_msg_id}".strip()
        reply.set_content(body_text)   # plain text only — v1 drafts carry no HTML

        raw = base64.urlsafe_b64encode(reply.as_bytes()).decode()
        draft = self._execute(self._gmail.users().drafts().create(
            userId="me", body={"message": {"raw": raw, "threadId": parent["threadId"]}}))
        return draft["id"]

    # ---- drive mirror ----------------------------------------------------------------------------

    def upload_brief_to_drive(self, filename: str, content: str) -> str:
        """Upload the brief markdown into the get-or-create app folder; returns webViewLink."""
        created = self._execute(self._drive.files().create(
            body={"name": filename, "parents": [self._drive_folder_id()]},
            media_body=MediaInMemoryUpload(content.encode("utf-8"), mimetype="text/markdown"),
            fields="webViewLink"))
        return created["webViewLink"]

    def _drive_folder_id(self) -> str:
        if self._folder_id is None:
            resp = self._execute(self._drive.files().list(
                q=(f"name='{_BRIEF_FOLDER}' "
                   "and mimeType='application/vnd.google-apps.folder' and trashed=false"),
                spaces="drive", fields="files(id)"))
            files = resp.get("files") or []
            if files:
                self._folder_id = files[0]["id"]
            else:
                folder = self._execute(self._drive.files().create(
                    body={"name": _BRIEF_FOLDER,
                          "mimeType": "application/vnd.google-apps.folder"},
                    fields="id"))
                self._folder_id = folder["id"]
        return self._folder_id
