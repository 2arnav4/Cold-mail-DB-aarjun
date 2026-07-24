#!/usr/bin/env python3
"""
Cold Mail Sender
----------------
Reads contacts from turso-full.db, sends up to 20 cold emails per day
via Gmail SMTP, tracks all sender state (sent/failed/bounce-scan cursor/
opens backup) in that same turso-full.db, and attaches a resume.

Setup:
  1. Fill in CONFIG below (your Gmail, app password, resume path)
  2. Edit template.txt with your email body
  3. Run:  python3 send_emails.py
  4. To do a dry run (no emails sent): python3 send_emails.py --dry-run

Gmail App Password (NOT your regular password):
  https://myaccount.google.com/apppasswords
  (Requires 2FA to be enabled on your Google account)
"""

import sqlite3
import smtplib
import os
import sys
import argparse
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

# ─────────────────────────────────────────────
#  CONFIG — loaded from .env file automatically
# ─────────────────────────────────────────────
import os as _os


def _load_env(path=".env"):
    """Parse a .env file and inject into os.environ (no dependencies needed)."""
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                _os.environ.setdefault(key.strip(), val.strip())
    except FileNotFoundError:
        pass  # .env is optional; fall back to real env vars


_load_env()

from email_verify import verify_email  # noqa: E402 (must load after _load_env populates os.environ)

CONFIG = {
    "your_email": _os.environ.get("GMAIL_ADDRESS", ""),
    "app_password": _os.environ.get("GMAIL_APP_PASSWORD", ""),
    "your_name": "Aarjun Jain",
    "resume_path": "Aarjun_Resume.pdf",
    "db_path": "turso-full.db",
    "template_path": "template_startups.txt",
    "daily_limit": 100,
    "tracker_url": _os.environ.get("TRACKER_URL", ""),
}

# ─────────────────────────────────────────────────────────────────────────────
#  PERSONALIZED EMAILS
#  Key   = recipient email address (exact match from the PDF)
#  Value = { "subject": "...", "body": "..." }
#  For any contact NOT in this dict, the generic template.txt is used.
# ─────────────────────────────────────────────────────────────────────────────
PERSONALIZED_EMAILS = {}

# ─────────────────────────────────────────────
#  Contact filter — adjust to target who you want
# ─────────────────────────────────────────────
CONTACT_QUERY = """
    SELECT
        ct.id        AS contact_id,
        ct.name      AS contact_name,
        ct.role      AS contact_role,
        ct.email     AS contact_email,
        co.id        AS company_id,
        co.name      AS company_name,
        co.domain    AS company_domain,
        co.industry  AS company_industry,
        co.funding_stage AS funding_stage
    FROM contacts ct
    JOIN companies co ON ct.company_id = co.id
    WHERE
        ct.email IS NOT NULL
        AND ct.is_invalid = 0
        AND ct.email != ''
    ORDER BY ct.priority DESC, ct.id ASC
"""

# ─────────────────────────────────────────────────────────────────────────────
#  Sender state — lives in the same turso-full.db as the contacts, instead of
#  scattered sent_log*.json files. One database, one source of truth.
# ─────────────────────────────────────────────────────────────────────────────


