# Deploy and roll back

DefectDNA runs directly on a Windows machine or server: Python services and workers
plus the frontend, with no containers and no cloud CI/CD. A release is a git tag.

## Release

1. On `main`, run the checks: `.\scripts\check.ps1` (ruff, mypy, pytest, pip-audit,
   npm audit, frontend build, Playwright E2E). Every step must pass.
2. Approval: the release approver (pending decision P5) reviews the change list and the
   check output, then tags it:
   `git tag -a release-YYYYMMDD -m "<summary>"` and `git push origin release-YYYYMMDD`.
3. On the target machine:

```powershell
.\scripts\stop.ps1
git fetch --tags
git checkout release-YYYYMMDD
.\.venv\Scripts\python -m pip install -e .
cd frontend; npm ci; cd ..
.\scripts\migrate.ps1
.\scripts\start.ps1
```

4. Confirm: `start.ps1` reports all four ports ready, Admin → Operations shows no new
   critical alert, and a sign-in works. Record the tag and time in the release notes.

Migrations are additive and idempotent (indexes, starter taxonomy, vector index), so
they are safe to re-run and need no reverting on rollback.

## Roll back

```powershell
.\scripts\stop.ps1
git checkout <previous release tag>
.\.venv\Scripts\python -m pip install -e .
cd frontend; npm ci; cd ..
.\scripts\start.ps1
```

Confirm as above, then record the failing tag and what broke.

## Production configuration checks

With `ENV=prod` in `.env`, services refuse to start when a secret is shorter than 32
bytes or still a placeholder, `MONGODB_URI` is missing, or `SESSION_COOKIE_SECURE` is
false; Swagger/OpenAPI pages are off. Serve the app over HTTPS (for example IIS or
nginx for Windows in front of port 5173/8000) so the Secure session cookie works. AI
analysis stays off per project until an Admin enables it
(`PUT /api/v1/projects/{id}/settings`): that is the data-handling gate (P1).
