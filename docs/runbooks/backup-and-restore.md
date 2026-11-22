# Backup and restore

**Primary backup:** MongoDB Atlas cloud backups (enable continuous backup or daily
snapshots on the production cluster; NFR-03). Restores of a whole cluster or a point in
time are done in the Atlas UI: *Cluster → Backup → Restore*.

## Restore drill (run monthly and before each production release)

```bash
python ops/backup_restore_check.py --project <id>              # dry run: counts only
python ops/backup_restore_check.py --project <id> --execute    # full drill
```

The drill reads one project's documents from every collection, restores them into a
scratch database `<MONGODB_DB>_restore_check`, compares counts and a content checksum per
collection, then drops the scratch database. It never writes to the live database.
Exit code 0 = restore verified.

Record the result (date, project, OK/failed) in the release notes.

## Recover one project from an export

`--export-dir` writes one Extended-JSON file per collection. To restore into the live
database, load those files with `mongoimport --jsonArray` (or Atlas Data Explorer),
then **Dashboard → Rebuild** to re-derive trends and request a knowledge re-embed if
the embedding model changed since the export.
