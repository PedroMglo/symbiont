"""Email provider — reads recent emails via configured IMAP accounts."""

from __future__ import annotations

import email
import imaplib
import logging
import os
from email.header import decode_header

from personal_context.config import get_settings
from personal_context.types import EmailItem

log = logging.getLogger(__name__)


def get_emails() -> list[EmailItem]:
    """Fetch recent emails via IMAP from configured accounts."""
    cfg = get_settings()
    if not cfg.email.enabled:
        return []

    accounts: list[dict] = []
    if cfg.email.accounts:
        for acc in cfg.email.accounts:
            accounts.append(acc)

    if not accounts:
        log.warning("Email: no accounts configured")
        return []

    all_items: list[EmailItem] = []
    for acc in accounts:
        items = _fetch_account(acc)
        all_items.extend(items)

    return all_items


def _fetch_account(acc: dict) -> list[EmailItem]:
    """Fetch emails from a single IMAP account."""
    host = acc.get("imap_host", "")
    port = int(acc.get("imap_port", 993))
    user = acc.get("imap_user", "")
    ssl = acc.get("imap_ssl", True)
    max_emails = int(acc.get("max_emails", 10))
    folders = acc.get("folders", ["INBOX"])
    label = acc.get("label", user)

    # Password resolution: explicit local config, named env/secret, then derived account names.
    password = acc.get("password", "")
    if not password:
        password_env = acc.get("password_env", "")
        if password_env:
            password = os.environ.get(password_env, "")
    if not password:
        password_secret = acc.get("password_secret", "")
        if password_secret:
            secret_path = password_secret if password_secret.startswith("/") else f"/run/secrets/{password_secret}"
            try:
                with open(secret_path) as f:
                    password = f.read().strip()
            except OSError:
                pass
    if not password:
        # Try env var named after account: PERSONAL_CONTEXT_EMAIL_<SANITIZED_USER>_PASSWORD
        env_key = "PERSONAL_CONTEXT_EMAIL_" + user.upper().replace(".", "_").replace("@", "_AT_") + "_PASSWORD"
        password = os.environ.get(env_key, "")
    if not password:
        # Try Docker secret path
        secret_name = "email_" + user.lower().replace(".", "_").replace("@", "_at_") + "_password"
        secret_path = f"/run/secrets/{secret_name}"
        try:
            with open(secret_path) as f:
                password = f.read().strip()
        except OSError:
            pass

    if not password:
        log.warning("Email: no password configured for account; set secret or env var")
        return []

    try:
        if ssl:
            mail = imaplib.IMAP4_SSL(host, port)
        else:
            mail = imaplib.IMAP4(host, port)

        mail.login(user, password)

        items: list[EmailItem] = []
        for folder in folders:
            mail.select(folder, readonly=True)
            _, data = mail.search(None, "ALL")
            ids = data[0].split()
            recent_ids = ids[-max_emails:]

            for msg_id in reversed(recent_ids):
                _, msg_data = mail.fetch(msg_id, "(RFC822)")
                if not msg_data or not msg_data[0]:
                    continue
                raw_email = msg_data[0][1]
                msg = email.message_from_bytes(raw_email)

                subject = _decode_header(msg.get("Subject", ""))
                sender = _decode_header(msg.get("From", ""))
                date = msg.get("Date", "")

                snippet = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        if part.get_content_type() == "text/plain":
                            body = part.get_payload(decode=True)
                            if body:
                                snippet = body.decode("utf-8", errors="replace")[:200]
                            break
                else:
                    body = msg.get_payload(decode=True)
                    if body:
                        snippet = body.decode("utf-8", errors="replace")[:200]

                items.append(EmailItem(
                    subject=subject,
                    sender=f"[{label}] {sender}",
                    date=date,
                    snippet=snippet,
                ))

        mail.logout()
        log.info("Email: fetched %d items from %s", len(items), label)
        return items

    except Exception as exc:
        log.warning("Email: failed to fetch from %s: %s", label, exc)
        return []


def _decode_header(value: str) -> str:
    """Decode RFC2047 encoded header."""
    if not value:
        return ""
    parts = decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(part)
    return " ".join(decoded)
