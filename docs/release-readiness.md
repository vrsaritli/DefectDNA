# Release readiness — Definition of Done (Architecture §38)

Status of the MVP release checklist after Phase 7 (2026-11-22).

| # | Item | Status | Notes |
|---|---|---|---|
| 1 | FR-01…FR-13 end to end (upload → analyze → review → knowledge base → trends → export) | ✅ | Phases 3–6 plus the Phase 5 completion; FR-13 data deletion added in Phase 7 |
| 2 | Authorization matrix tests; cross-project access blocked | ✅ | See OWASP API1/API5 evidence |
| 3 | Golden-set accuracy measured; RAG vs non-RAG recorded | ✅ | 16.7% (rca-v1), 19.0% (rca-v2), 21.4% (rca-v2 + RAG). Golden labels are inconsistent; QA must confirm them before accuracy is meaningful (P7) |
| 4 | Citation validity 100% | ✅ | Enforced by validation; measured 100% in the RAG run |
| 5 | Baseline security controls verified; OWASP API checklist | ✅ | [security/owasp-api-checklist.md](security/owasp-api-checklist.md) |
| 6 | Data-handling approval (P1) before production use of real data | ⏳ Owner | Production projects keep AI analysis off until an Admin enables it |
| 7 | E2E tests for key flows | ✅ | Playwright (sign-in/password change → dashboard; review with cited case → approve) + in-process cross-service test (delete project data) |
| 8 | Production deployment, rollback and restore rehearsed | ⏳ Rehearsal | Local release by git tag with `scripts/check.ps1`, `migrate.ps1`, `start.ps1`/`stop.ps1`; rollback = previous tag ([runbook](runbooks/deploy-and-rollback.md)). Restore drill script ready (`ops/backup_restore_check.py`). One rollback and one restore drill still to be rehearsed on the target machine |
| 9 | Runbooks | ✅ | [runbooks/](runbooks/README.md) |
| 10 | PENDING items resolved or accepted | ⏳ Owner | P1 data handling, P5 release approver, P6 monitoring tool, P7 golden-set labels, P9 retention, P10 MFA. P2 is decided: run locally, no containers or cloud CI/CD |

**To go live:** name the release approver (P5), record the data-handling approval (P1),
run `.\scripts\check.ps1`, tag the release, follow
[deploy-and-rollback](runbooks/deploy-and-rollback.md), then rehearse one rollback and one
restore drill.
