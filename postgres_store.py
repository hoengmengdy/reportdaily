"""PostgreSQL storage reusing the existing report rules and SQLite local mode."""
from contextlib import contextmanager
import re
import sqlite3
from urllib.parse import parse_qs, urlsplit

from server import AppError, ROOT, Store

WRITE_LOCK = 91370071
UTC_SQL = "to_char(CURRENT_TIMESTAMP AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')"


def validate_database_url(value):
    try:
        if not isinstance(value, str) or any(c.isspace() for c in value):
            raise ValueError
        parsed = urlsplit(value)
        valid = (parsed.scheme in ("postgres", "postgresql") and parsed.hostname
                 and parsed.path not in ("", "/") and not parsed.fragment)
        parsed.port  # Validate malformed/out-of-range port numbers before connecting.
        local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
        ssl = parse_qs(parsed.query, keep_blank_values=True).get("sslmode", [])
        if not valid or len(ssl) > 1 or (not local and ssl not in (["require"], ["verify-ca"], ["verify-full"])):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise AppError("Set DATABASE_URL to a PostgreSQL connection URL with sslmode=require.", 503) from None
    return value


def translate_sql(sql):
    """Only internal SQL reaches this adapter; values always remain parameters."""
    if sql.strip().upper() == "BEGIN IMMEDIATE":
        return f"SELECT pg_advisory_xact_lock({WRITE_LOCK})"
    sql = re.sub(r"\bCURRENT_TIMESTAMP\b", UTC_SQL, sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bWHERE username\s*=\s*\?", "WHERE lower(username)=lower(?)", sql, flags=re.IGNORECASE)
    return sql.replace("?", "%s")


class PgCursor:
    def __init__(self, cursor, inserted=False):
        self.cursor = cursor
        self.rowcount = cursor.rowcount
        self.lastrowid = cursor.fetchone()["id"] if inserted else None

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()

    def __iter__(self):
        return iter(self.cursor)


class PgConnection:
    def __init__(self, raw, integrity_error):
        self.raw = raw
        self.integrity_error = integrity_error

    def execute(self, sql, params=None):
        inserted = bool(re.match(r"\s*INSERT INTO pages\s*\(", sql, re.IGNORECASE))
        translated = translate_sql(sql) + (" RETURNING id" if inserted else "")
        try:
            return PgCursor(self.raw.execute(translated, params), inserted)
        except self.integrity_error:
            # Keep existing validation/error handling independent of the driver.
            raise sqlite3.IntegrityError("Database constraint rejected the record.") from None

    def executemany(self, sql, rows):
        try:
            with self.raw.cursor() as cursor:
                cursor.executemany(translate_sql(sql), rows)
        except self.integrity_error:
            raise sqlite3.IntegrityError("Database constraint rejected the records.") from None


class PostgresStore(Store):
    def __init__(self, database_url):
        self.database_url = validate_database_url(database_url)
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError:
            raise AppError("Install hosted dependencies with python -m pip install -r requirements.txt.", 503) from None
        self.driver = psycopg
        self.row_factory = dict_row
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.raw.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY)")
            for migration in sorted((ROOT / "migrations" / "postgres").glob("*.sql")):
                if not db.execute("SELECT 1 FROM schema_migrations WHERE name=?", (migration.name,)).fetchone():
                    # No parameters: psycopg's simple protocol accepts schema scripts.
                    db.raw.execute(migration.read_text(encoding="utf-8"))
                    db.execute("INSERT INTO schema_migrations VALUES (?)", (migration.name,))
        self.seed()

    @contextmanager
    def connect(self):
        # One transaction/connection per operation, safe across function instances.
        try:
            raw = self.driver.connect(self.database_url, row_factory=self.row_factory,
                                      connect_timeout=10, prepare_threshold=None)
        except self.driver.Error:
            raise AppError("Cannot connect to the hosted database. Check DATABASE_URL in Vercel settings.", 503) from None
        with raw:
            yield PgConnection(raw, self.driver.IntegrityError)
