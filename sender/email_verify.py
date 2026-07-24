"""
email_verify.py — email verification.

Order: local (free, MX record check) -> SMTP RCPT probe (free, unlimited).
No third-party paid verification services are used here — verification is
done locally via DNS plus a direct SMTP RCPT TO probe sent from a real
mailbox address (GMAIL_ADDRESS).

The SMTP probe connects directly to the recipient's mail server and issues a
RCPT TO without sending anything — the same low-level trick paid verifiers
use internally. It's free and unlimited, but has a real blind spot: Gmail and
Google Workspace-hosted domains accept RCPT TO for virtually any address and
only bounce it later during actual delivery, so it won't catch a dead mailbox
on those domains. It's a real (if partial) improvement over local-only
checks: dead domains, typo'd domains, and servers that do strict RCPT-time
validation all still get caught.

If the SMTP probe is inconclusive (blocked port 25, timeout, etc.), the local
MX-check result is trusted (avoids blocking sends over a checker being down).
"""
import os
import re
import smtplib
import socket

import dns.resolver


def verify_local(email: str) -> bool:
    if not re.match(r'^[\w\.\+\-]+@[\w\-]+\.[a-z]{2,}$', email, re.I):
        print(f"  [Validation] Invalid syntax: {email}")
        return False

    domain = email.split('@')[1]
    try:
        records = dns.resolver.resolve(domain, 'MX')
        if records:
            return True
    except dns.resolver.NoAnswer:
        print(f"  [Validation] No mail server found for domain: {domain}")
        return False
    except dns.resolver.NXDOMAIN:
        print(f"  [Validation] Domain does not exist: {domain}")
        return False
    except Exception as e:
        print(f"  [Validation] DNS error for {domain}: {e}")
        return True  # Fallback to True if DNS fails to avoid false positives
    return False


SMTP_TIMEOUT = 6
SMTP_MAIL_FROM = os.environ.get("GMAIL_ADDRESS", "verify@localhost")
SMTP_HELO_DOMAIN = SMTP_MAIL_FROM.split("@")[-1] if "@" in SMTP_MAIL_FROM else "localhost"


def verify_smtp(email: str) -> bool | None:
    """Direct SMTP RCPT TO probe from the real GMAIL_ADDRESS mailbox. Free,
    no API key or quota involved."""
    domain = email.split('@')[1]
    try:
        records = sorted(dns.resolver.resolve(domain, 'MX'), key=lambda r: r.preference)
        mx_host = str(records[0].exchange).rstrip('.')
    except Exception as e:
        print(f"  [SMTP] MX lookup failed for {domain}: {e}")
        return None

    server = None
    try:
        server = smtplib.SMTP(timeout=SMTP_TIMEOUT)
        server.connect(mx_host, 25)
        server.helo(SMTP_HELO_DOMAIN)
        server.mail(SMTP_MAIL_FROM)
        code, message = server.rcpt(email)

        if code == 250:
            return True
        if code in (550, 551, 553, 554):
            return False
        print(f"  [SMTP] inconclusive response {code}: {message}")
        return None
    except (smtplib.SMTPException, socket.timeout, OSError) as e:
        print(f"  [SMTP] connection to {mx_host} failed: {e}")
        return None
    finally:
        if server:
            try:
                server.quit()
            except Exception:
                pass


def verify_email(email: str):
    """Returns (is_valid: bool, checked_by: str)."""
    if not verify_local(email):
        return False, "local_mx"

    result = verify_smtp(email)
    if result is True:
        return True, "smtp"
    if result is False:
        return False, "smtp"

    return True, "local_mx_fallback"
