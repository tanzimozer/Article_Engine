"""Google Drive hold queue.

Held articles become native Google Docs so they can be read and commented on
from anywhere. Approval is an edit to a ``STATUS:`` line inside the doc.

Everything else this pipeline writes to Drive is a one-way mirror. The hold
queue is the only two-way surface, which keeps the number of places that can
disagree with the database down to one.

Native Docs mean the Docs API and a service account, not Drive Desktop sync —
a native Doc on disk is a pointer file with no readable body.

Credentials come from ``GOOGLE_SERVICE_ACCOUNT_JSON`` (the JSON key, inline).
Without it every function here degrades to a logged no-op rather than raising,
so a missing credential holds articles in the database without taking the run
down.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive.file",
]


class DriveUnavailable(RuntimeError):
    """No usable Google credentials."""


def _services() -> tuple[Any, Any]:
    """Build (docs, drive) API clients. Raises DriveUnavailable if unconfigured."""
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not raw:
        raise DriveUnavailable("GOOGLE_SERVICE_ACCOUNT_JSON not set")

    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise DriveUnavailable(f"google client libraries missing: {exc}") from exc

    creds = service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=SCOPES
    )
    return (
        build("docs", "v1", credentials=creds, cache_discovery=False),
        build("drive", "v3", credentials=creds, cache_discovery=False),
    )


def create_hold_doc(cfg: dict[str, Any], article_id: str, title: str,
                    stage: str, reason: str, body: str) -> str | None:
    """Create a hold-queue Doc. Returns its id, or None when Drive is unconfigured.

    The STATUS line goes first so it is visible without scrolling — approving
    means changing one word at the top of the document.
    """
    try:
        docs, drive = _services()
    except DriveUnavailable as exc:
        log.warning("hold doc not created (%s); article held in the database only", exc)
        return None

    status_line = cfg["settings"]["drive"]["approval_status_line"]
    content = (
        f"{status_line} HELD\n"
        f"Change HELD to APPROVED to send this back through the pipeline.\n\n"
        f"Article: {article_id}\n"
        f"Held at: {stage}\n"
        f"Reason: {reason}\n\n"
        f"{'-' * 60}\n\n{body}\n"
    )

    doc = docs.documents().create(body={"title": f"[HOLD] {title}"}).execute()
    doc_id = doc["documentId"]
    docs.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": [{"insertText": {"location": {"index": 1}, "text": content}}]},
    ).execute()

    folder = cfg["settings"]["drive"].get("hold_queue_folder")
    if folder:
        try:
            drive.files().update(fileId=doc_id, addParents=folder,
                                 fields="id, parents").execute()
        except Exception:
            log.warning("could not move %s into %s; it is in Drive root", doc_id, folder)

    log.info("hold doc created: %s", doc_id)
    return doc_id


def read_status(cfg: dict[str, Any], doc_id: str) -> str | None:
    """Return the value on the STATUS line, uppercased. None if unreadable."""
    try:
        docs, _ = _services()
    except DriveUnavailable:
        return None

    status_line = cfg["settings"]["drive"]["approval_status_line"]
    try:
        doc = docs.documents().get(documentId=doc_id).execute()
    except Exception:
        log.exception("could not read hold doc %s", doc_id)
        return None

    for element in doc.get("body", {}).get("content", []):
        for run in element.get("paragraph", {}).get("elements", []):
            text = run.get("textRun", {}).get("content", "")
            if status_line in text:
                return text.split(status_line, 1)[1].strip().upper()
    return None
