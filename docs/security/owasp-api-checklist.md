# OWASP API Security Top 10 (2023) — DefectDNA checklist

Reviewed 2026-11-22 for the Phase 7 release candidate. Evidence points to code and tests.
Re-run before every production release (Architecture §22).

| # | Risk | Status | Evidence |
|---|---|---|---|
| API1 | Broken object-level authorization | ✅ | Every project route checks the caller's role **on that project** (`_require_project_role`); objects addressed by ID (`/rcas/{id}`, `/jobs/{id}`) resolve their project first, then check the role. Tests: cross-project 403 on imports, analyses, trends, exports, RCA detail (`test_rca_routes`, `test_trends_export_routes`); project-scoped review alias returns 404 for another project's RCA (`test_knowledge_routes`). |
| API2 | Broken authentication | ✅ | Argon2id, 12+ char policy with common-password list, lockout after `LOGIN_MAX_FAILURES`, server-side sessions with idle + absolute expiry, HttpOnly/Secure/SameSite=Strict cookie, CSRF token on every state change, sessions revoked on password change/reset (`test_authentication`). Internal services accept only short-lived HMAC service tokens. MFA pending (P10). |
| API3 | Broken object-property-level authorization | ✅ | Request bodies are Pydantic models (unknown fields ignored or rejected); responses are built explicitly (`enrich_rca`, `_user_summary`), never raw documents: no password hashes, embeddings, prompt text or scrub output are returned. |
| API4 | Unrestricted resource consumption | ✅ | 120 req/min per user/IP; uploads `UPLOAD_QUOTA_PER_HOUR` per user (imports, ADO sync, seed); analysis jobs `ANALYSIS_JOBS_PER_PROJECT_PER_DAY`; LLM and embedding daily call caps; file size/row limits; page size ≤ 100; analysis batch ≤ `MAX_ANALYSIS_BATCH`; per-request token ceiling 4,500 (`test_phase7_hardening`). |
| API5 | Broken function-level authorization | ✅ | Role matrix (§8) enforced server-side: approve/correct/reject = QA Lead or Admin (audited); knowledge ingestion, delete data, settings, ops = project Admin; Viewer read-only. Tests: Viewer cannot review, Viewer cannot import/rebuild, QA Lead cannot delete data. UI hiding is cosmetic only. |
| API6 | Unrestricted access to sensitive business flows | ✅ | Data deletion needs a typed confirmation and is audited; analysis quota per project per day; reviews are idempotent (`409 ALREADY_REVIEWED` unless `reopen`). |
| API7 | Server-side request forgery | ✅ | No user-supplied URLs are fetched. ADO organization URL comes from server config only, must be HTTPS without user info; provider base URLs are server config. |
| API8 | Security misconfiguration | ✅ | Security headers on the gateway (CSP, X-Frame-Options, nosniff, HSTS on HTTPS, no-store) and nginx; production refuses weak/placeholder secrets and insecure cookies; OpenAPI/Swagger disabled in production; uniform error envelope without stack traces; services bind to 127.0.0.1 in `scripts/start.ps1` (only the frontend/proxy is meant to be reachable). |
| API9 | Improper inventory management | ✅ | Single versioned public API `/api/v1` on the gateway; internal `/internal/*` routes need a service token and are not published; endpoint tables in README per phase; Postman doc aligned (`POSTMAN_PHASE5.md`). |
| API10 | Unsafe consumption of APIs | ✅ | LLM output is schema-constrained, validated against the taxonomy, citations checked against retrieved cases, anything else rejected; defect text delimited and labelled untrusted (prompt-injection guard); provider errors mapped to codes without echoing provider bodies; timeouts, retries with backoff, `Retry-After` honoured; embedding dimensions verified. |

## Related controls

- **Data handling (NFR-05):** text is scrubbed (emails, URLs, internal hosts, IPs,
  tokens/keys, GUIDs, long numbers) before Groq or Cohere; AI analysis is off by default
  for production projects until an Admin enables it. Approval to send real defect text
  (P1) is still an owner decision.
- **Logging:** JSON logs with correlation IDs; no defect text, prompts, passwords,
  tokens or keys (`test_job_runs_end_to_end_without_logging_defect_text`).
- **CSV/Excel injection:** export cells starting with `= + - @ \t \r` are neutralised
  (`test_exports_are_injection_safe_and_audited`).
- **Dependencies:** `pip-audit` and `npm audit` run in `scripts/check.ps1` before every
  release. 2026-11-22: npm 0 findings; Python runtime dependencies 0 findings; pytest
  (dev) bumped to 9.0.3+ for PYSEC-2026-1845; the venv's old setuptools upgraded.

## Open items (owner decisions)

| Item | Ref |
|---|---|
| MFA for local accounts | P10 |
| Data-handling approval for real defect text | P1 |
| Monitoring/alerting tool (alerts currently via `ops/check_alerts.py` exit codes) | P6 |
| Atlas network access: restrict to the host's IP (Atlas allowlist) | P2 |
