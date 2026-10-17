from contextlib import contextmanager
import importlib
from importlib.util import find_spec
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import test_app
from server import AppError, Store
from postgres_store import PgConnection, translate_sql, validate_database_url
from migrate_to_postgres import connection_input, import_snapshot, read_connection_url, snapshot


@unittest.skipUnless(find_spec("flask"), "Install requirements.txt to test the hosted adapter")
class HostedFlaskTests(test_app.HttpTests):
    """Run the same real API/permission/export checks through Vercel's entry point."""
    @classmethod
    def setUpClass(cls):
        cls.cloud = importlib.import_module("app")
        cls.temp = tempfile.TemporaryDirectory()
        cls.store = Store(Path(cls.temp.name) / "hosted.sqlite3")
        for role in ("admin", "editor", "viewer"):
            cls.store.create_user(role, "a-long-test-password-42", role)
        cls.store.create_user("shared-viewer", "", "viewer", passwordless=True)
        cls.store_patch = patch.object(cls.cloud, "get_store", return_value=cls.store)
        cls.store_patch.start()
        cls.client = cls.cloud.app.test_client(use_cookies=False)

    @classmethod
    def tearDownClass(cls):
        cls.store_patch.stop()
        cls.temp.cleanup()

    def request(self, method, path, data=None, headers=None):
        response = self.client.open(path, method=method, base_url="https://reports.example.test",
                                    data=json.dumps(data) if data is not None else None,
                                    headers={"Content-Type": "application/json", "Cookie": self.cookie,
                                             "X-CSRF-Token": self.csrf, **(headers or {})})
        content = response.get_json() if response.is_json else response.data
        return response.status_code, content, dict(response.headers)

    def test_hosted_cookies_require_https(self):
        self.assertIn("Secure", self.login()["Set-Cookie"])


