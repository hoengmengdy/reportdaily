"""Vercel Flask entry point. Uses PostgreSQL; never creates a local database."""
from email.message import Message
import io
import os
from types import SimpleNamespace
import threading

from flask import Flask, Response, request
from server import AppError, Handler, password_hash

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 1_000_000
_store = None
_store_lock = threading.Lock()
_dummy_hash = password_hash("dummy-credential-never-used-for-login")


def get_store():
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                database_url = os.environ.get("DATABASE_URL")
                if not database_url:
                    raise AppError("Connect a Neon database and set DATABASE_URL in Vercel Environment Variables, then redeploy.", 503)
                from postgres_store import PostgresStore
                _store = PostgresStore(database_url)
    return _store


def hosted_context(store):
    setup_token = os.environ.get("SETUP_TOKEN", "")
    return SimpleNamespace(store=store, secure_cookie=True, allow_local_setup=False,
                           allow_hosted_setup=len(setup_token) >= 32,
                           setup_token=setup_token, enable_public_viewer=True,
                           dummy_hash=_dummy_hash)


class WebHandler(Handler):
    """Adapt a Flask request to the tested API without opening a server socket."""
    def __init__(self):
        self.path = request.full_path.rstrip("?")
        self.headers = Message()
        for key, value in request.headers:
            self.headers[key] = value
        self.rfile = io.BytesIO(request.get_data())
        self.wfile = io.BytesIO()
        self.client_address = (request.remote_addr or "unknown", 0)
        self.server = hosted_context(None)
        self.status = 200
        self.response_headers = []

    def send_response(self, code, message=None):
        self.status = code

    def send_header(self, keyword, value):
        self.response_headers.append((keyword, value))

    def end_headers(self):
        pass


@app.route("/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "DELETE"])
@app.route("/<path:path>", methods=["GET", "POST", "PUT", "DELETE"])
def handle(path):
    handler = WebHandler()
    try:
        if request.path.startswith("/api/"):
            handler.server.store = get_store()
        handler.dispatch(request.method)
    except AppError as error:
        handler.respond(error.status, {"error": str(error)})
    except Exception:
        # Connection strings must never appear in logs or error responses.
        handler.respond(503, {"error": "Hosted database initialization failed. Check DATABASE_URL and the deployment configuration."})
    return Response(handler.wfile.getvalue(), status=handler.status, headers=handler.response_headers)
