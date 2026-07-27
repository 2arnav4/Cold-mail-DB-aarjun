#!/usr/bin/env python3
"""
import_contacts.py — load a contact CSV into turso-full.db.

The CSV is the source of truth that lives in git; turso-full.db is built from
it locally and stays gitignored. Pull the repo, run this once, and you have a
working database — without two people writing to the same tracked binary.

Additive and idempotent. Nothing is ever deleted: existing contacts,
companies, and all send history (sent_emails / failed_sends / wrong_address /
opens_backup) are left exactly as they are. Every address that has already
been contacted — sent to, bounced, rejected at verification, or seen opening
a mail — is skipped, so dropping in a newer CSV and re-running can never
re-queue someone who was already emailed.

Matching is case-insensitive throughout. That matters: Hi@WeAreCatalystX.com
was emailed and then Hi@wearecatalystx.com was emailed again — the same
mailbox, contacted twice, because a case-sensitive check treated them as two
different people.

Usage:
    python3 import_contacts.py                        # default CSV below
    python3 import_contacts.py "Some Other List.csv"  # any other CSV
    python3 import_contacts.py --dry-run              # report, write nothing
    python3 import_contacts.py --priority 110         # queue these first
"""

import argparse
import csv
import os
import re
import sqlite3
import sys
from datetime import datetime

DB_PATH = "turso-full.db"
DEFAULT_CSV = "Copy of HR_LIST 300 - Sorted_HR_LIST.csv"

# Column headers are not stable across the lists we get handed — "Contact
# Details" in one file is "Email" in the next — so match on aliases instead of
# fixed positions.
COLUMN_ALIASES = {
    "email": ("email", "emails", "contact details", "email address", "e-mail", "mail"),
    "company": ("company", "company name", "organisation", "organization", "startup"),
    "name": ("name", "contact name", "full name", "person", "founder"),
    "role": ("designation", "role", "title", "position"),
}

# Addresses at these domains say nothing about which company someone works
# for, so they can't be used to identify a company the way a corporate domain
# can. Two unrelated companies whose contacts both use Gmail would otherwise
# collide on the UNIQUE(domain) constraint and get merged into one company.
FREE_MAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.in", "yahoo.co.in",
    "outlook.com", "hotmail.com", "live.com", "icloud.com", "me.com",
    "rediffmail.com", "protonmail.com", "proton.me", "aol.com", "zoho.com",
}

EMAIL_RE = re.compile(r"^[\w.+\-']+@[\w\-]+(\.[\w\-]+)+$")

