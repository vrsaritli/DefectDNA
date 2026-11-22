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

## Phase 4 — AI RCA slice

Phase 4 proposes a root cause, SDLC phase and testing gap for each closed defect,
using Groq `openai/gpt-oss-120b` with strict JSON-schema output, and shows the
proposal read-only (approve/correct is Phase 5).

**Pipeline (ai-knowledge-service worker):** scrub sensitive values (emails, URLs,
internal hosts, IPs, tokens/keys, GUIDs, long numbers) → pack defects into
token-budgeted batches → Groq → validate every item against the project's
taxonomy version → retry rejected items once → store `rca_analyses` as
`AI_PROPOSED`, or `NEEDS_MANUAL_RCA` with a failure code. Reviewed RCAs are never
overwritten; re-analysis replaces only unreviewed ones. Logs contain IDs, codes and
counts only — never defect text, prompts or keys.

**Token budget per Groq request:** defects are packed while the estimated prompt
plus expected output stays at or below `LLM_BATCH_TARGET_TOKENS` (4,000), and
`max_completion_tokens` is set so prompt + output can never exceed
`LLM_REQUEST_TOKEN_BUDGET` (4,500). The fixed system prompt (rules + taxonomy) is
about 1,300 tokens, so batching (usually 3–4 defects per call) avoids paying it
once per defect. In the baseline run, requests averaged 3,274 tokens (max 4,106).

**Taxonomy:** each project starts with a versioned starter taxonomy. Root causes
follow the QA team's existing categories from the ADO exports (Requirements,
Design, Code, Integration, Data, Configuration, Deployment, Third Party, Security,
Test Gap, Other). QA Leads can publish a new version with
`PUT /api/v1/projects/{id}/taxonomy`.

**Data-handling gate:** AI analysis is on by default when `ENV=dev` and off when
`ENV=prod` until a project Admin enables it with
`PUT /api/v1/projects/{id}/settings {"llm_analysis_enabled": true}`.

### Setup

1. Add `GROQ_API_KEY` (and optionally the other Phase 4 values in
   `.env.example`) to `.env`.
2. Apply the Phase 4 indexes and starter taxonomy:

```bash
python migrations/004_ai_rca.py
```

3. Run the AI-knowledge service and its worker (both are needed):

```bash
uvicorn services.ai_knowledge_service.app.main:app --reload --port 8002
python -m services.ai_knowledge_service.app.worker
```

4. In the app, open **AI analysis** → **Analyze new closed defects**. Progress
   is polled from `/api/v1/jobs/{id}`; open any row to see the defect next to the
   AI proposal.

### New endpoints

| Method | Endpoint | Purpose | Min role |
|---|---|---|---|
| GET | `/projects/{pid}/taxonomy` | Current taxonomy | Viewer |
| PUT | `/projects/{pid}/taxonomy` | Publish a new taxonomy version | QA Lead |
| GET/PUT | `/projects/{pid}/settings` | Read / set `llm_analysis_enabled` | Viewer / Admin |
| POST | `/projects/{pid}/analyses` | Analyze unanalyzed closed defects, or `{"ado_ids": [...]}`, `{"reanalyze": true}` (job) | QA Lead |
| GET | `/projects/{pid}/rcas?status=` | List RCAs | Viewer |
| GET | `/rcas/{rca_id}` | RCA detail with taxonomy labels | Viewer |

`GET /projects/{pid}/defects/{ado_id}` now includes the defect's latest RCA.

### Golden set and accuracy baseline

```bash
# rebuild from ADO exports that carry a QA "Root Cause (RCA)" column
python -m services.ai_knowledge_service.evals.build_golden_set <Prod.xlsx> <UAT.xlsx> \
    --out services/ai_knowledge_service/evals/golden_set.json
# run it through the production pipeline (calls Groq)
python -m services.ai_knowledge_service.evals.run_golden_set
```

See [services/ai_knowledge_service/evals/README.md](services/ai_knowledge_service/evals/README.md)
for the baseline and how to read it.
## Phase 6 — Trends, dashboard and export

Phase 6 turns reviewed RCAs into trends. It also adds the approve/correct step that
trends depend on: Phase 5 on `main` did not yet include one. The knowledge-base write
(embedding the confirmed RCA) still belongs to Phase 5 and plugs into the same review
commit.

