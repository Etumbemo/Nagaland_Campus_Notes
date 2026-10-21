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
   cd E:\Naga_Node\campus-notes\backend
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   python app.py
   ```

   The API listens on `0.0.0.0:8000`. Uploaded PDFs are stored in
   `backend/uploads` and served from `/uploads`.
   Production database users need only normal application data permissions
   unless schema setup is explicitly enabled. Set `SCHEMA_AUTO_MIGRATE=true`
   only when startup is authorized to create the database and apply additive
   schema changes. Neither schema setup nor migrations drop tables or columns.
   Open `http://localhost:8000/admin/` for the separate administrator
   dashboard. System and department administrators sign in there; the mobile
   app is for reader accounts and favorites.
   Readers can create accounts from the mobile app. Administrator accounts
   are provisioned only through `ADMIN_EMAIL`/`ADMIN_PASSWORD` or by an
   authenticated super admin; public registration never grants admin access.
   Institute staff registration collects a phone number, designation,
   college/university name, and a required PDF, JPG, or PNG affiliation document
   (up to `MAX_UPLOAD_MB`). Supporting documents are stored privately and can
   only be downloaded by a system administrator from the Institute approvals
   section. Institute accounts remain read-only until approved; approved
   institute accounts can upload and edit question papers in the mobile app.
4. Configure `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`,
   `SMTP_FROM_EMAIL`, `SMTP_FROM_NAME`, and `SMTP_USE_TLS` in `backend/.env`
   to enable password recovery for reader, institute, and administrator
   accounts. Use credentials from your mail provider; do not commit them.
   For Gmail, set `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`,
   `SMTP_USE_TLS=true`, and use the sending Gmail address for both
   `SMTP_USERNAME` and `SMTP_FROM_EMAIL`. `SMTP_PASSWORD` must be a Google
   App Password (created with 2-Step Verification enabled), not the normal
   account password.
   The API returns a generic response whether or not an account exists, sends
   a six-digit code that expires after 10 minutes, limits verification to five
   attempts, and enforces a 60-second resend cooldown. The admin dashboard and
   mobile app both provide the recovery flow. Without valid SMTP settings,
   password-reset email delivery will fail explicitly.

## AI exam preparation

The mobile Home screen links to mock tests and important-question lists based
only on uploaded question-paper PDFs with valid year metadata. Configure
`OLLAMA_BASE_URL` and `OLLAMA_MODEL` for the backend-reachable local Ollama
service; it is always tried first. An optional hosted OpenAI-compatible chat
completion endpoint can be configured with `HOSTED_AI_BASE_URL`,
`HOSTED_AI_MODEL`, and `HOSTED_AI_API_KEY`. The hosted API key stays in the
backend environment, never in the mobile app. Students must consent before
their selected PDF text can be sent to the hosted fallback. If both providers
are unavailable, generation reports an error rather than returning invented
questions. To bound processing, each generation is limited to 10 source PDFs,
200 total pages, 120,000 extracted text characters, and 30 mock-test or 20
important questions.

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
   cd E:\Naga_Node\campus-notes\mobile
   npm install
   npx expo start
   ```

Never put MySQL credentials or the signing key in the mobile app environment.
Only the public API URL belongs in `mobile/.env`.
