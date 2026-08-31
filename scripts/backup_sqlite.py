#!/usr/bin/env python3
"""Create a consistent online backup of the radio SQLite database.

Usage::

    python scripts/backup_sqlite.py /app/data/thatradiothing.sqlite3 \
        /backups/thatradiothing-2026-08-30.sqlite3

SQLite's online backup API includes committed WAL data while the service is
running.  This is safer than copying the main database file directly.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path


def backup(source_path: str, destination_path: str) -> None:
    """Back up *source_path* to *destination_path* atomically via SQLite."""

    source = Path(source_path)
    destination = Path(destination_path)
    if not source.is_file():
        raise FileNotFoundError(f"SQLite source does not exist: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        if temporary.exists():
            temporary.unlink()
        with (
            sqlite3.connect(source) as source_connection,
            sqlite3.connect(temporary) as destination_connection,
        ):
            source_connection.backup(destination_connection)
            destination_connection.execute("PRAGMA wal_checkpoint(FULL)")
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    """Parse command-line arguments, create the backup, and report success."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="live SQLite database path")
    parser.add_argument("destination", help="backup file to create or replace")
    args = parser.parse_args()
    backup(args.source, args.destination)
    print(f"SQLite backup written to {args.destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