**Flow:** a QA Lead approves or corrects an RCA on **AI analysis** →
`POST /api/v1/rcas/{id}/review` → the ai-knowledge-service stores the confirmed RCA
and enqueues a `trend_fact` job → the trends worker upserts one `trend_facts` document
per defect → the **Dashboard** tab aggregates them. Counts include approved and
corrected RCAs only. A correction that keeps the AI's codes counts as approved, so
"AI agreement" means "QA kept the AI's answer".

### Setup

```bash
python migrations/006_trends.py
uvicorn services.trends_service.app.main:app --reload --port 8003
python -m services.trends_service.app.worker
```

Set `TRENDS_SERVICE_URL` if the trends-service is not on port 8003. To load RCAs that
were reviewed before the worker ran (or to reconcile), click **Rebuild** on the
dashboard or call `POST /api/v1/projects/{id}/trends/rebuild`.

### Dashboard

Stat cards (reviewed RCAs, AI agreement, top root cause, most affected module), a
monthly stacked bar chart of root causes, ranked lists for modules, root causes,
testing gaps and SDLC phases, and a breakdown table grouped by module, release, month,
testing gap or SDLC phase. Filters: module, release and month range. The layout follows
the shadcn-admin overview pattern; chart colours use a CVD-validated categorical
palette, and each root cause keeps its colour when filters change.

### New endpoints

| Method | Endpoint | Purpose | Min role |
|---|---|---|---|
| POST | `/rcas/{rca_id}/review` | `{"decision": "APPROVE"}` or `{"decision": "CORRECT", "final": {...}, "note": "..."}`; `"reopen": true` to change a reviewed RCA | QA Lead |
| GET | `/projects/{pid}/trends?group_by=module,month` | Counts with root-cause breakdown; filters `module`, `release`, `month_from`, `month_to`, `root_cause`, `testing_gap`, `severity` | Viewer |
| GET | `/projects/{pid}/trends/summary` | Dashboard data (same filters) | Viewer |
| POST | `/projects/{pid}/trends/rebuild` | Re-derive trend facts from all reviewed RCAs | QA Lead |
| GET | `/projects/{pid}/export?type=defects\|rcas\|trends&format=csv\|xlsx` | Download; cells starting with `= + - @` are neutralised; capped at `EXPORT_MAX_ROWS` | Viewer |

Exports and reviews are written to the audit log (`DATA_EXPORTED`, `RCA_APPROVED`,
`RCA_CORRECTED`, `TRENDS_REBUILT`).
## Phase 5 — Knowledge base and RAG (completed)

The Phase 5 commit on `main` (`c611f28`) added the Cohere adapter, migration 005 and a
retrieval test. This completes the phase:

- **Grounded analysis (RAG):** before each analysis job the worker embeds every defect
  (Cohere, `search_query`), retrieves the top-k approved knowledge entries **of the same
  project** above the similarity threshold (Atlas `$vectorSearch`, with an in-process
  cosine fallback for non-Atlas databases), and adds them to the prompt (`rca-v2`).
  The model must list the cases it relied on; a citation of anything not retrieved for
  that defect is rejected and retried, so cited IDs are always a subset of retrieved IDs.
  If embedding or retrieval fails, analysis continues without cases and the reason is
  recorded on the RCA (`retrieval_status`).
- **Learning loop:** approving or correcting an RCA embeds the confirmed RCA
  (`search_document`) and writes it to `knowledge_entries`. The embedding is computed
  before any write, so a provider failure commits nothing (`502
  EMBEDDING_PROVIDER_ERROR`, retryable); a failed knowledge write reverts the review.
  Without `COHERE_API_KEY`, reviews still commit and entries wait as
  `PENDING_EMBEDDING` until the re-embed job runs.
- **Reopen:** `POST /rcas/{id}/reopen` returns a reviewed RCA to the AI proposal and
  removes it from the knowledge base and trends. `"reopen": true` on a review changes
  a confirmed RCA in place.
- **Seed import:** QA Leads upload historical defects with a confirmed-RCA column
  (`Root Cause (RCA)`; optional `SDLC Phase (RCA)` and `Testing Gap`) on the Import tab.
  Defects are imported as usual, labels are mapped to taxonomy codes (by code or label;
  "Code - Front-end" falls back to "Code"), and each becomes an approved RCA
  (`review_source = SEED_IMPORT`) plus a knowledge entry. Unmapped labels are reported,
  not guessed. A plain `SDLC Phase` column is ignored on purpose: in ADO exports it is
  where the defect was found, not where it was introduced.
- **Re-embed job:** `POST /projects/{id}/knowledge/reembed` (`{"force": true}` after an
  embedding-model change) embeds pending entries and entries with a different model.
