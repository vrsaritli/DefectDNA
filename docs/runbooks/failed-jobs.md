# Failed or stuck jobs

Background work runs as documents in the `jobs` collection: `analyze_defects` and
`reembed_all` (ai-knowledge worker), `trend_fact` (trends worker), `delete_project_data`
(gateway).

## See what is wrong

- Admin → Operations: failed jobs (with error code) and jobs stuck past their lease.
- `python ops/check_alerts.py --project <id>`.
- `GET /api/v1/jobs/{job_id}`: progress and per-item errors (`ADO-1234: NEEDS_MANUAL_RCA: ...`).

## Fix

| Symptom | Cause | Action |
|---|---|---|
| `analyze_defects` stuck in RUNNING | Worker stopped mid-job | Restart the ai-knowledge worker. Leases expire after 10 minutes and the job resumes from the last finished batch; nothing is analyzed twice. |
| Analysis job FAILED `MAX_ATTEMPTS_EXCEEDED` | Job crashed three times | Check worker logs for the job ID (logs carry IDs and codes, never defect text). Fix the cause, then run analysis again; already-analyzed defects are skipped. |
| Items `LLM_DAILY_CAP_REACHED` | `LLM_DAILY_CALL_CAP` hit | Wait for the next UTC day or raise the cap deliberately; re-run analysis. |
| Items `NEEDS_MANUAL_RCA: LLM_*` | Provider problem | See [provider outage](provider-outage.md); then *Re-analyze unreviewed*. |
| `trend_fact` FAILED `INVALID_TREND_FACT` | Malformed payload | Dashboard → **Rebuild** re-derives every trend fact from reviewed RCAs. |
| Trends missing after a worker outage | Trends worker was down | Restart it (queued facts are processed), or **Rebuild**. |
| `delete_project_data` FAILED | One service was unreachable | Request the deletion again: every step is idempotent. |
| `reembed_all` FAILED `EMBEDDING_*` | Cohere problem | Fix the key/outage and request the re-embed again. |

## Confirm

The job shows SUCCEEDED; Operations no longer lists it as failed or stuck.
