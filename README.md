# DefectDNA

## Phase 1 — Foundation

The runnable foundation includes a shared library, four FastAPI services, MongoDB/Atlas
configuration, service tokens, structured JSON logging, and MongoDB-backed jobs.
Docker Compose and CI/CD are intentionally not included in this phase.

### Setup

1. Set `MONGODB_URI` in `.env` to the MongoDB Atlas development cluster.
2. Keep the Azure DevOps PAT in `.env` and never commit `.env`.
3. Install Python 3.12 dependencies:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

### Run services

Each service runs in a separate terminal:

```bash
uvicorn services.api_gateway.app.main:app --reload --port 8000
uvicorn services.ingestion_service.app.main:app --reload --port 8001
uvicorn services.ai_knowledge_service.app.main:app --reload --port 8002
uvicorn services.trends_service.app.main:app --reload --port 8003
```

Health endpoints are available at `/health` and `/health/ready`.

### Foundation migration

```bash
python migrations/001_foundation.py
```

### Tests

```bash
pytest
ruff check .
mypy libs services
```

## Phase 2 — Authentication and roles

Phase 2 adds local Argon2id accounts, first-login password changes, lockout,
server-backed signed sessions, CSRF protection, per-project roles, Admin user
management, audit records, request rate limiting, and security headers.

1. Set `SESSION_SIGNING_KEY` in `.env` to a random value of at least 32 bytes.
2. Apply the authentication indexes:

```bash
python migrations/002_authentication.py
```

3. Bootstrap the first project and Admin interactively (the password is read
   without echoing):

```bash
python -m services.api_gateway.app.bootstrap_admin
```

The Admin can create projects, users, and project roles through `/api/v1`.
Newly created users receive a one-time temporary password and must change it at
their first sign-in. Session cookies are Secure by default; set
`SESSION_COOKIE_SECURE=false` only for local HTTP development.
MFA is not included because the architecture document leaves that requirement
pending an owner decision.

## Phase 3 — Ingestion

The ingestion service supports CSV/XLSX imports, automatic or explicit column
mapping, row-level validation reports, project-scoped idempotent defect upserts,
ADO read-only synchronization, and project-scoped defect list/detail endpoints.
The gateway enforces project roles and forwards ingestion requests using a
service token. A minimal React interface is available for sign-in, importing
files, and browsing defects.

1. Configure `SERVICE_TOKEN_SECRET` with at least 32 random bytes in `.env`.
2. Set `ADO_ORGANIZATION_URL`, `ADO_PROJECT`, and `ADO_PAT` in `.env` to enable
   ADO synchronization. The PAT must be read-only; it is never returned by the
   API. Live ADO synchronization is unavailable until a credential is configured.
3. Apply the ingestion indexes:

```bash
python migrations/003_ingestion.py
```

4. Start the API gateway and ingestion service using the commands above. For
   local HTTP development, set `SESSION_COOKIE_SECURE=false` in `.env`.
5. Start the frontend in another terminal:

```bash
cd frontend
npm install
npm run dev
```

The Vite development server proxies `/api` requests to the gateway on port
8000. Uploads are limited by `MAX_IMPORT_FILE_BYTES` (10 MiB by default) and
`MAX_IMPORT_ROWS` (5,000 rows by default).
For CSV/XLSX files, `ID` and `Title` are required; common ADO export headers
are detected automatically. An optional JSON mapping can map source headings
to canonical fields such as `ado_id`, `title`, `module`, and `release`.

The repository does not include a sanitized sample ADO export or an ADO PAT.
CSV/XLSX parsing and ADO request behavior are validated with local fixtures and
mocked HTTP responses; live synchronization still requires the pending ADO
credential and test project.
