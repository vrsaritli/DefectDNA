# DefectDNA runbooks

Operational procedures for the release (Architecture §38 item 9). Each runbook lists
when to use it, the steps, and how to confirm it worked.

| Runbook | Use when |
|---|---|
| [Deploy and roll back](deploy-and-rollback.md) | Releasing a version, or undoing one |
| [Secrets rotation](secrets-rotation.md) | A key leaked, a person left, or on the rotation schedule |
| [Failed or stuck jobs](failed-jobs.md) | Operations shows failed/stuck jobs, or analysis does not finish |
| [Provider outage (Groq / Cohere)](provider-outage.md) | Provider error alerts, many NEEDS_MANUAL_RCA |
| [Re-embed the knowledge base](reembed.md) | Embedding model change, or entries pending embedding |
| [Delete project data](delete-project-data.md) | An owner asks for a project's data to be removed |
| [Backup and restore](backup-and-restore.md) | Restore drill, or recovering lost data |

**Alerts.** `python ops/check_alerts.py` exits 0 (ok), 1 (warning) or 2 (critical) and
prints the alerts as JSON. Schedule it every 15 minutes on whatever the platform offers
(cron, Task Scheduler, a platform job) and notify on a non-zero exit. The same alerts
are on the Admin → Operations tab. Monitoring tooling itself is still pending (P6).