- **Review screen:** shows the retrieved cases, highlights the ones the AI cited, and
  shows the knowledge-base size and whether RAG is on.
- Seeded RCAs count in trends but not in "AI agreement".

### Setup

1. Add `COHERE_API_KEY` to `.env` (optionally `COHERE_EMBED_MODEL`,
   `COHERE_EMBED_DIMENSIONS`, `RAG_TOP_K`, `RAG_SIMILARITY_THRESHOLD`).
2. Run `python migrations/005_knowledge_rag.py`. On Atlas it also creates the vector
   index `knowledge_vector_index` (cosine, filters `project_id`, `status`, `module`).
3. Restart the ai-knowledge-service and its worker. The worker log shows
   `rag: enabled`.
4. Seed the knowledge base (Import tab → **Seed confirmed RCAs**), then run analysis.

### Endpoints

| Method | Endpoint | Purpose | Min role |
|---|---|---|---|
| POST | `/projects/{pid}/knowledge/seed` | Seed import (multipart `file`) | QA Lead |
| POST | `/projects/{pid}/knowledge/retrieve` | `{"text": "..."}`: cases a text would be grounded on | QA Lead |
| GET | `/projects/{pid}/knowledge/stats` | Entry counts, pending embeddings, RAG on/off | Viewer |
| POST | `/projects/{pid}/knowledge/reembed` | Re-embed job | QA Lead |
| POST | `/rcas/{id}/reopen` (also `/projects/{pid}/rcas/{id}/reopen`) | Undo a review | QA Lead |

`POST /projects/{pid}/rcas/{id}/review` is accepted as an alias of `/rcas/{id}/review`,
and `{"action": ..., "correction": {...}}` bodies as in `POSTMAN_PHASE5.md`.

### Golden set with and without RAG

```bash
python -m services.ai_knowledge_service.evals.run_golden_set          # no RAG
python -m services.ai_knowledge_service.evals.run_golden_set --rag    # needs COHERE_API_KEY
```

`--rag` grounds each golden case on the *other* golden cases' confirmed labels
(leave-one-out), never on its own label.
## Phase 7 — Hardening and release

Everything runs on your own machine or server: no containers and no cloud CI/CD.

- **Delete project data** (Admin tab, or `DELETE /api/v1/projects/{id}/data` with
  `{"confirm_project_id": "<id>"}`): a tracked job removes the project's defects, RCAs,
  knowledge entries, trends and jobs from every service; users, roles, taxonomy and the
  audit log are kept. Idempotent and audited.
- **Operations** (Admin tab, `GET /api/v1/projects/{id}/ops`): failed and stuck jobs,
  provider errors and LLM latency, NEEDS_MANUAL share, AI agreement, LLM/embedding usage
  against daily caps, pending embeddings, sign-in failures and lockouts, with alerts.
  `python ops/check_alerts.py` prints the same alerts as JSON and exits 0/1/2, so Windows
  Task Scheduler (or cron) can run it every 15 minutes.
- **Hardening:** correlation ID on every request (accepted or generated, returned in
  `X-Correlation-ID`, logged by every service, forwarded internally); with `ENV=prod` the
  services refuse weak or placeholder secrets and insecure cookies, and Swagger/OpenAPI is
  off; upload and analysis quotas (`UPLOAD_QUOTA_PER_HOUR`,
  `ANALYSIS_JOBS_PER_PROJECT_PER_DAY`); lint clean.
- **Local release tooling** (`scripts/`):

```powershell
.\scripts\migrate.ps1          # migrations 001-006 against MONGODB_URI
.\scripts\start.ps1            # 4 services + 2 workers + frontend -> http://127.0.0.1:5173
.\scripts\stop.ps1
.\scripts\check.ps1            # ruff, mypy, pytest, pip-audit, npm audit, build, Playwright E2E
```

- **Releases** are git tags (`release-YYYYMMDD`); rolling back means stopping, checking
  out the previous tag and starting again. See
  [deploy and roll back](docs/runbooks/deploy-and-rollback.md).
- **Docs:** [runbooks](docs/runbooks/README.md),
  [OWASP API checklist](docs/security/owasp-api-checklist.md),
  [release readiness](docs/release-readiness.md), restore drill
  `python ops/backup_restore_check.py`.

### E2E tests

```powershell
cd frontend
npx playwright install chromium   # once
npm run e2e
```

### Still owner decisions

Release approver (P5), monitoring tool (P6), data-handling approval for real defect text
(P1), confirmed golden-set labels (P7), retention (P9), MFA (P10). See
[release readiness](docs/release-readiness.md).