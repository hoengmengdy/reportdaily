import csv
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import date, timedelta
import http.client
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import AppError, ROOT, ReportServer, Store, csv_export, page_values, today, totals, xlsx_export


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "test.sqlite3"
        self.store = Store(self.path)
        with self.store.connect() as db:
            self.user_id = db.execute("INSERT INTO users(username,password_hash,role) VALUES ('tester','unused','admin')").lastrowid

    def save(self, day=None, actual=0):
        report = self.store.report(day or today())
        return self.store.save_report({"date": report["date"], "revision": report["revision"],
                                       "rows": [{"page_id": r["page_id"], "actual": actual} for r in report["rows"]]}, self.user_id)

    def test_seed_exact_unicode_urls_and_totals(self):
        supplied = json.loads((ROOT / "seed_pages.json").read_text(encoding="utf-8"))
        pages = self.store.pages()
        self.assertEqual(len(pages), 14)
        self.assertEqual(sum(p["target"] for p in pages), 82)
        self.assertEqual([{k: p[k] for k in ("name", "url", "target")} for p in pages], supplied)
        self.assertEqual(pages[2]["name"], "រឿង AI")
        self.assertEqual(pages[10]["name"], "ÄĎMIN BL TËÄM 021")

    def test_seed_does_not_overwrite_edits_or_restore_deleted_pages(self):
        pages = self.store.pages()
        changed = {**pages[0], "name": "Edited រឿង", "target": 42, "url": "https://facebook.com/profile.php?id=1234567890"}
        self.store.save_page(changed, pages[0]["id"])
        self.store.delete_page(pages[1]["id"])
        for _ in range(3):
            self.store = Store(self.path)
        current = self.store.pages()
        self.assertEqual(len(current), 13)
        self.assertEqual(current[0]["name"], changed["name"])
        self.assertEqual(current[0]["target"], 42)
        self.assertEqual(current[0]["url"], changed["url"])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM pages").fetchone()[0], 14)
            self.assertEqual(db.execute("SELECT count(*) FROM schema_migrations").fetchone()[0], 3)

    def test_duplicate_ids_and_restore(self):
        page = self.store.pages()[0]
        duplicate = {**page, "url": page["url"].replace("https://www.", "http://") + "&ref=share"}
        with self.assertRaises(AppError) as error:
            self.store.save_page(duplicate)
        self.assertEqual(error.exception.status, 409)
        self.store.delete_page(page["id"])
        restored_id = self.store.save_page({**page, "name": "Restored"})
        self.assertEqual(restored_id, page["id"])
        self.assertEqual(len(self.store.pages()), 14)

    def test_add_edit_and_delete_update_live_totals(self):
        new_id = self.store.save_page({"name": "ថ្មី 🪷", "url": "https://facebook.com/profile.php?id=123456789", "target": 7})
        self.assertEqual(sum(p["target"] for p in self.store.pages()), 89)
        self.store.save_page({"name": "ថ្មី 🪷", "url": "https://facebook.com/profile.php?id=123456789", "target": 9}, new_id)
        self.assertEqual(self.store.report(today())["totals"]["target"], 91)
        self.store.delete_page(new_id)
        self.assertEqual(self.store.report(today())["totals"]["target"], 82)

    def test_reports_survive_restart_and_keep_snapshots(self):
        yesterday = (date.fromisoformat(today()) - timedelta(days=1)).isoformat()
        saved = self.save(yesterday, 2)
        first = self.store.pages()[0]
        self.store.save_page({**first, "name": "New name", "target": 99}, first["id"])
        self.store.delete_page(first["id"])
        reopened = Store(self.path).report(yesterday)
        self.assertEqual(reopened, saved)
        self.assertEqual(reopened["rows"][0]["name"], "Lilyy")
        self.assertEqual(reopened["totals"]["actual"], 28)
        self.assertEqual(reopened["totals"]["target"], 82)
        self.assertEqual(len(self.store.report(today())["rows"]), 13)

    def test_actuals_can_update_without_changing_snapshot(self):
        saved = self.save(actual=1)
        page = self.store.pages()[0]
        self.store.save_page({**page, "target": 88}, page["id"])
        updated = self.save(actual=3)
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(updated["totals"]["target"], 82)
        self.assertEqual(updated["totals"]["actual"], 42)
        self.assertEqual(updated["rows"][0]["target"], saved["rows"][0]["target"])

    def test_stale_report_revision_is_rejected(self):
        draft = self.store.report(today())
        payload = {"date": today(), "revision": 0, "rows": [{"page_id": r["page_id"], "actual": 1} for r in draft["rows"]]}
        self.store.save_report(payload, self.user_id)
        with self.assertRaises(AppError) as error:
            self.store.save_report(payload, self.user_id)
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.store.report(today())["revision"], 1)

    def test_changed_page_roster_rejects_stale_draft(self):
        draft = self.store.report(today())
        self.store.delete_page(draft["rows"][0]["page_id"])
        with self.assertRaises(AppError) as error:
            self.store.save_report({"date": today(), "revision": 0, "rows": [{"page_id": r["page_id"], "actual": 1} for r in draft["rows"]]}, self.user_id)
        self.assertEqual(error.exception.status, 409)
        self.assertFalse(self.store.report(today())["saved"])

    def test_duplicate_and_missing_report_rows_are_rejected(self):
        for rows in ([], [{"page_id": 1, "actual": 2}] * 14):
            with self.subTest(rows=len(rows)), self.assertRaises(AppError):
                self.store.save_report({"date": today(), "revision": 0, "rows": rows}, self.user_id)

    def test_monthly_history_aggregates_saved_days_only(self):
        self.save(actual=1)
        history = self.store.history(today()[:7])
        self.assertEqual(history["days"], 1)
        self.assertEqual(history["totals"]["target"], 82)
        self.assertEqual(history["totals"]["actual"], 14)
        self.assertEqual(history["totals"]["remaining"], 68)
        self.assertEqual(history["reports"][0]["date"], today())

    def test_monthly_history_combines_days(self):
        self.save("2020-02-01", 1)
        self.save("2020-02-02", 3)
        self.save("2020-03-01", 5)
        history = self.store.history("2020-02")
        self.assertEqual(history["days"], 2)
        self.assertEqual(history["totals"]["target"], 164)
        self.assertEqual(history["totals"]["actual"], 56)
        self.assertEqual([r["date"] for r in history["reports"]], ["2020-02-02", "2020-02-01"])

    def test_validation_rejects_invalid_dates_and_counts(self):
        for day in ("2020-1-01", "2020-02-30", "2099-01-01", "garbage", None):
            with self.subTest(day=day), self.assertRaises(AppError):
                self.store.report(day)
        for value in (-1, 1.2, True, "3", 1000001, None):
            with self.subTest(value=value), self.assertRaises(AppError):
                self.store.save_page({"name": "Test", "url": "https://facebook.com/profile.php?id=12345678", "target": value})

    def test_profile_url_validation(self):
        bad_urls = ["javascript:alert(1)", "https://facebook.com.evil.test/profile.php?id=12345678",
                    "https://facebook.com@evil.test/profile.php?id=12345678", "https://facebook.com/profile.php?id=abc",
                    "https://facebook.com/profile.php?id=12345678&id=87654321", "https://facebook.com/profile.php?id=12345678#bad",
                    "ftp://facebook.com/profile.php?id=12345678", "https://facebook.com:123/profile.php?id=12345678",
                    "https://facebook.com/profile.php?id=12345678&x=\x00"]
        for url in bad_urls:
            with self.subTest(url=url), self.assertRaises(AppError):
                page_values({"name": "Test", "url": url, "target": 3})

    def test_invalid_unicode_rejected_without_affecting_khmer(self):
        for name in ("\ud800", "bad\uffff", "bad\x00"):
            with self.subTest(name=repr(name)), self.assertRaises(AppError):
                page_values({"name": name, "url": "https://facebook.com/profile.php?id=12345678", "target": 3})
        self.assertEqual(page_values({"name": "រឿង AI 🪷", "url": "https://facebook.com/profile.php?id=12345678", "target": 3})[0], "រឿង AI 🪷")

    def test_overposting_does_not_mask_other_pages(self):
        result = totals([{"target": 3, "actual": 100}, {"target": 13, "actual": 0}])
        self.assertEqual(result["actual"], 100)
        self.assertEqual(result["remaining"], 13)
        self.assertEqual(result["completion"], 18.8)
        self.assertEqual(totals([{"target": 0, "actual": 0}])["completion"], 100)

    def test_csv_unicode_headers_values_and_formula_protection(self):
        report = self.save(actual=2)
        report["rows"][0]["name"] = " =HYPERLINK(\"bad\")"
        blob = csv_export(report)
        self.assertTrue(blob.startswith(b"\xef\xbb\xbf"))
        rows = list(csv.reader(io.StringIO(blob.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 15)
        self.assertEqual(rows[1][1], "' =HYPERLINK(\"bad\")")
        self.assertEqual(rows[3][1], "រឿង AI")
        self.assertEqual(rows[11][1], "ÄĎMIN BL TËÄM 021")
        self.assertEqual(rows[1][3:], ["3", "2", "Below target"])

    def test_xlsx_is_valid_zip_xml_with_clickable_links_and_strings(self):
        report = self.save(actual=3)
        report["rows"][0]["name"] = '=1+2 <&> "'
        with zipfile.ZipFile(io.BytesIO(xlsx_export(report))) as archive:
            self.assertIsNone(archive.testzip())
            for name in archive.namelist():
                ET.fromstring(archive.read(name))
            ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
            self.assertEqual(len(sheet.findall("s:sheetData/s:row", ns)), 15)
            self.assertEqual(len(sheet.findall("s:hyperlinks/s:hyperlink", ns)), 14)
            self.assertEqual(sheet.find('.//s:c[@r="B2"]', ns).attrib["t"], "inlineStr")
            self.assertEqual(sheet.find('.//s:c[@r="B2"]/s:is/s:t', ns).text, '=1+2 <&> "')
            self.assertIsNone(sheet.find(".//s:f", ns))

    def test_foreign_keys_and_target_constraints(self):
        with self.store.connect() as db:
            self.assertEqual(db.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE pages SET target=-1 WHERE id=1")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE users SET passwordless=1 WHERE role='admin'")

    def test_passwordless_creation_only_allows_viewer_role(self):
        for role in ("admin", "editor"):
            with self.subTest(role=role), self.assertRaises(AppError):
                self.store.create_user("unsafe-" + role, "", role, passwordless=True)
        self.store.create_user("shared", "", "viewer", passwordless=True)
        self.assertEqual(self.store.shared_viewer(), "shared")
        with self.store.connect() as db:
            user = db.execute("SELECT * FROM users WHERE username='shared'").fetchone()
            self.assertEqual(user["passwordless"], 1)
            self.assertEqual(user["password_hash"], "")

    def test_passwordless_migration_preserves_existing_accounts(self):
        old_path = Path(self.temp.name) / "old.sqlite3"
        with closing(sqlite3.connect(old_path)) as db, db:
            db.executescript((ROOT / "migrations" / "001_initial.sql").read_text(encoding="utf-8"))
            db.execute("CREATE TABLE schema_migrations (name TEXT PRIMARY KEY)")
            db.execute("INSERT INTO schema_migrations VALUES ('001_initial.sql')")
            db.execute("INSERT INTO users(username,password_hash,role) VALUES ('owner','original-admin-hash','admin')")
            db.execute("INSERT INTO users(username,password_hash,role) VALUES ('viewer','original-viewer-hash','viewer')")
        migrated = Store(old_path)
        with migrated.connect() as db:
            users = db.execute("SELECT password_hash,passwordless FROM users ORDER BY id").fetchall()
            self.assertEqual([(u["password_hash"], u["passwordless"]) for u in users],
                             [("original-admin-hash", 0), ("original-viewer-hash", 0)])
        self.assertIsNone(migrated.shared_viewer())

    def test_empty_report_can_be_saved_and_exported(self):
        for page in self.store.pages():
            self.store.delete_page(page["id"])
        saved = self.save()
        self.assertEqual(saved["rows"], [])
        self.assertEqual(saved["totals"]["completion"], 100)
        with zipfile.ZipFile(io.BytesIO(xlsx_export(saved))) as archive:
            sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
            ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
            self.assertIsNone(sheet.find("s:hyperlinks", ns))
        self.assertEqual(len(list(csv.reader(io.StringIO(csv_export(saved).decode("utf-8-sig"))))), 1)


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.store = Store(Path(cls.temp.name) / "http.sqlite3")
        for role in ("admin", "editor", "viewer"):
            cls.store.create_user(role, "a-long-test-password-42", role)
        cls.store.create_user("shared-viewer", "", "viewer", passwordless=True)
        cls.server = ReportServer(("127.0.0.1", 0), cls.store)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def setUp(self):
        self.cookie = ""
        self.csrf = ""
        with self.store.connect() as db:
            db.execute("DELETE FROM login_limits")

    def request(self, method, path, data=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        request_headers = {"Content-Type": "application/json", "Cookie": self.cookie, "X-CSRF-Token": self.csrf, **(headers or {})}
        body = json.dumps(data).encode() if data is not None else None
        connection.request(method, path, body, request_headers)
        response = connection.getresponse()
        payload, response_headers, status = response.read(), dict(response.getheaders()), response.status
        connection.close()
        content = json.loads(payload) if response_headers.get("Content-Type", "").startswith("application/json") else payload
        return status, content, response_headers

    def login(self, role="admin"):
        status, result, headers = self.request("POST", "/api/login", {"username": role, "password": "a-long-test-password-42"})
        self.assertEqual(status, 200)
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.csrf = result["csrf"]
        return headers

    def test_static_and_database_not_exposed(self):
        for path in ("/", "/index.html", "/app.js", "/styles.css", "/favicon.svg"):
            self.assertEqual(self.request("GET", path)[0], 200)
        for path in ("/server.py", "/data/reports.sqlite3", "/seed_pages.json", "/../server.py"):
            self.assertEqual(self.request("GET", path)[0], 404)

    def test_authentication_required(self):
        for path in ("/api/pages", "/api/report", "/api/history", "/api/export.csv", "/api/export.xlsx"):
            self.assertEqual(self.request("GET", path)[0], 401)
        self.assertEqual(self.request("POST", "/api/login", {"username": "admin", "password": "wrong"})[0], 401)

    def test_login_cookie_session_logout(self):
        headers = self.login()
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        status, result, _ = self.request("GET", "/api/session")
        self.assertEqual(status, 200)
        self.assertEqual(result["user"], {"username": "admin", "role": "admin"})
        self.assertEqual(self.request("POST", "/api/logout")[0], 200)
        self.assertEqual(self.request("GET", "/api/session")[0], 401)

    def test_csrf_and_cross_origin_protection(self):
        self.login()
        page = {"name": "test", "url": "https://facebook.com/profile.php?id=999999999", "target": 3}
        self.assertEqual(self.request("POST", "/api/pages", page, {"X-CSRF-Token": "wrong"})[0], 403)
        self.assertEqual(self.request("POST", "/api/pages", page, {"Origin": "https://evil.test"})[0], 403)
        self.assertEqual(self.request("POST", "/api/login", {}, {"Origin": "https://evil.test"})[0], 403)

    def test_viewer_is_read_only_and_editor_cannot_manage_pages(self):
        self.login("viewer")
        self.assertEqual(self.request("GET", "/api/pages")[0], 200)
        self.assertEqual(self.request("PUT", "/api/report", {})[0], 403)
        self.assertEqual(self.request("DELETE", "/api/pages/1")[0], 403)
        self.login("editor")
        self.assertEqual(self.request("POST", "/api/pages", {})[0], 403)
        status, report, _ = self.request("GET", "/api/report?date=2020-01-01")
        self.assertEqual(status, 200)
        report["rows"] = [{"page_id": r["page_id"], "actual": 3} for r in report["rows"]]
        self.assertEqual(self.request("PUT", "/api/report", report)[0], 200)

    def test_admin_crud_and_exports(self):
        self.login()
        data = {"name": "Test រឿង", "url": "https://facebook.com/profile.php?id=987654321", "target": 3}
        status, result, _ = self.request("POST", "/api/pages", data)
        self.assertEqual(status, 201)
        page_id = result["id"]
        self.assertEqual(self.request("POST", "/api/pages", data)[0], 409)
        self.assertEqual(self.request("PUT", f"/api/pages/{page_id}", {**data, "target": 5})[0], 200)
        status, report, _ = self.request("GET", "/api/report?date=2020-01-02")
        self.assertEqual(status, 200)
        report["rows"] = [{"page_id": r["page_id"], "actual": 3} for r in report["rows"]]
        self.assertEqual(self.request("PUT", "/api/report", report)[0], 200)
        for extension in ("csv", "xlsx"):
            status, blob, headers = self.request("GET", f"/api/export.{extension}?date=2020-01-02")
            self.assertEqual(status, 200)
            self.assertIn(f"facebook-report-2020-01-02.{extension}", headers["Content-Disposition"])
            self.assertGreater(len(blob), 100)
        self.assertEqual(self.request("DELETE", f"/api/pages/{page_id}")[0], 200)
        self.assertEqual(self.request("DELETE", f"/api/pages/{page_id}")[0], 404)

    def test_unsaved_export_rejected(self):
        self.login()
        self.assertEqual(self.request("GET", "/api/export.csv?date=2019-01-01")[0], 409)

    def test_passwordless_viewer_can_read_but_cannot_mutate(self):
        status, access, _ = self.request("GET", "/api/setup")
        self.assertEqual(status, 200)
        self.assertEqual(access["viewer"], "shared-viewer")
        status, session, headers = self.request("POST", "/api/login", {"username": "shared-viewer", "role": "admin"})
        self.assertEqual(status, 200)
        self.assertEqual(session["user"]["role"], "viewer")
        self.cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.csrf = session["csrf"]
        for path in ("/api/pages", "/api/report", "/api/history", "/api/export.csv?date=2020-01-02", "/api/export.xlsx?date=2020-01-02"):
            self.assertEqual(self.request("GET", path)[0], 200)
        for method, path in (("PUT", "/api/report"), ("POST", "/api/pages"), ("PUT", "/api/pages/1"), ("DELETE", "/api/pages/1")):
            self.assertEqual(self.request(method, path, {})[0], 403)
        self.assertEqual(self.request("POST", "/api/logout")[0], 200)
        self.assertEqual(self.request("GET", "/api/pages")[0], 401)

    def test_passwordless_login_does_not_bypass_protected_accounts(self):
        for role in ("admin", "editor", "viewer"):
            self.assertEqual(self.request("POST", "/api/login", {"username": role, "passwordless": True})[0], 401)
        self.login("admin")

    def test_passwordless_login_rejects_cross_origin(self):
        self.assertEqual(self.request("POST", "/api/login", {"username": "shared-viewer"}, {"Origin": "https://evil.test"})[0], 403)

    def test_invalid_json_and_content_type(self):
        self.login()
        self.assertEqual(self.request("POST", "/api/pages", [1, 2])[0], 400)
        self.assertEqual(self.request("POST", "/api/pages", {}, {"Content-Type": "text/plain"})[0], 415)

    def test_login_rate_limit(self):
        for _ in range(10):
            self.assertEqual(self.request("POST", "/api/login", {"username": "missing", "password": "wrong"})[0], 401)
        self.assertEqual(self.request("POST", "/api/login", {"username": "missing", "password": "wrong"})[0], 429)


class SetupTests(unittest.TestCase):
    request = HttpTests.request

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "setup.sqlite3")
        self.server = ReportServer(("127.0.0.1", 0), self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = self.csrf = ""

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def test_first_run_serves_page_and_creates_one_administrator(self):
        self.assertEqual(self.request("GET", "/")[0], 200)
        status, setup, _ = self.request("GET", "/api/setup")
        self.assertEqual(status, 200)
        self.assertTrue(setup["needed"])
        self.assertTrue(setup["enabled"])
        credentials = {"username": "owner", "password": "local-setup-password-42", "role": "viewer"}
        self.assertEqual(self.request("POST", "/api/setup", credentials)[0], 403)
        headers = {"X-Setup-Token": setup["token"]}
        self.assertEqual(self.request("POST", "/api/setup", {**credentials, "password": "short"}, headers)[0], 400)
        self.assertTrue(self.store.needs_setup())
        self.assertEqual(self.request("POST", "/api/setup", credentials, headers)[0], 201)
        self.assertEqual(self.request("POST", "/api/setup", credentials, headers)[0], 409)
        _, after, _ = self.request("GET", "/api/setup")
        self.assertFalse(after["needed"])
        self.assertIsNone(after["token"])
        status, result, _ = self.request("POST", "/api/login", credentials)
        self.assertEqual(status, 200)
        self.assertEqual(result["user"]["role"], "admin")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
            self.assertNotEqual(db.execute("SELECT password_hash FROM users").fetchone()[0], credentials["password"])

    def test_browser_setup_disabled_for_non_loopback_binding(self):
        self.server.allow_local_setup = False
        _, setup, _ = self.request("GET", "/api/setup")
        self.assertFalse(setup["enabled"])
        self.assertIsNone(setup["token"])
        self.assertEqual(self.request("POST", "/api/setup", {}, {"X-Setup-Token": self.server.setup_token})[0], 403)
        self.assertTrue(self.store.needs_setup())

    def test_setup_rejects_cross_origin_requests(self):
        for method in ("GET", "POST"):
            self.assertEqual(self.request(method, "/api/setup", {}, {"Origin": "https://evil.test"})[0], 403)
        self.assertTrue(self.store.needs_setup())

    def test_concurrent_setup_can_only_create_one_admin(self):
        def create(username):
            try:
                self.store.create_first_admin(username, "concurrent-setup-password")
                return 201
            except AppError as error:
                return error.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create, ("owner-one", "owner-two")))
        self.assertEqual(sorted(results), [201, 409])


if __name__ == "__main__":
    unittest.main()
