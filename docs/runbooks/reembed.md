# Re-embed the knowledge base

**When:** after changing `COHERE_EMBED_MODEL` (all vectors must come from one model),
when Operations shows `EMBEDDINGS_PENDING`, or after a Cohere outage.

1. If the model's dimensions change, set `COHERE_EMBED_DIMENSIONS` and re-run
   `python migrations/005_knowledge_rag.py` (updates the Atlas vector index).
2. Request the job (QA Lead or Admin):
   `POST /api/v1/projects/{id}/knowledge/reembed` with `{"force": true}` after a model
   change, or `{}` to embed only pending / other-model entries.
3. Follow it with `GET /api/v1/jobs/{job_id}`.

**Confirm:** `GET /api/v1/projects/{id}/knowledge/stats` shows `pending_embedding: 0` and
a single model in `embedding_models`.
