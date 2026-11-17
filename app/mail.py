"""Outgoing mail: sign-up confirmation, password reset, review notices. Plain text, in the recipient's language.

Pages only queue a `send_mail` job; the worker sends it. The API container has no internet access (compose.yml), the
worker does, and a slow mail server never holds up a page. The worker also makes the one-time token for a link, so a
token exists in plain text only in the mail itself (the database keeps its sha256, see users.py).

`MAIL_BACKEND=smtp` (production) sends through SMTP_HOST; `memory` (tests) only appends to `OUTBOX`, so no test can
send a real mail. The body is never logged."""
from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from .config import settings

log = logging.getLogger("mail")


@dataclass
class Mail:
    to: str
    subject: str
    body: str


OUTBOX: list[Mail] = []


class MailError(RuntimeError):
    """The mail could not be sent (no SMTP configured, or the server refused it)."""


def configured() -> bool:
    cfg = settings()
    return cfg.mail_backend == "memory" or bool(cfg.smtp_host and cfg.smtp_from)


def send(to: str, subject: str, body: str) -> None:
    cfg = settings()
    if cfg.mail_backend == "memory":
        OUTBOX.append(Mail(to, subject, body))
        return
    if not (cfg.smtp_host and cfg.smtp_from):
        raise MailError("SMTP_HOST and SMTP_FROM are not set")
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = cfg.smtp_from, to, subject
    msg["Date"], msg["Message-ID"] = formatdate(localtime=True), make_msgid()
    msg.set_content(body)
    try:
        if cfg.smtp_security == "ssl":
            server: smtplib.SMTP = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=20, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=20)
        with server:
            if cfg.smtp_security == "starttls":
                server.starttls(context=ssl.create_default_context())
            if cfg.smtp_user:
                server.login(cfg.smtp_user, cfg.smtp_password)
            server.send_message(msg)
    except (OSError, smtplib.SMTPException) as e:
        # the address and the reason, never the body (it holds a token)
        log.warning("mail to %s failed: %s", to, e)
        raise MailError(str(e)) from e


# ---------------------------------------------------------------- the queue (pages) and the job (worker)
def queue(conn, mail: str, user_id: int, base_url: str, **extra) -> None:
    """mail: 'confirm' | 'reset' (a link with a new token for the user) | 'review' (a notice to the admin)."""
    from psycopg.types.json import Jsonb
    conn.execute("INSERT INTO jobs (kind, payload) VALUES ('send_mail', %s)",
                 (Jsonb({"mail": mail, "user_id": user_id, "base": base_url.rstrip("/"), **extra}),))


def send_job(payload: dict) -> None:
    from . import i18n, users
    from .db import tx
    kind, base = payload["mail"], settings().public_url.rstrip("/") or payload["base"]
    with tx() as conn:
        u = conn.execute("SELECT id, email, lang, confirmed_at, disabled_at FROM users WHERE id = %s", (payload["user_id"],)).fetchone()
        if not u or u["disabled_at"] or (kind == "confirm" and u["confirmed_at"]) or (kind == "reset" and not u["confirmed_at"]):
            return    # nothing to send any more
        if kind in ("confirm", "reset"):
            link = f"{base}/admin/{kind}?token={users.new_token(conn, u['id'], kind)}"
            subject, body = i18n.tr(u["lang"], f"mail.{kind}.subject"), i18n.tr(u["lang"], f"mail.{kind}.body", link=link)
        elif kind == "review":
            subject = i18n.tr(u["lang"], "mail.review.subject", who=payload["who"])
            body = i18n.tr(u["lang"], "mail.review.body", who=payload["who"], n=payload["n"], category=payload["category"],
                           link=f"{base}/admin/batches/{payload['batch_id']}")
        else:
            raise ValueError(f"unknown mail {kind}")
    send(u["email"], subject, body)   # after the commit: the link works when the mail arrives


def run_queued() -> int:
    """Send every queued mail now (tests; the worker does this in production)."""
    from .db import tx
    with tx() as conn:
        jobs = conn.execute("SELECT id, payload FROM jobs WHERE kind = 'send_mail' AND status = 'queued' ORDER BY id").fetchall()
        conn.execute("UPDATE jobs SET status = 'done' WHERE id = ANY(%s)", ([j["id"] for j in jobs],))
    for j in jobs:
        send_job(j["payload"])
    return len(jobs)


def register(handlers: dict) -> None:
    handlers["send_mail"] = send_job
