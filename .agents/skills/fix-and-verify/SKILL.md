---
name: fix-and-verify
description: Drive a feature to working state end-to-end — start infrastructure, exercise the real endpoint or UI path, read logs and DB state, fix the root cause, restart, and repeat until the golden path passes. Use when the user asks to fix, debug, or verify a feature against the running system.
---

# fix-and-verify

Drive a feature to working state by running it, reading what breaks, fixing it, and repeating. Stop when the golden path produces the expected result with no errors.

## Input

The user names a feature to fix. If they don't, ask: "Which feature or endpoint should I test?"

## Protocol

### 1. Ensure infrastructure is up

```bash
# Postgres and Redis (idempotent)
scripts/deployment/postgres/start.sh
scripts/deployment/redis/start.sh

# Check backend + frontend
curl -sf http://127.0.0.1:8090/api/health > /dev/null || bash scripts/deployment/monitoring-ui/start.sh 8090 5174
```

If the backend is already running but the code has changed since it was started, restart it:
```bash
bash scripts/deployment/monitoring-ui/stop.sh
bash scripts/deployment/monitoring-ui/start.sh 8090 5174
```

Wait until `curl -sf http://127.0.0.1:8090/api/health` returns 200 before continuing.

### 2. Exercise the feature

Hit the real endpoint or UI path that exercises the changed code — not a unit test, not an import check. For an API endpoint:
```bash
curl -s -X POST http://127.0.0.1:8090/api/<path> \
  -H "Content-Type: application/json" \
  -d '{"key": "value"}'
```

Capture the full response (status code + body). If it takes more than a few seconds, run in background and tail the log:
```bash
tail -f logs/monitoring-ui-backend.log
```

### 3. Read the evidence

Always check both:
- **Response body** — error detail, HTTP status
- **Backend log** — `tail -50 logs/monitoring-ui-backend.log`

For database state:
```python
# Run with: uv run python check.py
from src.product_components.news_fetcher.env_loader import load_env_files
from pathlib import Path
load_env_files(Path('.'), filenames=('.env.shared', '.env.prod', '.env.thesis-builder', '.env.secrets'), override_existing=False)
import os, psycopg
dsn = (
    f"host={os.getenv('POSTGRES_HOST','127.0.0.1')} port={os.getenv('POSTGRES_PORT','5432')} "
    f"dbname={os.getenv('POSTGRES_DATABASE','trader')} user={os.getenv('POSTGRES_USER','trader')} "
    f"password={os.getenv('POSTGRES_PASSWORD','')} sslmode=disable"
)
with psycopg.connect(dsn) as conn:
    with conn.cursor() as cur:
        cur.execute("SELECT ...")
        print(cur.fetchall())
```

### 4. Diagnose and fix

Identify the root cause from the evidence. Common patterns in this codebase:

| Symptom | Likely cause |
|---|---|
| 503 with "unavailable" | `_run_with_infrastructure_mapping` caught an exception — read `LOGGER.exception` in backend log |
| SQL error "could not determine data type" | Parameter used only in `IS NULL` — change to `COALESCE(col, '') = COALESCE(%s, '')` |
| INFO logs not appearing in backend log | Root logger has no handler — add `StreamHandler` before `uvicorn.run()` in `__main__.py` |
| `cards_created = 0` despite evidence windows satisfied | Check `expires_at` — may be using injected clock (historical date); `_load_unpublished_signal` then fails `expires_at > NOW()` |
| Token budget not enforced | LLM self-reports tiny `estimated_tokens`; fix by overriding with `response.usage.total_tokens` in `OpenAIThesisClient.analyze()` |
| `ON CONFLICT DO NOTHING` silently skips insert | Idempotency key collides across runs — include `run_id` in the key when reprocessing |

Apply the minimal targeted fix. Do not refactor or clean up beyond what the bug requires.

### 5. Restart and re-test

After changing Python backend code, always restart:
```bash
bash scripts/deployment/monitoring-ui/stop.sh && sleep 2
bash scripts/deployment/monitoring-ui/start.sh 8090 5174
```

Frontend code (TypeScript/TSX) is hot-reloaded by Vite — no restart needed.

Then return to step 2.

### 6. Done criteria

- The feature returns the expected result (correct HTTP 200 + meaningful response body)
- The backend log shows the expected INFO messages with no `ERROR` or `EXCEPTION` lines for the exercised path
- If the feature writes to the database, a direct DB query confirms the expected rows were created

## Reporting

When done, report:
```
## fix-and-verify: <feature name>

**Verdict:** PASS | FAIL | BLOCKED

**Iterations:** <n>
**Root causes fixed:**
- <bug 1>: <one-line description>
- <bug 2>: ...

**Final result:** <response body / key metrics>
```
