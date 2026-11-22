# Secrets rotation

Secrets live only in the git-ignored `.env` on each machine (or a secrets manager if one is
adopted). Never in code, git history, logs or the browser. After changing a value,
restart with `.\scripts\stop.ps1` and `.\scripts\start.ps1`.

| Secret | Effect of rotating | Steps |
|---|---|---|
| `SESSION_SIGNING_KEY` | Every user is signed out | Generate 48 random bytes (`python -c "import secrets;print(secrets.token_urlsafe(48))"`), update the gateway secret, restart |
| `SERVICE_TOKEN_SECRET` | Gateway ↔ service calls fail until all services share the new value | Update the secret for **all four** services, restart them together |
| `MONGODB_URI` (password) | Brief reconnect | Create a new Atlas database user/password, update `.env`, restart, then delete the old Atlas user |
| `GROQ_API_KEY` | None if done in order | Create a new key in the Groq console, update the ai-knowledge service + worker, restart, revoke the old key |
| `COHERE_API_KEY` | None if done in order | Same as Groq, in the Cohere dashboard |
| `ADO_PAT` | ADO sync fails until updated | Create a new **read-only** PAT (Work Items: Read), update ingestion, restart, revoke the old PAT |

**Schedule:** every 90 days, and immediately when a key was exposed (pasted in chat,
committed, logged) or someone with access leaves.

**Confirm:** `/health/ready` is OK on all services; Admin → Operations shows no provider
errors after the next analysis; a sign-in works.

**If a secret was committed:** rotate first, then remove it from history. Rotation is the
fix; rewriting history alone is not enough.
