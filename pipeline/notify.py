"""Admin notifications by email.

Fires on every publish and on every hold or failure. Silence would be
indistinguishable from a broken cron, which is exactly the failure mode this
guards against.

Credentials come from ``SMTP_HOST``, ``SMTP_PORT``, ``SMTP_USER``,
``SMTP_PASS``. With none set, notifications log instead of sending, so a
missing credential never takes the pipeline down.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage
from typing import Any

log = logging.getLogger(__name__)


def _send(cfg: dict[str, Any], subject: str, body: str) -> None:
    to_addr = cfg["settings"]["notify"]["email_to"]
    host = os.environ.get("SMTP_HOST")
    user = os.environ.get("SMTP_USER")
    password = os.environ.get("SMTP_PASS")
    port = int(os.environ.get("SMTP_PORT", "587"))

    if not (host and user and password):
        log.info("[notify:unsent] %s\n%s", subject, body)
        return

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = user
    message["To"] = to_addr
    message.set_content(body)

    try:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(message)
        log.info("notified: %s", subject)
    except Exception:
        # A notification failing must never fail the run that triggered it.
        log.exception("notification failed: %s", subject)


def published(cfg: dict[str, Any], article_id: str, title: str, url: str) -> None:
    if not cfg["settings"]["notify"].get("on_publish"):
        return
    _send(cfg, f"[Article Engine] Published: {title}",
          f"{title}\n\n{url}\n\nArticle: {article_id}")


def held(cfg: dict[str, Any], article_id: str, stage: str, reason: str,
         doc_url: str | None = None) -> None:
    if not cfg["settings"]["notify"].get("on_hold"):
        return
    body = f"Article {article_id} held at {stage}.\n\nReason: {reason}\n"
    if doc_url:
        body += f"\nReview: {doc_url}\n"
    _send(cfg, f"[Article Engine] Held at {stage}", body)


def archived(cfg: dict[str, Any], article_id: str, reason: str) -> None:
    _send(cfg, "[Article Engine] Archived after repeated holds",
          f"Article {article_id} hit the re-hold cap and will not publish.\n\n{reason}")


def source_failures(cfg: dict[str, Any], failures: list[str]) -> None:
    """One or more sources failed. The run continued with partial coverage."""
    if not cfg["settings"]["notify"].get("on_infra_failure"):
        return
    _send(cfg, f"[Article Engine] {len(failures)} source(s) failed",
          "The run continued with partial coverage.\n\n" + "\n".join(failures))
