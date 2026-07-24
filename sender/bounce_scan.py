#!/usr/bin/env python3
"""
bounce_scan.py — Standalone auto bounce scanner.
Run this independently via macOS LaunchAgent every 2 hours.
It checks Gmail IMAP for bounces, classifies them, and re-syncs all
sent/bounce/open state to the Render tracker (in case Render's free-tier
instance reset since the last sync).
"""
import os
import sqlite3

# Make sure we run from the project directory
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from send_emails import CONFIG, init_state_tables, check_and_sync_bounces, sync_tracker_from_db

if __name__ == "__main__":
    cfg = CONFIG.copy()
    print("[bounce_scan] Starting auto bounce check + tracker sync...")
    conn = sqlite3.connect(cfg["db_path"])
    conn.row_factory = sqlite3.Row
    init_state_tables(conn)
    check_and_sync_bounces(cfg, conn)
    sync_tracker_from_db(cfg, conn)
    conn.close()
    print("[bounce_scan] Done.")