def init_state_tables(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sent_emails (
            email      TEXT PRIMARY KEY,
            company    TEXT DEFAULT '',
            sent_at    TEXT NOT NULL,
            sent_date  TEXT NOT NULL,
            status     TEXT DEFAULT 'sent'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS failed_sends (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            email        TEXT NOT NULL,
            name         TEXT DEFAULT '',
            company      TEXT DEFAULT '',
            error        TEXT DEFAULT '',
            bounce_type  TEXT DEFAULT '',
            retry_after  TEXT DEFAULT '',
            time         TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bounce_scan_state (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS opens_backup (
            email       TEXT NOT NULL,
            company     TEXT DEFAULT '',
            opened_at   TEXT NOT NULL,
            ip          TEXT DEFAULT '',
            user_agent  TEXT DEFAULT '',
            is_bot      INTEGER DEFAULT 0,
            UNIQUE(email, opened_at)
        )
    """)
    conn.commit()


def already_sent(conn: sqlite3.Connection, email: str) -> bool:
    return conn.execute("SELECT 1 FROM sent_emails WHERE email = ?", (email,)).fetchone() is not None


def sent_today(conn: sqlite3.Connection) -> int:
    today = str(date.today())
    row = conn.execute(
        "SELECT COUNT(*) FROM sent_emails WHERE sent_date = ? AND status != 'bounced'",
        (today,),
    ).fetchone()
    return row[0]


def record_sent(conn: sqlite3.Connection, email: str, company: str = ""):
    """Record a successful send. Commits immediately — safe against crashes
    mid-run, same as the old save-after-each-send behavior."""
    sent_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT OR REPLACE INTO sent_emails (email, company, sent_at, sent_date, status) "
        "VALUES (?, ?, ?, ?, 'sent')",
        (email, company, sent_at, str(date.today())),
    )
    conn.commit()


def record_failed(conn: sqlite3.Connection, contact: dict, error: str, bounce_type: str = "", retry_after: str = ""):
    conn.execute(
        "INSERT INTO failed_sends (email, name, company, error, bounce_type, retry_after, time) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            contact["contact_email"],
            contact.get("contact_name") or "",
            contact.get("company_name") or "",
            str(error),
            bounce_type,
            retry_after,
            datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    conn.commit()


def sync_tracker_from_db(cfg: dict, conn: sqlite3.Connection):
    """Re-sync all sent/bounce/open state to the Render tracker.
    Also downloads new opens from the tracker to back them up locally,
    making the dashboard fully persistent across Render restarts."""
    tracker_url = cfg.get("tracker_url", "").rstrip("/")
    if not tracker_url:
        return

    import urllib.request, json as _json

    bounced_emails_set = {
        r[0].lower() for r in conn.execute("SELECT DISTINCT email FROM failed_sends").fetchall()
    }

    # Exclude any false opens for bounced emails from the local backup
    conn.execute(
        "DELETE FROM opens_backup WHERE LOWER(email) IN (SELECT LOWER(email) FROM failed_sends)"
    )
    conn.commit()

    # 1. Download current opens from Render to back them up locally
    try:
        req = urllib.request.Request(f"{tracker_url}/api/stats", method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            server_opens = _json.loads(resp.read())

        for o in server_opens:
            if o.get("email", "").lower() in bounced_emails_set:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO opens_backup (email, company, opened_at, ip, user_agent, is_bot) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    o.get("email", ""),
                    o.get("company", ""),
                    o.get("opened_at", ""),
                    o.get("ip", ""),
                    o.get("user_agent", ""),
                    o.get("is_bot", 0),
                ),
            )
        conn.commit()
    except Exception as err:
        print(f"  Warning: Could not fetch opens backup from server: {err}")

    sends = [
        {"email": r[0], "company": r[1], "sent_at": r[2]}
        for r in conn.execute("SELECT email, company, sent_at FROM sent_emails").fetchall()
    ]

    # Exclude pre-send verification failures — those addresses never actually
    # got a send attempt, so they must never appear in the tracker's "sends"
    # table (that would inflate Total Sent with emails that were never sent).
    bounces = [
        {
            "email": r[0],
            "company": r[1],
            "reason": r[2],
            "bounce_type": r[3],
            "retry_after": r[4],
            "sent_at": r[5],
        }
        for r in conn.execute(
            "SELECT email, company, error, bounce_type, retry_after, time FROM failed_sends "
            "WHERE bounce_type != 'verification'"
        ).fetchall()
    ]

    opens = [
        dict(row)
        for row in conn.execute(
            "SELECT email, company, opened_at, ip, user_agent, is_bot FROM opens_backup"
        ).fetchall()
    ]

    try:
        payload = _json.dumps({"sends": sends, "bounces": bounces, "opens": opens}).encode()
        req = urllib.request.Request(
            f"{tracker_url}/api/bulk_sync",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = _json.loads(resp.read())
        print(
            f"  Tracker synced — {result.get('synced_sends', 0)} sends, {result.get('synced_bounces', 0)} bounces, {result.get('synced_opens', 0)} opens"
        )
    except Exception as e:
        print(f"  Tracker sync warning: {e}")


def load_template(template_path: str) -> tuple:
    """
    Returns (subject, body). Template format:
      First line: Subject: <subject text>
      Blank line
      Rest: body
    """
    with open(template_path, "r") as f:
        content = f.read()

    lines = content.splitlines()
    subject = ""
    body_start = 0

    for i, line in enumerate(lines):
        if line.lower().startswith("subject:"):
            subject = line[len("subject:") :].strip()
            body_start = i + 1
            break

    # Skip blank lines after subject
    while body_start < len(lines) and lines[body_start].strip() == "":
        body_start += 1

    body = "\n".join(lines[body_start:])
    return subject, body


def render(template: str, contact: dict) -> str:
    """Replace {{placeholders}} with contact data."""
    result = template
    replacements = {
        "{{contact_name}}": contact.get("contact_name") or "there",
        "{{first_name}}": (contact.get("contact_name") or "there").split()[0],
        "{{company}}": contact.get("company_name") or "",
        "{{company_domain}}": contact.get("company_domain") or "",
        "{{role}}": contact.get("contact_role") or "",
        "{{industry}}": contact.get("company_industry") or "",
        "{{funding_stage}}": contact.get("funding_stage") or "",
    }
    for placeholder, value in replacements.items():
        result = result.replace(placeholder, value)
    return result


import re


def text_to_html(text: str, pixel_tag: str = "") -> str:
    """Convert plain text email body to clean HTML with clickable links."""
    import html as html_lib

    # Escape HTML special chars first
    escaped = html_lib.escape(text)

    # 1. Parse markdown links [Link Text](URL)
    escaped = re.sub(
        r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', escaped
    )

    # 2. Auto-link remaining raw https:// and http:// URLs (ignoring already linked ones)
    escaped = re.sub(
        r'(?<!href=")(?<!">)(https?://[^\s<>"]+)', r'<a href="\1">\1</a>', escaped
    )

    # Convert newlines to <br> and wrap in clean HTML
    body_html = escaped.replace("\n", "<br>\n")

    return f"""<!DOCTYPE html>
<html>
<body style="font-family: Arial, sans-serif; font-size: 14px; line-height: 1.6; color: #222; max-width: 600px;">
{body_html}
{pixel_tag}
</body>
</html>"""


def wrap_links(text, email, company, tracker_url):
    if not tracker_url:
        return text
    import re
    import base64

    tracker_clean = tracker_url.rstrip("/")

    def replace_url(match):
        url = match.group(1)
        if tracker_clean in url:
            return url
        payload = f"{email}|{company}|{url}"
        encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        return f"{tracker_clean}/c/{encoded}"

    url_pattern = r"(https?://[a-zA-Z0-9.\-_~!$&\'()*+,;=:@/%?#]+)"
    return re.sub(url_pattern, replace_url, text)


def build_email(cfg: dict, contact: dict, subject: str, body: str) -> MIMEMultipart:
    # Use personalized email if we have one for this exact address
    personalized = PERSONALIZED_EMAILS.get(contact["contact_email"])
    if personalized:
        final_subject = personalized["subject"]
        final_body = personalized["body"]
    else:
        final_subject = render(subject, contact)
        final_body = render(body, contact)

    # Build tracking pixel tag if TRACKER_URL is configured
    tracker_url = cfg.get("tracker_url", "").rstrip("/")
    pixel_tag = ""
    if tracker_url:
        import base64 as _b64

        payload = f"{contact['contact_email']}|{contact.get('company_name', '')}"
        encoded = _b64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        pixel_tag = f'<img src="{tracker_url}/t/{encoded}.gif" width="1" height="1" style="display:none;" />'

    # Send as multipart/alternative (plain text + HTML) so links are clickable
    msg = MIMEMultipart("mixed")  # outer container (holds alternative + attachment)
    msg["From"] = f"{cfg['your_name']} <{cfg['your_email']}>"
    msg["To"] = contact["contact_email"]
    msg["Subject"] = final_subject

    # Inner multipart/alternative for plain + HTML
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(final_body, "plain", "utf-8"))
    alt.attach(MIMEText(text_to_html(final_body, pixel_tag), "html", "utf-8"))
    msg.attach(alt)

    # Attach resume if it exists
    resume_path = cfg["resume_path"]
    if os.path.exists(resume_path):
        with open(resume_path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        filename = os.path.basename(resume_path)
        part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
        msg.attach(part)
    else:
        print(
            f"  WARNING: Resume not found at '{resume_path}' — sending without attachment"
        )

    return msg


def remove_contacted_from_db(conn: sqlite3.Connection):
    """Clean up contacted email addresses from the local database before sending."""
    try:
        conn.execute("DELETE FROM contacts WHERE email IN (SELECT email FROM sent_emails)")
        conn.execute(
            "DELETE FROM contacts WHERE id IN "
            "(SELECT contact_id FROM send_queue WHERE status IN ('SENT', 'DONE'))"
        )
        deleted_count = conn.total_changes
        conn.commit()
        if deleted_count > 0:
            print(f"  Pre-send Cleanup: Removed already-contacted contacts from local database.")
    except Exception as e:
        print(f"  Warning: Pre-send database cleanup failed: {e}")


def fetch_contacts(conn: sqlite3.Connection) -> list:
    cur = conn.cursor()
    cur.execute(CONTACT_QUERY)
    return [dict(row) for row in cur.fetchall()]


def print_summary(sent: list, skipped: int, remaining_today: int, dry_run: bool):
    mode = "[DRY RUN] " if dry_run else ""
    print(f"\n{'─' * 50}")
    print(f"  {mode}Done!")
    print(f"  Emails sent this run : {len(sent)}")
    print(f"  Skipped (already sent): {skipped}")
    print(f"  Remaining quota today : {remaining_today}")
    if sent:
        print(f"\n  Sent to:")
        for s in sent:
            print(f"    - {s['contact_email']}  ({s['company_name']})")
    print(f"{'─' * 50}\n")


def check_and_sync_bounces(cfg: dict, conn: sqlite3.Connection) -> list:
    """Connects to Gmail via IMAP, detects bounces using DSN parsing, classifies them,
    marks the matching sent_emails rows as bounced (so they stop counting against
    today's quota), and syncs to the Render tracker."""
    import imaplib
    import email as _email
    import re
    import urllib.request as _urllib
    import json as _json

    print("Checking Gmail for new bounces via IMAP...")
    email_addr = cfg["your_email"]
    app_pwd = cfg["app_password"].replace(" ", "")
    tracker_url = cfg.get("tracker_url", "").rstrip("/")

    row = conn.execute("SELECT value FROM bounce_scan_state WHERE key='last_uid'").fetchone()
    last_uid = int(row[0]) if row else 0

    bounced_records = []  # list of {email, reason, bounce_type, retry_after}

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(email_addr, app_pwd)
        mail.select("INBOX")

        # Search for messages since last processed UID
        status, messages = mail.uid(
            "search",
            None,
            f'(OR FROM "mailer-daemon" FROM "postmaster") UID {last_uid + 1}:*',
        )
        if status != "OK" or not messages[0].split():
            print("  No new bounce notification messages found.")
            mail.logout()
            return []

        message_uids = messages[0].split()
        print(f"  Scanning {len(message_uids)} new bounce notification emails...")

        max_uid = last_uid
        for uid_bytes in message_uids:
            msg_uid = int(uid_bytes)
            if msg_uid > max_uid:
                max_uid = msg_uid

            res, msg_data = mail.uid("fetch", uid_bytes, "(RFC822)")
            for response_part in msg_data:
                if not isinstance(response_part, tuple):
                    continue
                original_msg = _email.message_from_bytes(response_part[1])

                # Check for standard message/delivery-status DSN part
                dsn_part = None
                for part in original_msg.walk():
                    if part.get_content_type() == "message/delivery-status":
                        dsn_part = part
                        break

                bounced_email = None
                bounce_status = None
                bounce_action = None
                diagnostic_code = None

                if dsn_part:
                    try:
                        payload = dsn_part.get_payload()
                        if isinstance(payload, list):
                            for subpart in payload:
                                sub_msg = subpart
                                if sub_msg.get("Final-Recipient"):
                                    fr = sub_msg.get("Final-Recipient")
                                    parts = fr.split(";", 1)
                                    bounced_email = (
                                        parts[1].strip()
                                        if len(parts) > 1
                                        else parts[0].strip()
                                    )
                                    bounce_status = sub_msg.get("Status")
                                    bounce_action = sub_msg.get("Action")
                                    diagnostic_code = sub_msg.get("Diagnostic-Code")
                                    break
                        else:
                            payload_bytes = dsn_part.get_payload(decode=True)
                            blocks = payload_bytes.split(b"\n\n")
                            for block in blocks:
                                sub_msg = _email.message_from_bytes(block)
                                if sub_msg.get("Final-Recipient"):
                                    fr = sub_msg.get("Final-Recipient")
                                    parts = fr.split(";", 1)
                                    bounced_email = (
                                        parts[1].strip()
                                        if len(parts) > 1
                                        else parts[0].strip()
                                    )
                                    bounce_status = sub_msg.get("Status")
                                    bounce_action = sub_msg.get("Action")
                                    diagnostic_code = sub_msg.get("Diagnostic-Code")
                                    break
                    except Exception as pe:
                        print(f"    Error parsing DSN part: {pe}")

                # If no bounced_email was found via delivery-status, fall back to "To:" headers inside the body
                if not bounced_email:
                    body = ""
                    if original_msg.is_multipart():
                        for part in original_msg.walk():
                            if part.get_content_type() == "text/plain":
                                body += part.get_payload(decode=True).decode(
                                    errors="ignore"
                                )
                    else:
                        body = original_msg.get_payload(decode=True).decode(
                            errors="ignore"
                        )

                    to_matches = re.findall(
                        r"To:\s*(?:[^<\n]*?)\<?([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})\>?",
                        body,
                        re.IGNORECASE,
                    )
                    if to_matches:
                        for m in to_matches:
                            if m.lower() != email_addr.lower():
                                bounced_email = m
                                break

                if bounced_email:
                    bounced_email = bounced_email.strip("<> ")
                    if bounced_email.lower() == email_addr.lower():
                        continue

                    # Classify status
                    bounce_type = "unknown"
                    if bounce_status:
                        status_clean = bounce_status.strip()
                        if status_clean.startswith("5"):
                            bounce_type = "hard"
                        elif status_clean.startswith("4"):
                            bounce_type = "soft"
                    elif bounce_action:
                        action_clean = bounce_action.strip().lower()
                        if action_clean == "failed":
                            bounce_type = "hard"
                        elif action_clean == "delayed":
                            bounce_type = "soft"

                    # Classify reason
                    reason = "Bounce — unknown reason"
                    if diagnostic_code:
                        diag_clean = diagnostic_code.strip()
                        parts = diag_clean.split(";", 1)
                        reason = (
                            parts[1].strip() if len(parts) > 1 else parts[0].strip()
                        )

                    # Classify retry_after
                    retry_after = ""
                    if bounce_type == "soft":
                        retry_after = "Temporary failure — Gmail will retry automatically for up to ~4 days before giving up"

                    bounced_records.append(
                        {
                            "email": bounced_email,
                            "reason": reason,
                            "bounce_type": bounce_type,
                            "retry_after": retry_after,
                        }
                    )

        mail.close()
        mail.logout()

        # Update persisted state with the max UID processed
        if max_uid > last_uid:
            try:
                conn.execute(
                    "INSERT INTO bounce_scan_state (key, value) VALUES ('last_uid', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(max_uid),),
                )
                conn.commit()
            except Exception as se:
                print(f"  Warning: Could not save bounce scan state: {se}")

    except Exception as e:
        print(f"  Warning: IMAP bounce check failed: {e}")
        return []

    if not bounced_records:
        print("  No new bounced addresses detected.")
        return []

    bounced_emails = [r["email"] for r in bounced_records]
    hard = sum(1 for r in bounced_records if r["bounce_type"] == "hard")
    soft = sum(1 for r in bounced_records if r["bounce_type"] == "soft")
    unknown = sum(1 for r in bounced_records if r["bounce_type"] == "unknown")
    print(
        f"  Detected {len(bounced_records)} bounces — {hard} hard, {soft} soft, {unknown} unknown: {bounced_emails}"
    )

    existing_failed = {
        r[0] for r in conn.execute("SELECT DISTINCT email FROM failed_sends").fetchall()
    }

    new_bounces_logged = 0

    for rec in bounced_records:
        email = rec["email"]

        existing_sent = conn.execute(
            "SELECT company FROM sent_emails WHERE email = ?", (email,)
        ).fetchone()
        if existing_sent:
            # Mark bounced so it stops counting against today's quota, but keep
            # the row so already_sent() still guards against re-queuing it.
            conn.execute("UPDATE sent_emails SET status='bounced' WHERE email = ?", (email,))
            company = existing_sent[0]
        else:
            conn.execute(
                "INSERT OR IGNORE INTO sent_emails (email, company, sent_at, sent_date, status) "
                "VALUES (?, '', ?, ?, 'bounced')",
                (email, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), str(date.today())),
            )
            company = ""

        if email not in existing_failed:
            conn.execute(
                "INSERT INTO failed_sends (email, name, company, error, bounce_type, retry_after, time) "
                "VALUES (?, '', ?, ?, ?, ?, ?)",
                (
                    email,
                    company,
                    rec["reason"],
                    rec["bounce_type"],
                    rec["retry_after"],
                    datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            new_bounces_logged += 1

            if tracker_url:
                try:
                    payload = _json.dumps(
                        {
                            "email": email,
                            "reason": rec["reason"],
                            "bounce_type": rec["bounce_type"],
                            "retry_after": rec["retry_after"],
                        }
                    ).encode("utf-8")
                    req = _urllib.Request(
                        f"{tracker_url}/api/log_bounce",
                        data=payload,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with _urllib.urlopen(req, timeout=8):
                        pass
                except Exception as te:
                    print(f"    Tracker log_bounce failed for {email}: {te}")

    conn.commit()

    # Clean local SQLite database — only remove hard bounces (soft/unknown may still be valid)
    try:
        hard_emails = [r["email"] for r in bounced_records if r["bounce_type"] == "hard"]
        if hard_emails:
            conn.executemany("DELETE FROM contacts WHERE email = ?", [(e,) for e in hard_emails])
            conn.commit()
            print(f"  Removed hard-bounced contacts from local database.")
    except Exception as dbe:
        print(f"  Warning: Database cleanup failed: {dbe}")

    print(f"  Successfully synced {new_bounces_logged} new bounces.")
    return bounced_emails


def move_to_wrong_address(conn: sqlite3.Connection, contact: dict, service: str, reason: str):
    """Remove a contact from `contacts` and record it in `wrong_address` so it
    never resurfaces in a future CONTACT_QUERY."""
    conn.execute(
        """
        INSERT INTO wrong_address
            (original_contact_id, company_id, company_name, name, role, email,
             failed_service, invalid_reason, checked_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            contact["contact_id"],
            contact.get("company_id"),
            contact.get("company_name"),
            contact.get("contact_name"),
            contact.get("contact_role"),
            contact["contact_email"],
            service,
            reason,
            datetime.now().isoformat(),
        ),
    )
    conn.execute("DELETE FROM contacts WHERE id = ?", (contact["contact_id"],))
    conn.commit()


def main():
    parser = argparse.ArgumentParser(description="Cold Mail Sender")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print emails without actually sending them",
    )
    parser.add_argument("--limit", type=int, help="Cap how many we send THIS run (never exceeds the daily quota)")
    parser.add_argument(
        "--daily-limit",
        type=int,
        help="Override cfg['daily_limit'] for this run only (lets today's send count exceed the normal cap)",
    )
    parser.add_argument(
        "--show-queue",
        action="store_true",
        help="Show the next contacts that would be emailed and exit",
    )
    parser.add_argument(
        "--check-bounces",
        action="store_true",
        help="Check Gmail for bounced emails, sync them to the DB, clean up, and exit",
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Refresh the tracker only: check bounces + push sends/bounces/opens to Render, then exit. Never sends email.",
    )
    args = parser.parse_args()
    dry_run = args.dry_run
    cfg = CONFIG.copy()

    if args.daily_limit:
        cfg["daily_limit"] = args.daily_limit

    conn = sqlite3.connect(cfg["db_path"])
    conn.row_factory = sqlite3.Row
    init_state_tables(conn)

    # Refresh-only: identical to the sync step that normally runs before a real
    # send, but exits immediately after so it can never trigger a send.
    if args.sync:
        check_and_sync_bounces(cfg, conn)
        sync_tracker_from_db(cfg, conn)
        conn.close()
        sys.exit(0)

    # Handle manual bounce check flag
    if args.check_bounces:
        check_and_sync_bounces(cfg, conn)
        conn.close()
        sys.exit(0)

    # Validate config
    if not dry_run:
        if cfg["your_email"] == "you@gmail.com":
            print("ERROR: Please set your_email in CONFIG before running.")
            conn.close()
            sys.exit(1)
        if cfg["app_password"] == "xxxx xxxx xxxx xxxx":
            print("ERROR: Please set your Gmail app_password in CONFIG before running.")
            conn.close()
            sys.exit(1)

    # Automatically check for bounces on startup (if not a dry run)
    if not dry_run:
        check_and_sync_bounces(cfg, conn)
        # Re-sync all local data to Render so dashboard survives server restarts
        sync_tracker_from_db(cfg, conn)

    # Load state
    already_sent_today = sent_today(conn)
    quota_left = cfg["daily_limit"] - already_sent_today

    # --limit caps how many we send THIS run (not the daily total)
    if args.limit:
        quota_left = min(quota_left, args.limit)

    print(f"\nCold Mail Sender")
    print(
        f"  Today's quota: {already_sent_today}/{cfg['daily_limit']} used  ->  {quota_left} left"
    )

    if quota_left <= 0:
        print("  Daily limit already reached. Come back tomorrow!")
        conn.close()
        sys.exit(0)

    # Load template
    if not os.path.exists(cfg["template_path"]):
        print(f"ERROR: Template file not found: {cfg['template_path']}")
        conn.close()
        sys.exit(1)
    subject_template, body_template = load_template(cfg["template_path"])
    if not subject_template:
        print("ERROR: Template missing 'Subject:' line on the first line.")
        conn.close()
        sys.exit(1)

    # Pre-send check: remove already contacted addresses from database
    remove_contacted_from_db(conn)

    # Fetch contacts
    all_contacts = fetch_contacts(conn)
    print(f"  Total contacts in DB: {len(all_contacts)}")

    # Filter out already-sent
    queue = [c for c in all_contacts if not already_sent(conn, c["contact_email"])]
    print(f"  Unsent contacts     : {len(queue)}")

    if args.show_queue:
        print(f"\n  Next {min(quota_left, 20)} in queue:")
        for c in queue[:20]:
            print(
                f"    {c['contact_email']:40s} | {c['company_name']} | {c['contact_role']}"
            )
        conn.close()
        sys.exit(0)

    # Take only what we're allowed today
    batch = queue[:quota_left]
    skipped_count = len(all_contacts) - len(queue)

    if not batch:
        print("  All contacts have been emailed!")
        conn.close()
        sys.exit(0)

    # Send
    sent_this_run = []
    smtp_conn = None

    if not dry_run:
        try:
            smtp_conn = smtplib.SMTP_SSL("smtp.gmail.com", 465)
            smtp_conn.login(cfg["your_email"], cfg["app_password"].replace(" ", ""))
            print(f"  Gmail SMTP connected\n")
        except Exception as e:
            print(f"ERROR: Gmail login failed: {e}")
            print(
                "  Make sure you're using an App Password, not your regular password."
            )
            print("  Generate one at: https://myaccount.google.com/apppasswords")
            conn.close()
            sys.exit(1)

    # In dry-run we just preview the top of the queue. In a real run, invalid
    # addresses get filtered out live by the verification waterfall, so we walk
    # the full priority-ordered queue and keep going until quota_left contacts
    # have actually been sent to (or the queue runs out) — otherwise skipped
    # invalids would silently shrink today's real send count below the daily cap.
    candidates = batch if dry_run else queue

    try:
        for i, contact in enumerate(candidates, 1):
            if not dry_run and len(sent_this_run) >= quota_left:
                break

            email_addr = contact["contact_email"]
            company = contact["company_name"]
            role = contact["contact_role"] or "—"

            progress = f"{len(sent_this_run) + 1:02d}/{quota_left}" if not dry_run else f"{i:02d}/{len(batch)}"
            print(f"  [{progress}] {email_addr:40s} | {company} | {role}")

            if not dry_run:
                is_valid, checked_by = verify_email(email_addr)
                if not is_valid:
                    print(f"         Invalid ({checked_by}). Moving to wrong_address, skipping.")
                    record_failed(conn, contact, f"Failed verification: {checked_by}", bounce_type="verification")
                    move_to_wrong_address(conn, contact, checked_by, "failed verification")
                    continue

            if dry_run:
                personalized = PERSONALIZED_EMAILS.get(email_addr)
                if personalized:
                    subject = personalized["subject"]
                    body = personalized["body"]
                    mode_tag = "[PERSONALIZED]"
                else:
                    subject = render(subject_template, contact)
                    body = render(body_template, contact)
                    mode_tag = "[GENERIC TEMPLATE]"
                print(f"         {mode_tag}")
                print(f"         Subject : {subject}")
                print(f"         Body preview: {body[:120].strip()}...")
                print()
                sent_this_run.append(
                    contact
                )  # track for summary display only, do NOT write to the DB
                continue

            try:
                msg = build_email(cfg, contact, subject_template, body_template)
                smtp_conn.send_message(msg)
                record_sent(conn, email_addr, company)  # commits immediately
                sent_this_run.append(contact)

                # Notify tracking server of successful send
                tracker_url = cfg.get("tracker_url", "").rstrip("/")
                if tracker_url:
                    try:
                        import urllib.request as _urllib
                        import json as _json

                        req = _urllib.Request(
                            f"{tracker_url}/api/log_send",
                            data=_json.dumps(
                                {"email": email_addr, "company": company}
                            ).encode("utf-8"),
                            headers={"Content-Type": "application/json"},
                            method="POST",
                        )
                        with _urllib.urlopen(req, timeout=5) as resp:
                            pass
                    except Exception as te:
                        print(f"         Tracker API Log Warning: {te}")

                print(f"         SENT")
            except Exception as e:
                print(f"         FAILED: {e}")
                record_failed(conn, contact, e)

    finally:
        if smtp_conn:
            smtp_conn.quit()

    # Dry run never writes to the DB — nothing was actually sent

    remaining = cfg["daily_limit"] - sent_today(conn)
    print_summary(sent_this_run, skipped_count, remaining, dry_run)
    conn.close()


if __name__ == "__main__":
    main()