@unittest.skipUnless(find_spec("flask"), "Install requirements.txt to test the hosted adapter")
class HostedSetupTests(unittest.TestCase):
    def setUp(self):
        self.cloud = importlib.import_module("app")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "setup.sqlite3")
        self.client = self.cloud.app.test_client(use_cookies=False)

    def test_missing_database_serves_page_and_returns_clear_api_error(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(self.cloud, "_store", None):
            self.assertEqual(self.client.get("/").status_code, 200)
            response = self.client.get("/api/setup")
            self.assertEqual(response.status_code, 503)
            self.assertIn("DATABASE_URL", response.get_json()["error"])
            self.assertIsNone(self.cloud._store)

    def test_hosted_setup_requires_private_token_and_creates_viewer(self):
        private_key = "test-only-private-setup-key-" + "a" * 32
        with patch.object(self.cloud, "get_store", return_value=self.store), patch.dict(os.environ, {"SETUP_TOKEN": private_key}):
            status = self.client.get("/api/setup").get_json()
            self.assertTrue(status["enabled"])
            self.assertTrue(status["token_required"])
            self.assertIsNone(status["token"])
            self.assertNotIn(private_key, json.dumps(status))
            credentials = {"username": "owner", "password": "private-owner-password"}
            self.assertEqual(self.client.post("/api/setup", json=credentials).status_code, 403)
            self.assertEqual(self.client.post("/api/setup", json=credentials, headers={"X-Setup-Token": private_key}).status_code, 201)
            self.assertEqual(self.client.post("/api/setup", json=credentials, headers={"X-Setup-Token": private_key}).status_code, 409)
            self.assertEqual(self.store.shared_viewer(), "viewer")
            self.assertEqual(self.client.post("/api/login", json={"username": "viewer"}).get_json()["user"]["role"], "viewer")

    def test_hosted_setup_disabled_without_secret_even_on_loopback(self):
        with patch.object(self.cloud, "get_store", return_value=self.store), patch.dict(os.environ, {}, clear=True):
            self.assertFalse(self.client.get("/api/setup").get_json()["enabled"])
            self.assertEqual(self.client.post("/api/setup", json={}).status_code, 403)

    def test_configuration_errors_never_reveal_connection_secrets(self):
        secret = "postgresql://owner:private-password@database.invalid/db"
        with patch.object(self.cloud, "get_store", side_effect=RuntimeError(secret)):
            response = self.client.get("/api/setup")
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("private-password", response.get_data(as_text=True))

    def test_login_limits_survive_new_store_instances(self):
        for _ in range(10):
            self.store.reserve_login_attempt("test-client")
        reopened = Store(self.store.path)
        with self.assertRaises(AppError) as error:
            reopened.reserve_login_attempt("test-client")
        self.assertEqual(error.exception.status, 429)
        reopened.clear_login_attempts("test-client")
        self.store.reserve_login_attempt("test-client")


class PostgresAdapterTests(unittest.TestCase):
    def test_database_requires_postgres_url_and_remote_tls(self):
        for value in (None, "", "sqlite:///tmp/data.db", "postgresql://db.example.test/db", "postgresql://db.example.test/db?sslmode=disable"):
            with self.subTest(value=value), self.assertRaises(AppError):
                validate_database_url(value)
        for value in ("postgresql://owner:secret@db.example.test/reports?sslmode=require", "postgresql://localhost/reports"):
            self.assertEqual(validate_database_url(value), value)

    def test_sql_uses_parameters_case_insensitive_login_and_database_lock(self):
        self.assertEqual(translate_sql("SELECT * FROM users WHERE username=?"), "SELECT * FROM users WHERE lower(username)=lower(%s)")
        self.assertIn("pg_advisory_xact_lock", translate_sql("BEGIN IMMEDIATE"))
        self.assertIn("AT TIME ZONE 'UTC'", translate_sql("UPDATE reports SET updated_at=CURRENT_TIMESTAMP WHERE date=?"))
        raw = MagicMock()
        raw.execute.return_value.rowcount = 1
        raw.execute.return_value.fetchone.return_value = {"id": 42}
        connection = PgConnection(raw, sqlite3.IntegrityError)
        values = ("រឿង AI ? % ' ", "https://facebook.com/profile.php?id=12345678", "12345678", 3)
        result = connection.execute("INSERT INTO pages(name,url,profile_id,target) VALUES (?,?,?,?)", values)
        sql, params = raw.execute.call_args.args
        self.assertTrue(sql.endswith("RETURNING id"))
        self.assertNotIn(values[0], sql)
        self.assertEqual(params, values)
        self.assertEqual(result.lastrowid, 42)


class ConnectionInputTests(unittest.TestCase):
    url = "postgresql://owner:test-only-secret@ep-test.example.test/neondb?sslmode=require&channel_binding=require"

    def test_accepts_url_quotes_psql_and_env_assignment(self):
        for value in (self.url, "  " + self.url + "  ", "'" + self.url + "'", '"' + self.url + '"',
                      "psql '" + self.url + "'", 'psql "' + self.url + '"', "DATABASE_URL='" + self.url + "'"):
            with self.subTest(value=value):
                self.assertEqual(connection_input(value), self.url)

    def test_adds_tls_without_losing_existing_parameters(self):
        result = connection_input("postgresql://owner:secret@ep-test.example.test/neondb?channel_binding=require")
        self.assertIn("sslmode=require", result)
        self.assertIn("channel_binding=require", result)

    def test_never_weakens_explicit_tls_validation(self):
        for suffix in ("sslmode=disable", "sslmode=prefer", "sslmode=require&sslmode=disable"):
            with self.subTest(suffix=suffix), self.assertRaises(AppError):
                connection_input("postgresql://owner:secret@ep-test.example.test/neondb?" + suffix)

    def test_rejects_blank_website_template_and_shell_commands(self):
        for value in ("", " ", "https://console.neon.tech", "postgresql://USER:PASSWORD@HOST/neondb?sslmode=require",
                      "python migrate_to_postgres.py", "psql 'unterminated", "psql '" + self.url + "'; echo hello",
                      self.url + "#fragment", self.url + "\nextra"):
            with self.subTest(value=value), self.assertRaises(AppError) as error:
                connection_input(value)
            self.assertNotIn("test-only-secret", str(error.exception))

    def test_reprompts_for_bad_input_without_echoing_it(self):
        with patch.dict(os.environ, {}, clear=True), patch("migrate_to_postgres.getpass.getpass", side_effect=["", "psql '" + self.url + "'"]), patch("builtins.print") as output:
            self.assertEqual(read_connection_url(), self.url)
            self.assertNotIn("test-only-secret", str(output.call_args_list))

    def test_stops_after_three_bad_attempts(self):
        with patch.dict(os.environ, {}, clear=True), patch("migrate_to_postgres.getpass.getpass", return_value=""), patch("builtins.print"), self.assertRaises(AppError):
            read_connection_url()


class LocalMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Store(Path(self.temp.name) / "source.sqlite3")
        self.target = Store(Path(self.temp.name) / "target.sqlite3")

    def adapter(self):
        # Exercise transaction/FK/data behavior on real SQLite without a hosted DB.
        target = self.target
        class Adapter:
            @contextmanager
            def connect(self):
                with target.connect() as db:
                    class Connection:
                        execute = db.execute
                        executemany = db.executemany
                        raw = MagicMock()  # Sequence synchronization is PostgreSQL-specific.
                    yield Connection()
        return Adapter()

    def test_snapshot_and_import_preserve_edits_reports_and_accounts(self):
        self.source.create_user("owner", "a-private-password-42", "admin")
        self.source.create_user("viewer", "", "viewer", passwordless=True)
        first = self.source.pages()[0]
        self.source.save_page({**first, "name": "កែថ្មី", "target": 22}, first["id"])
        report = self.source.report("2020-01-01")
        with self.source.connect() as db:
            user_id = db.execute("SELECT id FROM users WHERE username='owner'").fetchone()["id"]
        self.source.save_report({"date": report["date"], "revision": 0,
                                 "rows": [{"page_id": r["page_id"], "actual": 2} for r in report["rows"]]}, user_id)
        before = snapshot(self.source.path)
        counts = import_snapshot(self.adapter(), before)
        self.assertEqual(counts["users"], 2)
        self.assertEqual(snapshot(self.target.path), before)
        self.assertEqual(snapshot(self.source.path), before)
        self.assertEqual(self.target.shared_viewer(), "viewer")

    def test_import_refuses_occupied_destination(self):
        self.target.create_user("existing", "a-private-password-42")
        before = snapshot(self.target.path)
        with self.assertRaises(AppError):
            import_snapshot(self.adapter(), snapshot(self.source.path))
        self.assertEqual(snapshot(self.target.path), before)

    def test_import_refuses_edited_destination_roster(self):
        first = self.target.pages()[0]
        self.target.save_page({**first, "target": 77}, first["id"])
        before = snapshot(self.target.path)
        with self.assertRaises(AppError):
            import_snapshot(self.adapter(), snapshot(self.source.path))
        self.assertEqual(snapshot(self.target.path), before)

    def test_import_rolls_back_if_any_record_fails(self):
        source = snapshot(self.source.path)
        source["pages"][0]["target"] = -1
        before = snapshot(self.target.path)
        with self.assertRaises(sqlite3.IntegrityError):
            import_snapshot(self.adapter(), source)
        self.assertEqual(snapshot(self.target.path), before)

    def test_missing_source_does_not_create_a_new_database(self):
        missing = Path(self.temp.name) / "missing.sqlite3"
        with self.assertRaises(AppError):
            snapshot(missing)
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
