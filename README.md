# Campus-notes

Campus-notes is the mobile version of Qbank, with an Expo React Native client and a
FastAPI backend that uses PyMySQL to run SQL directly against MySQL (no ORM).
The original `Qbank` and `Qbank-backend`
folders are left unchanged.

## Requirements

- Node.js 20+
- Python 3.11+
- MySQL 8+
- Expo Go or an iOS/Android simulator
- Tesseract OCR (only needed for scanned PDFs)

## Configure the API

1. In `backend`, copy `.env.example` to `.env`, then set `SECRET_KEY` to a
   unique random value of at least 32 characters, the MySQL credentials,
   `CORS_ORIGINS`, and the admin credentials. The API refuses to start with
   the example signing key. The admin is created on first startup when both
   `ADMIN_EMAIL` and `ADMIN_PASSWORD` are set.
2. Install and start MySQL 8 on your computer. Set `MYSQL_HOST`, `MYSQL_PORT`,
   `MYSQL_USER`, `MYSQL_PASSWORD`, and `MYSQL_DATABASE` in `.env`. By default,
   startup does not create or migrate tables; apply `backend/schema.sql`
   manually, or opt in to startup schema setup with `SCHEMA_AUTO_MIGRATE=true`.
   The schema file is the single source for table definitions; startup applies
   its `CREATE TABLE` statements and then runs additive compatibility migrations.
   To preview the app with demo departments and papers, set
   `SEED_DEMO_DATA=true` or run `python -m app.dev_seed` from `backend` after
   applying the schema. Demo fixtures are defined only in that Python seed
   module and are inserted only when missing.
3. Create and activate a Python virtual environment, install dependencies,
   then run the API with `app.py`:

   ```powershell
   cd backend
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   python app.py
   ```

   For development and tests, install the extra developer requirements instead:

   ```powershell
   pip install -r requirements-dev.txt
   ```

   The API listens on `0.0.0.0:8000`. Question-paper PDFs are stored in
   `backend/uploads` and served from `/uploads`. Reader study-resource PDFs are
   stored privately in `backend/resource_uploads` by default;
   `RESOURCE_UPLOAD_DIR` can override that location but must remain outside the
   public `UPLOAD_DIR`. Relative storage paths are resolved against the
   `backend` folder.
   Production database users need only normal application data permissions
   unless schema setup is explicitly enabled. Set `SCHEMA_AUTO_MIGRATE=true`
   only when startup is authorized to create the database and apply additive
   schema changes. Neither schema setup nor migrations drop tables or columns.
   Open `http://localhost:8000/admin/` for the separate administrator
   dashboard. Only system administrators sign in there; the mobile app is for
   reader and institute accounts. Apply the updated `backend/schema.sql` to an
   existing database (or opt in to `SCHEMA_AUTO_MIGRATE=true`) before starting
   this version; it adds the study-resources table without deleting existing
   data.
   Readers can create accounts from the mobile app. Administrator accounts
   are provisioned only through `ADMIN_EMAIL`/`ADMIN_PASSWORD` or by an
   authenticated super admin; public registration never grants admin access.
   On startup, legacy department administrator accounts are converted to
   reader accounts, their department assignments are cleared, and their
   existing sessions are revoked.
   Readers can submit PDF notes, assignments, and other study materials from
   the mobile app. These files remain private until a system administrator
   approves each submission in the admin dashboard. Administrator uploads are
   published immediately. Approved resources are served through an authenticated
   API and stored outside the public question-paper upload directory.
   Institute staff registration collects a phone number, designation,
   college/university name, and a required PDF, JPG, or PNG affiliation document
   (up to `MAX_UPLOAD_MB`). Supporting documents are stored privately and can
   only be downloaded by a system administrator from the Institute approvals
   section. Institute accounts remain read-only until approved; approved
   institute accounts can upload and edit question papers in the mobile app,
   but only administrators can delete them. When an administrator approves an
   institute account, Campus-notes sends an approval confirmation to its
   registered email address using the SMTP settings below.
4. Configure `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`,
   `SMTP_FROM_EMAIL`, `SMTP_FROM_NAME`, and `SMTP_USE_TLS` in `backend/.env`
   to enable password recovery for reader, institute, and administrator
   accounts and approval confirmations for institute staff. Use credentials
   from your mail provider; do not commit them.
   For Gmail, set `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`,
   `SMTP_USE_TLS=true`, and use the sending Gmail address for both
   `SMTP_USERNAME` and `SMTP_FROM_EMAIL`. `SMTP_PASSWORD` must be a Google
   App Password (created with 2-Step Verification enabled), not the normal
   account password.
   The API returns a generic response whether or not an account exists, sends
   a six-digit code that expires after 10 minutes, limits verification to five
   attempts, and enforces a 60-second resend cooldown. The admin dashboard and
   mobile app both provide the recovery flow. Without valid SMTP settings,
   email delivery fails quietly from the client's point of view: the API still
   returns the same generic response, and the failure is written to the server
   log. Check the log first when a reset code never arrives.

## Access policy

Question-paper PDFs are intentionally public: `/uploads` is served without
authentication so that a shared paper link works, and institute affiliation
documents and unapproved reader resources are kept outside that public static
directory. Approved reader resources are delivered through an authenticated
endpoint. Because of that, **only material that may be public belongs in
`backend/uploads`**. Paper files that are no longer referenced by a paper are
quarantined with:

