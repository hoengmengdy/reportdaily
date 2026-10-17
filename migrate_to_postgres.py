"""Copy existing local records into a fresh PostgreSQL database, without secrets in argv."""
import argparse
from contextlib import closing
import getpass
import json
import os
from pathlib import Path
import shlex
import sqlite3
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from server import AppError, ROOT
from postgres_store import PostgresStore, validate_database_url

TABLES = ("users", "pages", "seed_registry", "reports", "report_rows")


def connection_input(value):
    """Parse copied text, never execute it and never include credentials in errors."""
    value = value.strip()
    if not value:
        raise AppError("Nothing was entered. Copy the connection string from Neon > Connect, paste it here, then press Enter. Input stays hidden.")
    if value.startswith("DATABASE_URL="):
        value = value.split("=", 1)[1].strip()
    if not value.startswith(("postgresql://", "postgres://")):
        try:
            parts = shlex.split(value)
        except ValueError:
            raise AppError("The copied text has unmatched quotes. Copy the complete connection string from Neon > Connect.") from None
        if len(parts) == 1:
            value = parts[0]
        elif len(parts) == 2 and parts[0].lower() == "psql":
            value = parts[1]
        else:
            raise AppError("Paste the PostgreSQL URL or the psql command from Neon > Connect. Do not paste a Python command or terminal prompt.")
    try:
        parsed = urlsplit(value)
        if parsed.scheme in ("http", "https"):
            raise AppError("This is a website address. In Neon, open your database > Connect and copy the connection string beginning postgresql://.")
        if parsed.scheme not in ("postgres", "postgresql") or not parsed.hostname or parsed.path in ("", "/"):
            raise AppError("Copy the complete PostgreSQL connection string from Neon > Connect, including the database name.")
        if parsed.hostname.lower() in ("host", "hostname", "your-host"):
            raise AppError("The example URL is only a template. Use your actual connection string from Neon > Connect.")
        query = parse_qsl(parsed.query, keep_blank_values=True)
        if parsed.hostname not in ("localhost", "127.0.0.1", "::1") and not any(key == "sslmode" for key, _ in query):
            query.append(("sslmode", "require"))
            value = urlunsplit(parsed._replace(query=urlencode(query)))
    except ValueError:
        raise AppError("The connection string is incomplete or malformed. Copy it again from Neon > Connect.") from None
    return validate_database_url(value)


def read_connection_url():
    configured = os.environ.get("DATABASE_URL")
    if configured:
        return connection_input(configured)
    print("Open Neon > your database > Connect and copy the PostgreSQL connection string.")
    print("You can paste the URL or psql command. Input stays hidden; press Enter after pasting.")
    for _ in range(3):
        try:
            return connection_input(getpass.getpass("Neon PostgreSQL connection URL (hidden): "))
        except AppError as error:
            print(str(error))
    raise AppError("No valid connection string was provided. No data was imported. Run this command again after copying it from Neon > Connect.")


def snapshot(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise AppError("The source SQLite database does not exist.")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise AppError("The source database has broken references; migration was stopped.")
        return {table: [dict(row) for row in db.execute(f"SELECT * FROM {table}")] for table in TABLES}


def import_snapshot(target, source):
    initial = json.loads((ROOT / "seed_pages.json").read_text(encoding="utf-8"))
    expected = {p["url"].split("id=", 1)[1]: (p["name"], p["url"], p["target"]) for p in initial}
    with target.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM users LIMIT 1").fetchone() or db.execute("SELECT 1 FROM reports LIMIT 1").fetchone():
            raise AppError("The destination already contains accounts or reports. Nothing was overwritten; use a fresh database.", 409)
        pages = db.execute("SELECT name,url,profile_id,target,active FROM pages").fetchall()
        actual = {p["profile_id"]: (p["name"], p["url"], p["target"]) for p in pages}
        if actual != expected or any(p["active"] != 1 for p in pages):
            raise AppError("The destination page list has been edited. Nothing was overwritten; use a fresh database.", 409)
        # Only untouched seed records can be replaced. All copies are one transaction.
        db.execute("DELETE FROM seed_registry")
        db.execute("DELETE FROM pages")
        for table in TABLES:
            rows = source[table]
            if not rows:
                continue
            columns = list(rows[0])
            allowed_columns = {
                "users": {"id", "username", "password_hash", "role", "created_at", "passwordless"},
                "pages": {"id", "name", "url", "profile_id", "target", "active", "created_at", "updated_at"},
                "seed_registry": {"seed_key", "page_id"},
                "reports": {"date", "revision", "updated_by", "updated_at"},
                "report_rows": {"report_date", "page_id", "page_name", "profile_url", "target", "actual"},
            }[table]
            if not set(columns) <= allowed_columns:
                raise AppError("The source schema is newer than this migration tool; no data was imported.")
            placeholders = ",".join("?" for _ in columns)
            db.executemany(f"INSERT INTO {table}({','.join(columns)}) VALUES ({placeholders})",
                           [tuple(row[column] for column in columns) for row in rows])
        for table in ("users", "pages"):
            db.raw.execute(f"SELECT setval(pg_get_serial_sequence(%s,'id'), COALESCE((SELECT max(id) FROM {table}),1), EXISTS(SELECT 1 FROM {table}))", (table,))
    return {table: len(rows) for table, rows in source.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "data" / "reports.sqlite3")
    args = parser.parse_args()
    try:
        records = snapshot(args.source)
        database_url = read_connection_url()
        counts = import_snapshot(PostgresStore(database_url), records)
    except (KeyboardInterrupt, EOFError):
        parser.exit(130, "\nCancelled. No data was imported.\n")
    except AppError as error:
        parser.exit(1, f"{error}\n")
    except Exception:
        parser.exit(1, "Migration failed. No partial records were saved. Check the database connection and source schema.\n")
    print("Import complete: " + ", ".join(f"{n} {table}" for table, n in counts.items()))
    print("Your local database is unchanged. Existing accounts and passwordless viewer access were preserved. Sign in again on the hosted site.")


if __name__ == "__main__":
    main()