# Schema for the contact side of the database. On a fresh clone turso-full.db
# does not exist at all — it is gitignored, because it is a binary that every
# send mutates and git cannot merge. This recreates it so that cloning the
# repo and running this script is enough to get a working queue. The state
# tables (sent_emails, failed_sends, bounce_scan_state, opens_backup) are left
# to send_emails.py's init_state_tables(), which owns them.
SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER NOT NULL,
    domain VARCHAR(255) NOT NULL,
    name VARCHAR(255) NOT NULL,
    source VARCHAR(32) NOT NULL,
    funding_stage VARCHAR(32),
    industry VARCHAR(64),
    created_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE (domain)
);
CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER NOT NULL,
    company_id INTEGER NOT NULL,
    name VARCHAR(255),
    role VARCHAR(128),
    email VARCHAR(255),
    email_confidence INTEGER,
    email_verified BOOLEAN DEFAULT 0 NOT NULL,
    is_invalid BOOLEAN DEFAULT 0 NOT NULL,
    created_at DATETIME NOT NULL,
    source TEXT(64),
    priority INTEGER DEFAULT 0,
    PRIMARY KEY (id),
    UNIQUE (email),
    FOREIGN KEY(company_id) REFERENCES companies (id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS send_queue (
    id INTEGER NOT NULL,
    contact_id INTEGER,
    status VARCHAR(16) DEFAULT 'PENDING' NOT NULL,
    PRIMARY KEY (id)
);
CREATE TABLE IF NOT EXISTS wrong_address (
    id INTEGER PRIMARY KEY,
    original_contact_id INTEGER,
    company_id INTEGER,
    company_name VARCHAR(255),
    name VARCHAR(255),
    role VARCHAR(128),
    email VARCHAR(255) NOT NULL,
    failed_service VARCHAR(32) NOT NULL,
    invalid_reason VARCHAR(64),
    checked_at DATETIME NOT NULL
);
"""


def detect_columns(fieldnames):
    """Map the CSV's actual headers onto the fields we need."""
    resolved = {}
    for header in fieldnames or []:
        key = (header or "").strip().lower()
        for field, aliases in COLUMN_ALIASES.items():
            if key in aliases and field not in resolved:
                resolved[field] = header
    return resolved


def split_emails(cell):
    """One cell can hold several addresses, e.g.
    "dhruv.shah@joinditto.in , dhruv.s@joinditto.in" — take them all."""
    if not cell:
        return []
    candidates = re.split(r"[,;/|]+|\s+", cell.strip())
    return [c.strip().strip("<>()[]").rstrip(".") for c in candidates if "@" in c]


def slugify(text, fallback="company"):
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return slug or fallback


def company_domain(email, company_name):
    """A company's identity key. Prefer its real mail domain; fall back to a
    slug of the name when the address is a free-mail one."""
    domain = email.split("@")[1].lower()
    if domain in FREE_MAIL_DOMAINS:
        return f"{slugify(company_name)}.invalid"
    return domain


def load_suppression(conn):
    """Every address that must never be queued again, lowercased.

    Note the opens_backup source: an address with a recorded open had a
    tracking pixel fire for it, which means a mail carrying that pixel was
    delivered and opened. Some of those have no sent_emails row (history was
    consolidated into this database part-way through the campaign), so
    checking sent_emails alone would let them through a second time.
    """
    suppressed = {}
    sources = [
        ("sent_emails", "SELECT email, status FROM sent_emails"),
        ("failed_sends", "SELECT email, 'failed' FROM failed_sends"),
        ("wrong_address", "SELECT email, 'invalid address' FROM wrong_address"),
        ("opens_backup", "SELECT DISTINCT email, 'opened' FROM opens_backup"),
    ]
    for table, query in sources:
        try:
            for email, why in conn.execute(query):
                if email:
                    suppressed.setdefault(email.strip().lower(), f"{why} ({table})")
        except sqlite3.OperationalError:
            pass  # table not created yet — send_emails.py makes them on first run
    return suppressed


def main():
    parser = argparse.ArgumentParser(description="Import contacts from a CSV into turso-full.db")
    parser.add_argument("csv_path", nargs="?", default=DEFAULT_CSV, help="CSV to import")
    parser.add_argument("--db", default=DB_PATH, help="Database path")
    parser.add_argument("--dry-run", action="store_true", help="Report what would happen, write nothing")
    parser.add_argument("--priority", type=int, default=100,
                        help="Send priority for imported contacts (higher goes first; default 100)")
    args = parser.parse_args()

    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    if not os.path.exists(args.csv_path):
        print(f"ERROR: CSV not found: {args.csv_path}")
        sys.exit(1)
    fresh = not os.path.exists(args.db)

    # utf-8-sig: these files come out of Excel, which writes a BOM that would
    # otherwise end up glued to the first header name.
    with open(args.csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        columns = detect_columns(reader.fieldnames)
        rows = list(reader)

    if "email" not in columns:
        print(f"ERROR: No email column found. Headers were: {reader.fieldnames}")
        print(f"       Expected one of: {', '.join(COLUMN_ALIASES['email'])}")
        sys.exit(1)

    print(f"\nImporting {args.csv_path}")
    print(f"  Rows in CSV   : {len(rows)}")
    print(f"  Columns mapped: {', '.join(f'{k}={v!r}' for k, v in columns.items())}")
    missing = [f for f in ("company", "name", "role") if f not in columns]
    if missing:
        print(f"  Not in this CSV: {', '.join(missing)} — those placeholders fall back to defaults")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    if fresh:
        print(f"  No database yet — creating {args.db}")
        conn.executescript(SCHEMA)
        conn.commit()

    suppressed = load_suppression(conn)
    existing_contacts = {
        r[0].strip().lower()
        for r in conn.execute("SELECT email FROM contacts WHERE email IS NOT NULL")
        if r[0]
    }
    companies_by_domain = {
        r["domain"].lower(): r["id"]
        for r in conn.execute("SELECT id, domain FROM companies")
    }

    next_company_id = (conn.execute("SELECT COALESCE(MAX(id), 0) FROM companies").fetchone()[0]) + 1
    next_contact_id = (conn.execute("SELECT COALESCE(MAX(id), 0) FROM contacts").fetchone()[0]) + 1

    source_tag = f"csv:{slugify(os.path.splitext(os.path.basename(args.csv_path))[0])}"[:32]
    now = datetime.now().isoformat()

    new_companies, new_contacts = [], []
    stat = {"added": 0, "already_queued": 0, "already_contacted": 0, "bad_email": 0, "no_email": 0}
    skipped_contacted, skipped_bad = [], []
    seen_this_run = set()

    for row in rows:
        raw = (row.get(columns["email"]) or "").strip()
        emails = split_emails(raw)
        if not emails:
            stat["no_email"] += 1
            continue

        company_name = (row.get(columns["company"]) or "").strip().strip("-,").strip() if "company" in columns else ""
        person_name = (row.get(columns["name"]) or "").strip() if "name" in columns else ""
        person_role = (row.get(columns["role"]) or "").strip() if "role" in columns else ""

        for email in emails:
            key = email.lower()

            if not EMAIL_RE.match(email):
                stat["bad_email"] += 1
                skipped_bad.append(email)
                continue
            if key in suppressed:
                stat["already_contacted"] += 1
                skipped_contacted.append((email, suppressed[key]))
                continue
            if key in existing_contacts or key in seen_this_run:
                stat["already_queued"] += 1
                continue

            domain = company_domain(email, company_name)
            company_id = companies_by_domain.get(domain)
            if company_id is None:
                company_id = next_company_id
                next_company_id += 1
                companies_by_domain[domain] = company_id
                new_companies.append(
                    (company_id, domain, company_name or domain, source_tag, None, None, now)
                )

            new_contacts.append(
                (next_contact_id, company_id, person_name or None, person_role or None,
                 email, None, 0, 0, now, source_tag, args.priority)
            )
            next_contact_id += 1
            seen_this_run.add(key)
            stat["added"] += 1

    print(f"\n  New contacts to add : {stat['added']}")
    print(f"  New companies       : {len(new_companies)}")
    print(f"  Skipped — already contacted : {stat['already_contacted']}")
    print(f"  Skipped — already in queue  : {stat['already_queued']}")
    print(f"  Skipped — malformed address : {stat['bad_email']}")
    if stat["no_email"]:
        print(f"  Skipped — row had no email  : {stat['no_email']}")

    for email, why in skipped_contacted[:10]:
        print(f"      already contacted: {email}  — {why}")
    if len(skipped_contacted) > 10:
        print(f"      ... and {len(skipped_contacted) - 10} more")
    for email in skipped_bad[:5]:
        print(f"      malformed: {email!r}")

    if args.dry_run:
        print("\n  [DRY RUN] Nothing written.\n")
        conn.close()
        return

    if not new_contacts:
        print("\n  Nothing new to import — database already up to date.\n")
        conn.close()
        return

    conn.executemany(
        "INSERT INTO companies (id, domain, name, source, funding_stage, industry, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        new_companies,
    )
    conn.executemany(
        "INSERT INTO contacts (id, company_id, name, role, email, email_confidence, "
        "email_verified, is_invalid, created_at, source, priority) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        new_contacts,
    )
    conn.commit()

    total_contacts = conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0]
    total_companies = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
    try:
        history = conn.execute("SELECT COUNT(*) FROM sent_emails").fetchone()[0]
    except sqlite3.OperationalError:
        history = None  # fresh database — send_emails.py creates this on first run
    conn.close()

    print(f"\n  Imported. Database now holds {total_contacts} contacts "
          f"across {total_companies} companies.")
    if history is None:
        print("  No send history yet — nothing has been emailed from this database.\n")
    else:
        print(f"  Send history preserved: {history} rows in sent_emails.\n")


if __name__ == "__main__":
    main()
