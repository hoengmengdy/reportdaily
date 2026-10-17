# Deploy this existing project to Vercel + Neon

The hosted entry point is `app.py` (Flask). It uses PostgreSQL via `DATABASE_URL`. `python server.py` still uses your local SQLite database unless you explicitly set `DATABASE_URL` in that terminal. No database file is stored in Vercel's temporary filesystem.

## 1. Prepare the Vercel project

Push these source changes to your existing GitHub repository, then import/redeploy the repository in Vercel. The source has not been pushed automatically. On the screen shown in the conversation, use:

| Setting | Value |
| --- | --- |
| Project name | `reportdaily` (or your preferred name) |
| Root directory | `./` |
| Application / Framework preset | **Flask** |
| Build command | Leave the override disabled |
| Output directory | Leave the override disabled |
| Install command | Leave the override disabled; Vercel installs `requirements.txt` |

`vercel.json` and `pyproject.toml` select the Flask entry point. There is no frontend build step. Python code, credentials, and database files are never static/public routes; the handler only serves the four named frontend assets.

You can create the initial deployment before connecting a database. The login page will load and report that `DATABASE_URL` must be set; report APIs will return a clear configuration error until it is connected.

## 2. Create and connect Neon

In your Vercel project, open **Storage**, select **Create Database**, choose **Neon (Postgres)**, and follow the integration to create/connect your database. Review the plan shown by Vercel/Neon before accepting it. The integration can add connection environment variables to the project.

Verify that **`DATABASE_URL`** is present in **Settings → Environment Variables** for **Production**. It should be a PostgreSQL connection URL with TLS (`sslmode=require`), preferably Neon's pooled URL. If an integration uses a prefix or only exposes another connection-variable name, add `DATABASE_URL` privately with that same connection URL.

Do not put the connection URL in source code, screenshots, Git, or chat. Use a separate Neon branch/database for Preview deployments when enabling Preview environment variables; Production and Preview otherwise share records.

## 3. Preserve the data already on this computer (recommended)

Before creating a new cloud administrator, copy the existing local database into the fresh Neon database. This preserves edited page targets, names/URLs, archived pages, seed history, accounts/password hashes, passwordless viewer access, and saved daily reports. Sessions are intentionally not transferred; sign in again online.

In PowerShell, from the existing project folder:

```powershell
Set-Location 'C:\Word\report all day'
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe migrate_to_postgres.py
```

The last command securely prompts for the Neon connection URL with input hidden. Open **Neon → your database → Connect** and copy its connection string. Paste either the raw `postgresql://…` URL or the copied `psql 'postgresql://…'` command; the tool parses that text without executing it. Input does not show while pasting/typing. Press Enter afterward. Quotes/outer whitespace are accepted, and `sslmode=require` is added if the remote URL omits it. Explicitly disabled TLS is rejected. Invalid input gets a specific explanation and up to three attempts. If an older copy of the script is currently waiting at a prompt, press Ctrl+C and run the command once again to load these changes.

The connection string is never printed or saved by the tool. The source SQLite database is opened read-only with a consistent snapshot. The destination must contain only untouched initial seed records and no accounts/reports; an occupied/edited destination is refused. The import is transactional and will not merge or overwrite an existing cloud workspace.

When it completes, **redeploy** Vercel so the connected environment variables are loaded. Sign in using your existing administrator account. The **View reports · no password** button works with your imported viewer account.

The migration tool has tests for local snapshots and destination guards. A real Neon migration cannot be verified until a database is connected.

## Alternative: start with a new cloud workspace

Skip this section when importing your local data.

Generate a private setup key locally:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Add that value as **`SETUP_TOKEN`** in the Vercel project's Environment Variables (Production). Redeploy. Open the site and create your administrator username/password, entering this private key in **Private setup key**. The endpoint never returns the key to visitors. Only the first administrator can be created; concurrent attempts are serialized in the database. It also creates a separate read-only passwordless viewer. Remove `SETUP_TOKEN` and redeploy after setup is complete.

A fresh workspace seeds the 14 original pages/targets. It does not automatically contain later edits or reports from your computer. Choose the import path before creating a new cloud administrator if you want to preserve those records.

## Validation and local hosted-adapter preview

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
node --check app.js
node --test tests/api_response.test.cjs
.\.venv\Scripts\python.exe -m flask --app app run --port 8001
```

This previews the hosted Flask adapter. Static/login files work without a database; APIs intentionally return 503 until `DATABASE_URL` is supplied in the process environment. Secure cookies are enabled by the hosted adapter, so use HTTPS for a signed-in hosted workflow. For normal local operation, keep using `python server.py` on port 8000.

## Architecture

- The frontend uses the same `/api/*` endpoints on the deployed domain.
- PostgreSQL stores pages, reports/snapshots, roles, sessions/CSRF tokens, seed/migration history, and login attempt limits.
- No database connections are shared concurrently; each operation gets its own connection/transaction.
- Transaction-scoped database locks protect migrations, seed operations, first-user setup, report revisions, and login throttling across serverless instances.
- Case-insensitive usernames, unique profile IDs, Unicode, historical targets, and read-only viewer permissions are retained.
- A missing/invalid connection URL does not fall back to a temporary SQLite file.
- `.vercelignore` excludes local databases, virtual environments, tests, Git metadata, and environment files from uploads. Your local database is never uploaded as a public asset.

Official references: [Flask on Vercel](https://vercel.com/docs/frameworks/backend/flask), [Vercel Postgres integrations](https://vercel.com/docs/postgres), [Neon managed Vercel integration](https://neon.com/docs/guides/vercel-managed-integration).
