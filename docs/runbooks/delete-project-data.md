# Delete project data

**Who:** an Admin of that project, on the owner's written request (keep the request).

1. Optional but recommended: take an export first
   (`python ops/backup_restore_check.py --project <id> --export-dir <secure-dir>`), and
   agree how long that export is kept.
2. Admin → **Delete project data**, type the project ID, confirm. (API:
   `DELETE /api/v1/projects/{id}/data` with `{"confirm_project_id": "<id>"}`.)
3. The job runs in order: ai-knowledge (queued analysis jobs first, since they hold
   defect text; then RCAs, knowledge entries, usage counters), trends, ingestion (defects,
   import batches).

**Kept on purpose:** users, roles, the project record, taxonomies, and the append-only
audit log (which records who requested the deletion and what was removed).

**Confirm:** the job shows SUCCEEDED with per-service counts in the audit entry
`PROJECT_DATA_DELETED`; defect list, RCAs and trends are empty. If a step failed, request
the deletion again (idempotent).

**Backups:** Atlas backups still contain the data until they age out under the backup
policy. Retention beyond the active project lifetime is pending (P9).
