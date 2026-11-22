# RCA golden set

`run_golden_set.py` sends the golden set through the same pipeline the worker uses
(scrub → token-budgeted batches → Groq → validate → one retry) and writes a JSON and a
Markdown report to `reports/`. Run it after any prompt, model, taxonomy or budget change
(NFR-06). Reports contain case IDs, codes and token counts only, not defect text.

## Golden set (`golden_set.json`)

Built by `build_golden_set.py` from `SAmple_demo_Prod_defects.xlsx` and
`SAmple_demo_UAT_defects.xlsx` (fictional data from the team's demo ADO project):

- 120 defects; 68 carry a QA **Root Cause (RCA)** value.
- A defect is a golden case only when that column and the `[RCA review]` comment name
  the same category: **42 cases**. 26 rows were excluded because the two disagree
  (for example, column "Code" but review comment "Requirement gap").
- The `[RCA review]` comment is removed from model input (it states the answer) and
  comment author names are dropped.
- Only `root_cause_code` is scored; the exports have no confirmed SDLC phase or
  testing gap.

## Baseline — 2026-11-22, `rca-v1`, `openai/gpt-oss-120b`, medium reasoning

| Metric | Value |
|---|---|
| Root-cause match rate | **16.7%** (7/42) |
| Stored outputs conforming to taxonomy | 100% |
| AI proposals / NEEDS_MANUAL_RCA | 42 / 0 |
| Groq requests | 12 (3–4 defects each) |
| Tokens per request, mean / max | 3,274 / 4,106 (ceiling 4,500) |

Full report: [reports/20261122T063658Z-rca-v1-medium.md](reports/20261122T063658Z-rca-v1-medium.md)

### How to read this baseline

The match rate is close to chance for seven categories, and the cause is the labels,
not the model. In the sample data, the confirmed category was assigned independently
of the developer's root-cause comment, so the label often contradicts the evidence the
model reads. Examples from the high-confidence misses:

| Case | Developer comment says | Golden label | Model |
|---|---|---|---|
| ADO-462 | holiday calendar table was not loaded for the current year | REQUIREMENTS | DATA |
| ADO-370 | cache key missed the tenant id | CONFIGURATION | CODE |
| ADO-714 | filter used a strict less-than on the upper bound | TEST_GAP | CODE |
| ADO-337 | migration script was not run in the release | CONFIGURATION | DEPLOYMENT |
| ADO-322 | notification service renamed the order-placed event | THIRD_PARTY | INTEGRATION |

In each case the model's answer matches the developer evidence. This baseline
therefore measures label quality as much as AI quality. Before Phase 5 compares RAG
against this baseline, a QA Lead should confirm or correct the 42 labels (pending
item P7: "golden-set defects with confirmed RCAs").

### rca-v2 without RAG — 2026-11-22

Same golden set, `rca-v2` prompt, no knowledge base: **19.0%** (8/42), 100%
conformance, 15 requests of 3 defects, mean 3,206 / max 3,535 tokens. The one-case
difference from rca-v1 is within noise. The RAG comparison (`--rag`) needs
`COHERE_API_KEY` and has not been run yet.
### rca-v2 with RAG (leave-one-out) — 2026-11-22

Each case grounded on the other 41 golden cases (Cohere `embed-english-v3.0`, top-k 5,
threshold 0.5): **21.4%** (9/42), against 19.0% without RAG. 39/42 cases retrieved at
least one similar case (mean 2.0); the AI cited cases for 18. Citation validity 100%
(every cited case was retrieved for that defect). 17 requests, mean 3,576 / max 4,236
tokens, none over the 4,500 ceiling.

The gain is within noise, as expected from the labels: each case's most similar
neighbour carries the same golden root cause in only 7 of 42 cases, so retrieved
"confirmed" labels often contradict each other. RAG can only help once the golden and
seed labels are consistent; re-run this comparison after a QA Lead confirms them.

Report: [reports/20261122T095239Z-rca-v2-medium-rag.md](reports/20261122T095239Z-rca-v2-medium-rag.md)