# Provider outage (Groq / Cohere)

**Signals:** `PROVIDER_ERRORS` or `LLM_LATENCY_HIGH` alerts; many `NEEDS_MANUAL_RCA` with
reasons such as `LLM_UNAVAILABLE`, `LLM_TIMEOUT`, `LLM_RATE_LIMITED`, `LLM_AUTH_FAILED`;
`retrieval_status` like `EMBEDDING_UNAVAILABLE` on new RCAs; approvals returning
`502 EMBEDDING_PROVIDER_ERROR`.

## What keeps working

- Groq down: imports, browsing, reviews, trends and export keep working. Analysis marks
  items `NEEDS_MANUAL_RCA` with the reason; nothing is corrupted.
- Cohere down: analysis continues **without** past cases (recorded per RCA). Approvals
  fail with 502 and commit nothing, so the QA Lead can retry later. Without any Cohere
  key configured, approvals commit and entries wait as `PENDING_EMBEDDING`.

## Steps

1. Check the provider's status page. `*_AUTH_FAILED` means the key is wrong or revoked:
   see [secrets rotation](secrets-rotation.md).
2. `*_RATE_LIMITED`: lower `LLM_MAX_DEFECTS_PER_BATCH`, or spread analysis out; the
   adapter already retries with backoff and honours `Retry-After`.
3. When the provider is back: AI analysis → **Re-analyze unreviewed** for the affected
   defects; Admin → request a knowledge re-embed if entries are pending.
4. Long outage: QA Leads can classify defects manually (**Classify manually**); those
   confirmed RCAs still enter the knowledge base.

## Confirm

New analysis produces `AI_PROPOSED` results; provider alerts clear in Operations.