```powershell
python -m scripts.sweep_orphans                        # dry run, lists orphans
python -m scripts.sweep_orphans --quarantine ../backup/quarantine
```

## Rate limits

The API throttles the endpoints that are expensive or abusable. Limits are per
client address for unauthenticated routes and per user id for AI generation,
so students behind one campus network do not consume each other's allowance.
A throttled request returns HTTP 429 with a `Retry-After` header.

| Endpoint | Limit |
| --- | --- |
| `POST /api/auth/login` | 15 attempts / 15 minutes |
| `POST /api/auth/register` | 5 / hour |
| `POST /api/auth/password-reset/request` | 5 / hour (plus the per-email cooldown) |
| `POST /api/ai-prep/generate` | 5 / hour and 20 / day per user |

These counters live in the API process. Running several uvicorn workers
multiplies the effective limit; move the counters to shared storage before
scaling horizontally.

## Backup and restore

A backup you have never restored is not a backup. The repository ships two
scripts for this.

```powershell
cd backend

# 1. Write a portable SQL dump to ..\backup
python -m scripts.backup_database

# 2. Archive the PDFs and private documents
tar -czf ..\backup\documents_$(Get-Date -Format yyyyMMdd_HHmmss).tar.gz uploads institute_documents resource_uploads

# 3. Prove the dump restores, by replaying it into campus_notes_verify
python -m scripts.verify_backup
```

`verify_backup` recreates a scratch database, replays the dump, and compares
row counts against the live database table by table. Treat a `MISMATCH` as a
failed backup. Nothing in these scripts writes to the live database except the
scratch database they create.

Restore drill (do this before relying on the backup):

1. `python -m scripts.backup_database`
2. `python -m scripts.verify_backup` — must print `RESULT: PASS`
3. Extract the documents archive somewhere temporary and open one PDF.

## AI exam preparation

The mobile Home screen links to mock tests and important-question lists based
only on uploaded question-paper PDFs with valid year metadata. Paper `year`
must be a four-digit year, because that is what the preparation pipeline can
interpret.

Configure `OLLAMA_BASE_URL` and `OLLAMA_MODEL` for the backend-reachable local
Ollama service; it is always tried first. **Set `OLLAMA_MODEL` explicitly to a
model you have actually pulled.** The code default (`llama3.2:3b`) is not
installed on every machine, and a missing model makes every local call fail.
The resolved model is reported by `GET /api/health`, which never exposes
secrets:

```powershell
ollama list                     # see what is installed
ollama pull llama3.2:3b         # or pin whatever you prefer
```

`GET /api/health` reports the local model name, the local URL, whether the
hosted fallback is configured, and the provider timeout. Use it to confirm
configuration before testing the feature.

An optional hosted OpenAI-compatible chat completion endpoint can be
configured with `HOSTED_AI_BASE_URL`, `HOSTED_AI_MODEL`, and
`HOSTED_AI_API_KEY`. The hosted API key stays in the backend environment,
never in the mobile app. Students must consent before their selected PDF text
can be sent to the hosted fallback. Leave the three `HOSTED_AI_*` values empty
to disable the paid fallback entirely — the API then reports an honest
unavailable error instead of quietly spending money. If both providers are
unavailable, generation reports an error rather than returning invented
questions.

`AI_PROVIDER_TIMEOUT_SECONDS` defaults to 45. Raise it (for example to 120)
when using a larger local model, because generation time grows with model size
and the amount of source text.

Generation is bounded: 10 source PDFs, 200 total pages, 120,000 extracted text
characters, and 30 mock-test or 20 important questions. Extracted page text is
cached per PDF, so repeating a generation over the same papers does not
re-extract or re-OCR them.

Every question must cite at least one selected paper. A question labelled
`past_paper` is checked against the text of the page it cites; if the wording
is not actually on that page it is downgraded to `ai_generated` and marked
`grounding: unverified`. This keeps the interface honest about which questions
really came from an exam paper.

Digital PDFs are read with PyMuPDF. Scanned PDFs use Tesseract OCR; install the
Tesseract executable on the backend host and set `TESSERACT_CMD` if it is not
on `PATH`. OCR uses English by default. Subject and year choices are derived
from uploaded PDF records and stored paper metadata, not filenames. Correct
missing or inaccurate years in paper metadata before using those papers.

## Run the mobile app

1. In the `mobile` folder, copy `.env.example` to `.env`.
2. Set `EXPO_PUBLIC_API_URL` to an address reachable from the device. Use
   `http://10.0.2.2:8000` for the Android emulator, `http://localhost:8000`
   for an iOS simulator, or your computer's Wi-Fi/LAN IP for a phone running
   Expo Go. The phone and computer need to share the same network.
3. Install dependencies and start Expo directly with `npx`:

   ```powershell
   cd mobile
   npm install
   npx expo start
   ```

Never put MySQL credentials or the signing key in the mobile app environment.
Only the public API URL belongs in `mobile/.env`.

Paper PDF links are stored as relative paths (`/uploads/<file>`), so the app
resolves them against `EXPO_PUBLIC_API_URL`. Changing the server's LAN address
no longer breaks existing papers.

## Tests

```powershell
cd backend
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
pytest -q
```

The suite does not require MySQL, Ollama, SMTP, or Tesseract: it uses fake
database connections and injected providers. Tests that need OCR or a live
provider are out of scope for the suite.
