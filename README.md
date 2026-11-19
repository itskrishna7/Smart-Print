# Smart-Print

Smart-Print is a FastAPI and React/Vite application for print-shop orders,
document analysis, printing workflows, and customer uploads.

## Local development

1. Use Python 3.10 or newer and Node.js.
2. Install backend dependencies:

   ```powershell
   python -m pip install -r .\requirements.txt
   ```

3. Set `SMARTPRINT_ENV=development` and `DATABASE_URL` in the backend
   terminal/IDE environment. Use a pooled Neon PostgreSQL URL for application
   traffic. `.env.example` is a placeholder reference; the application does
   not load `.env` files automatically. Local mode uses the `uploads/` and
   `qrcodes/` directories and does not require Supabase credentials.
4. Start the API:

   ```powershell
   python -m uvicorn main:app --reload
   ```

5. In another terminal, install frontend dependencies and start Vite:

   ```powershell
   npm --prefix frontend install
   npm --prefix frontend run dev
   ```

## Supabase Storage setup

Production uses a private Supabase Storage bucket. Create a private bucket
named `smartprint-backend` in the Supabase dashboard. Set its maximum file size
to 50 MiB, and restrict allowed MIME types to `application/pdf` when that
setting is available.

Configure these variables on the **backend only**:

```text
SMARTPRINT_ENV=production
SMARTPRINT_STORAGE_BACKEND=supabase
SUPABASE_URL=https://<project-ref>.supabase.co
SUPABASE_SERVICE_ROLE_KEY=<backend-secret>
SUPABASE_BUCKET=smartprint-backend
SMARTPRINT_STORAGE_TIMEOUT_SECONDS=30
SMARTPRINT_STORAGE_DOWNLOAD_URL_TTL_SECONDS=300
```

Also configure the application's existing database, session-secret, and
HTTPS-origin settings as described in
[SECURITY-OPERATIONS.md](./SECURITY-OPERATIONS.md). Never expose the Supabase
service-role key in the frontend or make the bucket public. Configure the
Supabase project's CORS allowlist for the deployed Vercel HTTPS origin so the
browser can upload with `PUT` and `Content-Type`.

The browser uploads to a signed URL for a random staging object.
Smart-Print validates the saved upload intent and actual object size, performs
analysis on temporary files, and uses the private bucket for order documents,
QR images, reprints, and batch exports. Downloads are authorized by the backend
before a short-lived signed URL is returned. Existing objects are not copied:
files that exist only in the old Cloudflare R2 bucket remain there and cannot be
opened from Smart-Print after switching to Supabase.

Schedule cleanup of abandoned objects under the `staging/` prefix. Do not set
bucket-wide expiry for permanent order files. Application settings and
deployment steps, including Render/Vercel configuration and database
migrations, are maintained in [SECURITY-OPERATIONS.md](./SECURITY-OPERATIONS.md).

## Neon PostgreSQL and Aiven migration

Create a Neon project and choose a region close to the Render backend. In the
Neon Console, select the intended branch, database, and role, then copy both
connection strings:

- Put the pooled connection string in Render's backend-only `DATABASE_URL`.
- Keep the direct/unpooled connection string in a local secret store as
  `DATABASE_URL_UNPOOLED`; the schema and data migration tools require it.

Do not put either URL in Vite variables, source code, logs, or committed files.
The checked-in `.env.example` contains placeholders only. The API continues to
use FastAPI, existing authentication and authorization, and Supabase Storage;
only its relational database driver changes.

Install the optional source-transfer dependency and review the PostgreSQL
schema migration plan. Both commands are non-mutating by default:

```powershell
python -m pip install -r requirements-migration.txt
python migrate.py
python migrate_aiven_to_neon.py
```

For Aiven access, configure `SMARTPRINT_DB_HOST`, `SMARTPRINT_DB_PORT`,
`SMARTPRINT_DB_USER`, `SMARTPRINT_DB_PASSWORD`, `SMARTPRINT_DB_NAME`, and
`SMARTPRINT_DB_SSL_CA` locally. Use a read-only Aiven account and retain its CA
file. The transfer opens a read-only repeatable-read transaction on Aiven,
streams rows in batches, checks the destination schema, and does not write to
the source. It transfers database rows only; Supabase Storage files are not
copied or modified.

