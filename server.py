"""Local Facebook reporting app. Python 3.12+, no third-party dependencies."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import csv
from datetime import date, datetime, timedelta, timezone
import getpass
import hashlib
import hmac
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlsplit
import zipfile
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent
BANGKOK = timezone(timedelta(hours=7))
MAX_COUNT = 1_000_000
SESSION_SECONDS = 12 * 60 * 60
PASSWORD_ITERATIONS = 600_000


class AppError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def today():
    return datetime.now(BANGKOK).date().isoformat()


def valid_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise AppError("Use a date in YYYY-MM-DD format.")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise AppError("Invalid report date.") from None
    if parsed.isoformat() > today():
        raise AppError("Reports cannot be created for a future date.")
    return value


def count(value, label="Post count"):
    if type(value) is not int or not 0 <= value <= MAX_COUNT:
        raise AppError(f"{label} must be a whole number between 0 and {MAX_COUNT:,}.")
    return value


def page_values(data):
    name = data.get("name")
    url = data.get("url")
    if not isinstance(name, str) or not name.strip() or len(name) > 200:
        raise AppError("Page name must contain 1–200 characters.")
    # Store names exactly as entered, including Khmer and accented characters.
    if any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF or ord(c) in (0xFFFE, 0xFFFF) for c in name):
        raise AppError("Page names cannot contain control characters or invalid Unicode.")
    if not isinstance(url, str) or len(url) > 2048 or any(c.isspace() or ord(c) < 32 or ord(c) > 126 for c in url):
        raise AppError("Enter a valid Facebook profile URL.")
    try:
        parsed = urlsplit(url)
        params = parse_qs(parsed.query)
        ids = params.get("id", [])
        allowed = (parsed.scheme in ("http", "https")
                   and parsed.netloc.lower() in ("facebook.com", "www.facebook.com")
                   and parsed.path == "/profile.php" and not parsed.fragment
                   and len(ids) == 1 and re.fullmatch(r"[1-9][0-9]{4,29}", ids[0]))
    except ValueError:
        allowed = False
    if not allowed:
        raise AppError("Use http(s)://www.facebook.com/profile.php?id= followed by a numeric profile ID.")
    return name, url, ids[0], count(data.get("target"), "Daily target")


def password_hash(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 1024:
        raise AppError("Use a password of 12–1024 characters.")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), PASSWORD_ITERATIONS)
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt}${digest.hex()}"


def check_password(password, stored):
    try:
        _, iterations, salt, digest = stored.split("$")
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations)).hex()
        return hmac.compare_digest(actual, digest)
    except (ValueError, AttributeError):
        return False


def totals(rows):
    target = sum(row["target"] for row in rows)
    actual = sum(row["actual"] for row in rows)
    # Over-posting on one page does not complete another page's outstanding work.
    remaining = sum(max(row["target"] - row["actual"], 0) for row in rows)
    credited = target - remaining
    return {"target": target, "actual": actual, "remaining": remaining,
            "completion": round(100 * credited / target, 1) if target else 100.0,
            "reached": sum(row["actual"] >= row["target"] for row in rows), "pages": len(rows)}


class Store:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY)")
            for migration in sorted((ROOT / "migrations").glob("*.sql")):
                if not db.execute("SELECT 1 FROM schema_migrations WHERE name=?", (migration.name,)).fetchone():
                    # The transaction includes the schema version marker.
                    script = migration.read_text(encoding="utf-8")
                    name = migration.name.replace("'", "''")
                    db.executescript(f"BEGIN IMMEDIATE;\n{script}\nINSERT INTO schema_migrations VALUES ('{name}');\nCOMMIT;")
        self.seed()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def seed(self):
        supplied = json.loads((ROOT / "seed_pages.json").read_text(encoding="utf-8"))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for item in supplied:
                name, url, profile_id, target = page_values(item)
                if db.execute("SELECT 1 FROM seed_registry WHERE seed_key=?", (profile_id,)).fetchone():
                    continue
                existing = db.execute("SELECT id FROM pages WHERE profile_id=?", (profile_id,)).fetchone()
                page_id = existing["id"] if existing else db.execute(
                    "INSERT INTO pages(name,url,profile_id,target) VALUES (?,?,?,?)",
                    (name, url, profile_id, target)).lastrowid
                db.execute("INSERT INTO seed_registry VALUES (?,?)", (profile_id, page_id))

    def create_user(self, username, password, role="admin", *, passwordless=False):
        if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", username):
            raise AppError("Username must be 3–64 letters, numbers, dots, underscores, or hyphens.")
        if role not in ("admin", "editor", "viewer"):
            raise AppError("Unknown role.")
        if passwordless and role != "viewer":
            raise AppError("Only read-only viewer accounts can use passwordless access.")
        hashed = "" if passwordless else password_hash(password)
        try:
            with self.connect() as db:
                db.execute("INSERT INTO users(username,password_hash,role,passwordless) VALUES (?,?,?,?)", (username, hashed, role, int(passwordless)))
        except sqlite3.IntegrityError:
            raise AppError("That username already exists.", 409) from None

    def needs_setup(self):
        with self.connect() as db:
            return not db.execute("SELECT 1 FROM users LIMIT 1").fetchone()

    def shared_viewer(self):
        with self.connect() as db:
            row = db.execute("SELECT username FROM users WHERE role='viewer' AND passwordless=1 ORDER BY id LIMIT 1").fetchone()
            return row["username"] if row else None

    def create_first_admin(self, username, password, *, with_public_viewer=False):
        if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{3,64}", username):
            raise AppError("Username must be 3–64 letters, numbers, dots, underscores, or hyphens.")
        hashed = password_hash(password)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                raise AppError("Account setup is already complete. Please sign in.", 409)
            db.execute("INSERT INTO users(username,password_hash,role) VALUES (?,?,'admin')", (username, hashed))
            if with_public_viewer:
                viewer_name = "viewer" if username.lower() != "viewer" else "report-viewer"
                db.execute("INSERT INTO users(username,password_hash,role,passwordless) VALUES (?,'','viewer',1)", (viewer_name,))

    def reserve_login_attempt(self, client_key):
        key = hashlib.sha256(client_key.encode()).hexdigest()
        now = int(time.time())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM login_limits WHERE started_at<=?", (now - 900,))
            row = db.execute("SELECT attempts FROM login_limits WHERE client_key=?", (key,)).fetchone()
            if row and row["attempts"] >= 10:
                raise AppError("Too many sign-in attempts. Try again in 15 minutes.", 429)
            if row:
                db.execute("UPDATE login_limits SET attempts=attempts+1 WHERE client_key=?", (key,))
            else:
                db.execute("INSERT INTO login_limits VALUES (?,1,?)", (key, now))

    def clear_login_attempts(self, client_key):
        with self.connect() as db:
            db.execute("DELETE FROM login_limits WHERE client_key=?", (hashlib.sha256(client_key.encode()).hexdigest(),))

    def pages(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT id,name,url,profile_id,target FROM pages WHERE active=1 ORDER BY id")]

    def save_page(self, data, page_id=None):
        values = page_values(data)
        try:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if page_id is None:
                    archived = db.execute("SELECT id FROM pages WHERE profile_id=? AND active=0", (values[2],)).fetchone()
                    if archived:
                        page_id = archived["id"]
                        db.execute("UPDATE pages SET name=?,url=?,profile_id=?,target=?,active=1,updated_at=CURRENT_TIMESTAMP WHERE id=?", (*values, page_id))
                    else:
                        page_id = db.execute("INSERT INTO pages(name,url,profile_id,target) VALUES (?,?,?,?)", values).lastrowid
                else:
                    changed = db.execute("UPDATE pages SET name=?,url=?,profile_id=?,target=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1", (*values, page_id)).rowcount
                    if not changed:
                        raise AppError("Page not found.", 404)
        except sqlite3.IntegrityError:
            raise AppError("A page with this Facebook profile ID already exists.", 409) from None
        return page_id

    def delete_page(self, page_id):
        with self.connect() as db:
            if not db.execute("UPDATE pages SET active=0,updated_at=CURRENT_TIMESTAMP WHERE id=? AND active=1", (page_id,)).rowcount:
                raise AppError("Page not found.", 404)

    @staticmethod
    def _report(db, report_date):
        report = db.execute("SELECT date,revision,updated_at FROM reports WHERE date=?", (report_date,)).fetchone()
        if report:
            rows = [dict(row) for row in db.execute(
                "SELECT page_id,page_name AS name,profile_url AS url,target,actual FROM report_rows WHERE report_date=? ORDER BY page_id", (report_date,))]
        else:
            rows = [dict(row) for row in db.execute("SELECT id AS page_id,name,url,target,0 AS actual FROM pages WHERE active=1 ORDER BY id")]
        return {"date": report_date, "saved": bool(report), "revision": report["revision"] if report else 0,
                "updated_at": report["updated_at"] if report else None, "rows": rows, "totals": totals(rows)}

    def report(self, report_date):
        valid_date(report_date)
        with self.connect() as db:
            return self._report(db, report_date)

    def save_report(self, data, user_id):
        report_date = valid_date(data.get("date"))
        revision = data.get("revision")
        if type(revision) is not int or revision < 0:
            raise AppError("A report revision is required. Reload this report.")
        entries = data.get("rows")
        if not isinstance(entries, list) or len(entries) > 10000:
            raise AppError("Invalid report rows.")
        actuals = {}
        for row in entries:
            if not isinstance(row, dict) or type(row.get("page_id")) is not int or row["page_id"] in actuals:
                raise AppError("Invalid or duplicate report page.")
            actuals[row["page_id"]] = count(row.get("actual"), "Actual posts")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._report(db, report_date)
            if revision != current["revision"]:
                raise AppError("This report was changed in another session. Reload it before saving.", 409)
            if set(actuals) != {row["page_id"] for row in current["rows"]}:
                raise AppError("The page list has changed. Reload this report before saving.", 409)
            if current["saved"]:
                db.execute("UPDATE reports SET revision=revision+1,updated_by=?,updated_at=CURRENT_TIMESTAMP WHERE date=?", (user_id, report_date))
                for page_id, actual in actuals.items():
                    db.execute("UPDATE report_rows SET actual=? WHERE report_date=? AND page_id=?", (actual, report_date, page_id))
            else:
                db.execute("INSERT INTO reports(date,updated_by) VALUES (?,?)", (report_date, user_id))
                db.executemany("INSERT INTO report_rows(report_date,page_id,page_name,profile_url,target,actual) VALUES (?,?,?,?,?,?)",
                               [(report_date, r["page_id"], r["name"], r["url"], r["target"], actuals[r["page_id"]]) for r in current["rows"]])
            return self._report(db, report_date)

    def history(self, month):
        if not isinstance(month, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month) or month.startswith("0000"):
            raise AppError("Use a month in YYYY-MM format.")
        with self.connect() as db:
            result = []
            for row in db.execute("SELECT date FROM reports WHERE date LIKE ? ORDER BY date DESC", (month + "-%",)):
                report = self._report(db, row["date"])
                result.append({"date": report["date"], "updated_at": report["updated_at"], **report["totals"]})
        aggregate = {key: sum(row[key] for row in result) for key in ("target", "actual", "remaining", "reached", "pages")}
        aggregate["completion"] = round(100 * (aggregate["target"] - aggregate["remaining"]) / aggregate["target"], 1) if aggregate["target"] else (100 if result else 0)
        return {"month": month, "days": len(result), "reports": result, "totals": aggregate}


def export_rows(report):
    yield ["Report Date", "Page Name", "Facebook Profile URL", "Target Posts", "Actual Posts", "Status"]
    for row in report["rows"]:
        yield [report["date"], row["name"], row["url"], row["target"], row["actual"],
               "Reached" if row["actual"] >= row["target"] else "Below target"]


def csv_export(report):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    for row in export_rows(report):
        # Prevent spreadsheet formula execution when opening user-edited names.
        writer.writerow(["'" + v if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@")) else v for v in row])
    return output.getvalue().encode("utf-8-sig")


def xlsx_export(report):
    """Write actual Office Open XML, using inline strings to prevent formulas."""
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    xml_rows, links, relationships = [], [], []
    for i, row in enumerate(export_rows(report), 1):
        cells = []
        for j, value in enumerate(row):
            ref = f"{chr(65 + j)}{i}"
            if isinstance(value, int):
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                cleaned = "".join(c for c in value if ord(c) >= 32 or c in "\t\n\r")
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(cleaned)}</t></is></c>')
        xml_rows.append(f'<row r="{i}">{"".join(cells)}</row>')
        if i > 1:
            link_id = f"rId{i}"
            links.append(f'<hyperlink ref="C{i}" r:id="{link_id}"/>')
            relationships.append(f'<Relationship Id="{link_id}" Type="{rel_ns}/hyperlink" Target="{escape(row[2], {chr(34): "&quot;"})}" TargetMode="External"/>')
    worksheet = (f'<worksheet xmlns="{ns}" xmlns:r="{rel_ns}"><sheetViews><sheetView workbookViewId="0">'
                 '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
                 '<cols><col min="1" max="1" width="15" customWidth="1"/><col min="2" max="2" width="32" customWidth="1"/>'
                 '<col min="3" max="3" width="70" customWidth="1"/><col min="4" max="6" width="18" customWidth="1"/></cols>'
                 f'<sheetData>{"".join(xml_rows)}</sheetData><autoFilter ref="A1:F{len(xml_rows)}"/>'
                 + (f'<hyperlinks>{"".join(links)}</hyperlinks>' if links else '') + '</worksheet>')
    files = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        "_rels/.rels": f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="{rel_ns}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": f'<workbook xmlns="{ns}" xmlns:r="{rel_ns}"><sheets><sheet name="Daily report" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="{rel_ns}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/worksheets/sheet1.xml": worksheet,
        "xl/worksheets/_rels/sheet1.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + "".join(relationships) + '</Relationships>',
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>' + content)
    return output.getvalue()


class ReportServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, store, secure_cookie=False):
        self.store = store
        self.secure_cookie = secure_cookie
        self.setup_token = secrets.token_urlsafe(32)
        self.allow_local_setup = address[0] == "127.0.0.1"
        self.allow_hosted_setup = False
        self.enable_public_viewer = False
        # A real hash also equalizes unknown-user password checks.
        self.dummy_hash = password_hash(secrets.token_urlsafe(32))
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "DailyReport"

    def log_message(self, fmt, *args):
        # Never print request bodies, passwords, cookies, or tokens.
        super().log_message(fmt, *args)

    def respond(self, status, body, content_type="application/json; charset=utf-8", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        for key, value in {
            "Content-Type": content_type, "Content-Length": str(len(body)), "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "same-origin",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            **(headers or {})
        }.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        if self.headers.get_content_type() != "application/json":
            raise AppError("Send JSON with Content-Type: application/json.", 415)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1_000_000:
                raise ValueError
            value = json.loads(self.rfile.read(length))
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (ValueError, UnicodeDecodeError):
            raise AppError("Invalid JSON request body.") from None

    def same_origin(self):
        # JSON + CSRF tokens protect state changes; also reject cross-site origins.
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlsplit(origin)
            if parsed.scheme not in ("http", "https") or parsed.netloc != self.headers.get("Host"):
                raise AppError("Cross-origin requests are not allowed.", 403)
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise AppError("Cross-site requests are not allowed.", 403)

    def session(self, mutate=False):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
            token = cookie["report_session"].value
        except (KeyError, ValueError, CookieError):
            raise AppError("Please sign in.", 401) from None
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.server.store.connect() as db:
            row = db.execute("SELECT s.*,u.username,u.role FROM sessions s JOIN users u ON u.id=s.user_id WHERE token_hash=? AND expires_at>?", (digest, int(time.time()))).fetchone()
        if not row:
            raise AppError("Your session expired. Please sign in.", 401)
        if mutate:
            self.same_origin()
            if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), row["csrf_token"]):
                raise AppError("Invalid security token. Reload and try again.", 403)
        return dict(row)

    @staticmethod
    def require_role(session, roles):
        if session["role"] not in roles:
            raise AppError("Your account does not have permission for this action.", 403)

    def cookie(self, token, max_age):
        return f"report_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={max_age}" + ("; Secure" if self.server.secure_cookie else "")

    def login(self):
        self.same_origin()
        data = self.body()
        username, password = data.get("username"), data.get("password", "")
        if not isinstance(username, str) or not isinstance(password, str) or len(username) > 64 or len(password) > 1024:
            raise AppError("Invalid username or password.", 401)
        key, now = self.client_address[0], time.time()
        self.server.store.reserve_login_attempt(key)
        with self.server.store.connect() as db:
            user = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            correct = bool(user and user["role"] == "viewer" and user["passwordless"] == 1)
            if not correct:
                correct = check_password(password, user["password_hash"] if user else self.server.dummy_hash)
            if not user or not correct:
                raise AppError("Invalid username or password.", 401)
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            db.execute("DELETE FROM sessions WHERE expires_at<=?", (int(now),))
            db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), user["id"], csrf, int(now) + SESSION_SECONDS))
        self.server.store.clear_login_attempts(key)
        self.respond(200, {"user": {"username": user["username"], "role": user["role"]}, "csrf": csrf, "today": today()}, headers={"Set-Cookie": self.cookie(token, SESSION_SECONDS)})

    def first_run_setup(self, method):
        self.same_origin()
        local = self.server.allow_local_setup and self.client_address[0] == "127.0.0.1"
        hosted = self.server.allow_hosted_setup
        needed = self.server.store.needs_setup()
        if method == "GET":
            self.respond(200, {"needed": needed, "enabled": local or hosted, "viewer": self.server.store.shared_viewer(),
                               "token_required": hosted and not local and needed,
                               "token": self.server.setup_token if local and needed else None})
            return
        if not (local or hosted):
            raise AppError("Set a private SETUP_TOKEN in Vercel settings, or create your administrator locally with python server.py --create-user.", 403)
        if not hmac.compare_digest(self.headers.get("X-Setup-Token", ""), self.server.setup_token):
            raise AppError("Invalid setup token. Reload this page.", 403)
        if not needed:
            raise AppError("Account setup is already complete. Please sign in.", 409)
        data = self.body()
        self.server.store.create_first_admin(data.get("username"), data.get("password"),
                                             with_public_viewer=self.server.enable_public_viewer)
        self.respond(201, {"ok": True, "viewer": self.server.store.shared_viewer()})

    def dispatch(self, method):
        try:
            parsed = urlsplit(self.path)
            path, params = parsed.path, parse_qs(parsed.query)
            store = self.server.store
            if method == "GET" and path in ("/", "/index.html", "/app.js", "/styles.css", "/favicon.svg"):
                filename = "index.html" if path == "/" else path[1:]
                content_type = {"html": "text/html; charset=utf-8", "js": "text/javascript; charset=utf-8", "css": "text/css; charset=utf-8", "svg": "image/svg+xml"}[filename.rsplit(".", 1)[1]]
                self.respond(200, (ROOT / filename).read_bytes(), content_type)
                return
            if method == "POST" and path == "/api/login":
                self.login()
                return
            if path == "/api/setup" and method in ("GET", "POST"):
                self.first_run_setup(method)
                return
            if not path.startswith("/api/"):
                raise AppError("Not found.", 404)
            session = self.session(mutate=method != "GET")
            if method == "GET" and path == "/api/session":
                self.respond(200, {"user": {"username": session["username"], "role": session["role"]}, "csrf": session["csrf_token"], "today": today()})
            elif method == "POST" and path == "/api/logout":
                with store.connect() as db:
                    db.execute("DELETE FROM sessions WHERE token_hash=?", (session["token_hash"],))
                self.respond(200, {"ok": True}, headers={"Set-Cookie": self.cookie("", 0)})
            elif path == "/api/pages" and method == "GET":
                pages = store.pages()
                self.respond(200, {"pages": pages, "total_pages": len(pages), "total_target": sum(p["target"] for p in pages)})
            elif path == "/api/pages" and method == "POST":
                self.require_role(session, ("admin",))
                self.respond(201, {"id": store.save_page(self.body())})
            elif re.fullmatch(r"/api/pages/[1-9][0-9]*", path) and method in ("PUT", "DELETE"):
                self.require_role(session, ("admin",))
                page_id = int(path.rsplit("/", 1)[1])
                if method == "PUT":
                    store.save_page(self.body(), page_id)
                else:
                    store.delete_page(page_id)
                self.respond(200, {"ok": True})
            elif method == "GET" and path == "/api/report":
                self.respond(200, store.report(params.get("date", [today()])[0]))
            elif method == "PUT" and path == "/api/report":
                self.require_role(session, ("admin", "editor"))
                self.respond(200, store.save_report(self.body(), session["user_id"]))
            elif method == "GET" and path == "/api/history":
                self.respond(200, store.history(params.get("month", [today()[:7]])[0]))
            elif method == "GET" and path in ("/api/export.csv", "/api/export.xlsx"):
                report = store.report(params.get("date", [today()])[0])
                if not report["saved"]:
                    raise AppError("Save this report before exporting.", 409)
                is_excel = path.endswith("xlsx")
                extension = "xlsx" if is_excel else "csv"
                self.respond(200, xlsx_export(report) if is_excel else csv_export(report),
                             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if is_excel else "text/csv; charset=utf-8",
                             {"Content-Disposition": f'attachment; filename="facebook-report-{report["date"]}.{extension}"'})
            else:
                raise AppError("Not found.", 404)
        except AppError as error:
            self.respond(error.status, {"error": str(error)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            import traceback
            traceback.print_exc()
            self.respond(500, {"error": "An unexpected server error occurred. Check the server console."})

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_DELETE(self):
        self.dispatch("DELETE")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "reports.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--create-user", action="store_true", help="Create an account interactively, then exit")
    parser.add_argument("--role", choices=("admin", "editor", "viewer"), default="admin")
    parser.add_argument("--passwordless", action="store_true", help="Create a viewer account that does not require a password")
    parser.add_argument("--init-only", action="store_true", help="Apply migrations and idempotent seed, then exit")
    parser.add_argument("--secure-cookie", action="store_true", help="Require HTTPS for cookies when behind a TLS reverse proxy")
    args = parser.parse_args()
    if args.passwordless and (not args.create_user or args.role != "viewer"):
        parser.error("--passwordless requires --create-user --role viewer")
    if os.environ.get("DATABASE_URL"):
        from postgres_store import PostgresStore
        store = PostgresStore(os.environ["DATABASE_URL"])
    else:
        store = Store(args.db)
    if args.create_user:
        try:
            username = input("Username: ").strip()
            password = ""
            if not args.passwordless:
                password = getpass.getpass("Password (at least 12 characters): ")
                if password != getpass.getpass("Confirm password: "):
                    raise AppError("Passwords do not match.")
            store.create_user(username, password, args.role, passwordless=args.passwordless)
        except AppError as error:
            parser.exit(1, f"{error}\n")
        print(f"Created {args.role} account: {username}")
        return
    if args.init_only:
        pages = store.pages()
        print(f"Database ready: {len(pages)} pages, {sum(p['target'] for p in pages)} daily target posts.")
        return
    server = ReportServer((args.host, args.port), store, args.secure_cookie)
    print(f"Daily Facebook Report: http://{args.host}:{server.server_port}", flush=True)
    print("Report timezone: Asia/Bangkok (UTC+07:00). Press Ctrl+C to stop.", flush=True)
    if store.needs_setup():
        print("First run: open the local URL to create your administrator account." if server.allow_local_setup
              else "Create your administrator locally: python server.py --create-user", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
