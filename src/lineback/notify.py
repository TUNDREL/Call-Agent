"""Owner notifications for new booking requests (email via stdlib SMTP)."""

import logging
import smtplib
import sqlite3
import ssl
import threading
from email.message import EmailMessage
from typing import Protocol

from .config import Settings
from .db import Database, booking_reference, utcnow

logger = logging.getLogger("lineback.notify")

_SEND_LOCK = threading.Lock()


class Notifier(Protocol):
    delivers: bool

    def send(self, subject: str, body: str) -> None: ...


class LogNotifier:
    """Fallback when SMTP is not configured: nothing is delivered, only logged."""

    delivers = False

    def send(self, subject: str, body: str) -> None:
        logger.warning("NOT SENT (no SMTP configured): %s\n%s", subject, body)


class SmtpNotifier:
    delivers = True

    def __init__(self, settings: Settings):
        assert settings.smtp_host and settings.owner_email and settings.smtp_from
        self.s = settings

    def send(self, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.s.smtp_from
        msg["To"] = self.s.owner_email
        msg.set_content(body)
        context = ssl.create_default_context()
        if self.s.smtp_security == "ssl":
            client = smtplib.SMTP_SSL(self.s.smtp_host, self.s.smtp_port, timeout=15, context=context)
        else:
            client = smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=15)
        with client:
            if self.s.smtp_security == "starttls":
                client.starttls(context=context)
            if self.s.smtp_user:
                client.login(self.s.smtp_user, self.s.smtp_password or "")
            client.send_message(msg)


def build_notifier(settings: Settings) -> Notifier:
    if settings.smtp_host and settings.owner_email and settings.smtp_from:
        return SmtpNotifier(settings)
    return LogNotifier()


def format_booking(row: sqlite3.Row, business_name: str) -> tuple[str, str]:
    ref = booking_reference(row["id"])
    subject = f"[{business_name}] New booking request {ref} (needs confirmation)"
    body = "\n".join(
        [
            f"New booking REQUEST {ref}. It is not confirmed.",
            f"Received: {row['created_at']} (UTC)",
            "",
            f"Customer:       {row['customer_name'] or '(not given)'}",
            f"Phone:          {row['customer_phone']}",
            f"Address:        {row['service_address'] or '(not given)'}",
            f"Service:        {row['service_type'] or '(not given)'}",
            f"Preferred time: {row['preferred_time']}",
            f"Notes:          {row['notes'] or '-'}",
            "",
            "Please contact the customer to confirm.",
        ]
    )
    return subject, body


def send_unsent(db: Database, notifier: Notifier, business_name: str, limit: int = 20) -> int:
    """Send every pending request that has not been announced yet; stop at the first failure.

    Safe to call repeatedly: a failed send is retried on the next call.
    """
    if not notifier.delivers:
        return 0
    sent = 0
    with _SEND_LOCK:
        with db.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM booking_requests WHERE notified_at IS NULL AND status = 'pending' "
                "ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
        for row in rows:
            subject, body = format_booking(row, business_name)
            try:
                notifier.send(subject, body)
            except Exception as exc:
                logger.exception("Could not send notification for request %s", row["id"])
                with db.connection() as conn:
                    conn.execute(
                        "UPDATE booking_requests SET notify_error = ? WHERE id = ?",
                        (f"{type(exc).__name__}: {exc}"[:300], row["id"]),
                    )
                break
            with db.connection() as conn:
                conn.execute(
                    "UPDATE booking_requests SET notified_at = ?, notify_error = NULL WHERE id = ?",
                    (utcnow(), row["id"]),
                )
            sent += 1
    return sent


def dispatch_notifications(
    db: Database, notifier: Notifier, business_name: str, booking_ids: list[int]
) -> None:
    """Runs after the webhook response has been sent, so it never delays the call."""
    if notifier.delivers:
        send_unsent(db, notifier, business_name)
        return
    with db.connection() as conn:
        for booking_id in booking_ids:
            row = conn.execute(
                "SELECT * FROM booking_requests WHERE id = ?", (booking_id,)
            ).fetchone()
            if row:
                notifier.send(*format_booking(row, business_name))