First apply the additive schema to an isolated Neon test branch and then run
the real PostgreSQL integration tests against that branch:

```powershell
$env:DATABASE_URL_UNPOOLED = "<direct Neon test-branch URL>"
$env:SMARTPRINT_NEON_TEST_PROJECT_ID = "<Neon test project ID>"
$env:SMARTPRINT_NEON_TEST_BRANCH_ID = "<Neon test branch ID>"
python migrate.py --apply --target-environment test `
  --neon-project-id "<Neon project ID>" --neon-branch-id "<test branch ID>"
$env:SMARTPRINT_TEST_DATABASE_URL = $env:DATABASE_URL_UNPOOLED
python -m unittest discover -s tests -p test_postgres_integration.py -v
```

Apply commands require the Neon CLI to be installed and authenticated. They use
Neon's API to verify that the direct URL host belongs to the explicitly selected
project and branch before opening a database connection. The selected IDs must
also match the `SMARTPRINT_NEON_TEST_*` or `SMARTPRINT_NEON_PRODUCTION_*`
environment allowlist selected by `--target-environment`; a branch label alone
is not considered proof of target identity. Configure the corresponding
production project and branch IDs before any explicitly authorized production
operation.

The transfer utility defaults to a dry run. Before an authorized data import,
quiesce Aiven writes for the full transfer and inspect the source/target table
counts reported by the dry run. The tool refuses target tables with untracked
rows, records per-table progress transactionally, checks row counts and
checksums, and can resume completed tables only when their source and target
still match:

```powershell
$env:SMARTPRINT_NEON_TEST_PROJECT_ID = "<Neon test project ID>"
$env:SMARTPRINT_NEON_TEST_BRANCH_ID = "<Neon test branch ID>"
python migrate_aiven_to_neon.py
python migrate_aiven_to_neon.py --apply --target-environment test `
  --neon-project-id "<Neon project ID>" --neon-branch-id "<test branch ID>"
```

No production data transfer or Render cutover is automatic. Production schema
changes require `--apply --target-environment production
--confirm-production --neon-project-id "<Neon project ID>"
--neon-branch-id "<production branch ID>"; a production data transfer
additionally requires the same explicit target confirmation and branch IDs.
Set `SMARTPRINT_NEON_PRODUCTION_PROJECT_ID` and
`SMARTPRINT_NEON_PRODUCTION_BRANCH_ID` to those exact IDs; the tools reject
project/branch arguments that do not match the selected environment's allowlist.
First verify the complete source schema,
all per-table counts/checksums, login, order creation/tracking, printing,
rate-limits, and storage-backed document flows on a test branch.

Keep Aiven and its configuration unchanged until the Neon import and live
application checks are verified. For rollback before Neon accepts production
writes, redeploy the last MySQL-compatible backend release and restore its
Aiven environment settings. After production writes have reached Neon, simply
pointing the old backend at Aiven would lose those writes; pause traffic and
reconcile/transfer the new records before switching back. Keep the Aiven
database untouched as the rollback source.

Neon's Free plan has usage limits and is intended for prototyping; review the
current [Neon plans](https://neon.com/pricing) and production checklist before
relying on it for a live customer workload.

## Render and Vercel

For a Render backend, create a Web Service with repository root `.`, build
command `python -m pip install -r requirements.txt`, and start command
`python -m uvicorn main:app --host 0.0.0.0 --port $PORT`. Set the Neon pooled
URL as backend-only `DATABASE_URL` and configure the existing session, origin,
and Supabase variables. Configure `SMARTPRINT_NEON_TEST_PROJECT_ID` and
`SMARTPRINT_NEON_TEST_BRANCH_ID`, then run `python migrate.py --apply
--target-environment test --neon-project-id <test-project-id>
--neon-branch-id <test-branch-id>` against a non-production Neon branch before
any reviewed production schema migration.

Deploy the Vite frontend from the `frontend` root on Vercel with
`npm run build` and output directory `dist`. The frontend uses same-origin API
paths and secure `SameSite=Lax` owner-session cookies. Vercel hosts the React
frontend and `vercel.json` rewrites HTTP API requests to the Render-hosted
FastAPI backend under the Vercel origin. WebSocket requests are not currently
proxied through Vercel; the frontend does not currently use the backend's
WebSocket endpoint. If WebSocket support is added, connect directly to Render
and review session authentication for that cross-origin connection.
