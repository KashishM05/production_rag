# BetterDB RAG Demo — Step by Step

Complete walkthrough for the FastAPI RAG application demo covering all 5 BetterDB features.

---

## Architecture

```
PDF Upload → FastAPI (/ingest) → OpenAI Embeddings → Redis (Upstash OR local Valkey)
                                                              ↓
User Query → FastAPI (/query) → Rate Limit Check    rag:doc:{sha256}
                              → Semantic Cache Check semantic_cache:{md5}
                              → HGETALL Retrieval   rate_limit:user_{id}:*
                              → OpenAI LLM          langchain:memory:session:{id}
                              → Cache Store
                              → Session Write
                                   ↓
                     BetterDB Agent (Docker) → BetterDB Cloud Dashboard
```

### Redis options

| | Upstash (cloud) | Local Valkey (Docker) |
|---|---|---|
| `REDIS_URL` | `rediss://default:TOKEN@quiet-flea-79665.upstash.io:6379` | `redis://localhost:6379` |
| TLS | Required (`rediss://`) | None (`redis://`) |
| HGETALL latency | ~264ms (network RTT) | ~0.1ms (loopback) |
| Full scan (21 keys) | ~6000ms | ~5ms |
| BetterDB SLOWLOG | Empty (Upstash restricts) | Works (real HGETALL entries) |
| Rate limit 429 | Parallel requests only | Sequential requests too |
| Setup | Zero infra | `docker-compose up -d` |

---

## Prerequisites

| Tool | Version | Status |
|---|---|---|
| Python | 3.12+ | Required |
| Docker | any | Required (BetterDB agent) |
| OpenAI API Key | — | Required |
| Upstash Redis | free tier | Required |
| BetterDB Cloud | free | Required |

---

## Section 1 — One-Time Setup

### 1.1 Install dependencies

```bash
cd /path/to/betterdb-yt-collab
uv pip install fastapi uvicorn pypdf numpy pydantic-settings python-multipart openai redis python-dotenv
```

### 1.2 Configure .env

```dotenv
# BetterDB
BETTERDB_URL=https://betterdb-test1.app.betterdb.com
BETTERDB_TOKEN=<your_betterdb_token>

# OpenAI
OPENAI_API_KEY=sk-...

# ── OPTION A: Upstash Redis (cloud, zero infra) ───────────────────────────────
# TCP URL with TLS — note double-s in rediss://
# Get password: Upstash Dashboard → your DB → TCP tab → uncheck "Read-Only Token" → copy
REDIS_URL=rediss://default:<your_upstash_token>@quiet-flea-79665.upstash.io:6379

# ── OPTION B: Local Valkey (Docker, better for Feature 5 slowlog demo) ────────
# REDIS_URL=redis://localhost:6379
```

### 1.3 Start Redis

**Option A — Upstash:** Nothing to start. Already running in cloud.

**Option B — Local Valkey:**
```bash
docker-compose up -d
docker-compose ps    # wait for "healthy"
```

### 1.4 Connect Redis to BetterDB cloud

BetterDB cloud → Manage Connections → **+ Add Connection** → **Via Agent** tab → create token.

**Option A — Upstash (Direct Connection tab):**
```bash
docker run -d \
  --name betterdb-agent \
  -e VALKEY_HOST=quiet-flea-79665.upstash.io \
  -e VALKEY_PORT=6379 \
  -e VALKEY_TLS=true \
  -e VALKEY_USERNAME=default \
  -e VALKEY_PASSWORD=<your_upstash_write_token> \
  -e BETTERDB_CLOUD_URL=wss://betterdb-test1.app.betterdb.com/agent/ws \
  -e BETTERDB_TOKEN=<betterdb_agent_token> \
  betterdb/agent:latest
```

**Option B — Local Valkey:**
```bash
docker run -d \
  --name betterdb-agent-local \
  -e VALKEY_HOST=host.docker.internal \
  -e VALKEY_PORT=6379 \
  -e BETTERDB_CLOUD_URL=wss://betterdb-test1.app.betterdb.com/agent/ws \
  -e BETTERDB_TOKEN=<betterdb_agent_token> \
  betterdb/agent:latest
```

> `host.docker.internal` — lets the Docker container reach your Mac's `localhost:6379`.
> On Linux replace with `172.17.0.1`.

Verify agent connected:
```bash
docker logs betterdb-agent        # Upstash
docker logs betterdb-agent-local  # Local Valkey
# Expected: [Agent] WebSocket connected, sending hello
```

### 1.4 Start FastAPI server

```bash
uvicorn rag.main:app --reload --port 8000
```

### 1.5 Health check

```bash
curl http://localhost:8000/health
```

Expected:
```json
{"redis": "ok", "key_counts": {"rag:doc:": 0, "semantic_cache:": 0, "rate_limit:": 0, "langchain:memory:session:": 0}}
```

---

## Section 2 — Ingest a PDF

```bash
curl -F "file=@BetterDB_YouTube_Proposal.pdf" \
     -H "X-User-ID: student1" \
     http://localhost:8000/ingest
```

Expected:
```json
{"chunks_stored": 21, "source": "BetterDB_YouTube_Proposal.pdf", "keys_preview": ["rag:doc:abc...", ...]}
```

Check stats:
```bash
curl http://localhost:8000/stats
```

**BetterDB observation:**
- Open BetterDB → krishai connection → Key Analytics
- `rag:doc:*` = 21 keys, ~31 KB/key, **w/TTL = 0** (no expiry)

---

## Section 3 — Feature 1: MCP Server Debug

**What it demonstrates:** Ask Claude Code plain-English questions — BetterDB answers from real Redis data. No dashboards needed.

### Setup MCP in Claude Code

`~/.claude/claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "betterdb": {
      "type": "stdio",
      "command": "npx",
      "args": ["-y", "@betterdb/mcp"],
      "env": {
        "BETTERDB_URL": "https://betterdb-test1.app.betterdb.com",
        "BETTERDB_TOKEN": "<your_token>"
      }
    }
  }
}
```

### Questions to ask Claude Code (via MCP)

```
"What are the slowest commands in the last 24h?"
"Show me memory breakdown by namespace"
"Who are the top clients by command count?"
"Show me any anomalies detected"
"What keys have no TTL?"
"Show me the complete incident timeline"
```

Each question maps to a BetterDB MCP tool call that returns real data from your Upstash instance.

---

## Section 4 — Feature 2: Semantic Cache TTL Bug

**What it demonstrates:** `semantic_cache:*` and `rag:doc:*` keys are written with no TTL — silent memory bloat.

### Step 1: First query (cache MISS)

```bash
curl -X POST http://localhost:8000/query \
     -H "Content-Type: application/json" \
     -H "X-User-ID: student1" \
     -d '{"query": "What is BetterDB?", "session_id": "demo"}'
```

Expected response:
```json
{"response": "...", "cache_hit": false, "session_id": "demo", "docs_used": 3, "latency_ms": 4200}
```

### Step 2: Same query again (cache HIT)

```bash
curl -X POST http://localhost:8000/query \
     -H "Content-Type: application/json" \
     -H "X-User-ID: student1" \
     -d '{"query": "What is BetterDB?", "session_id": "demo"}'
```

Expected response:
```json
{"response": "...", "cache_hit": true, "session_id": "demo", "docs_used": 0, "latency_ms": 450}
```

### Step 3: Similar query (still a cache HIT — semantic similarity)

```bash
curl -X POST http://localhost:8000/query \
     -H "Content-Type: application/json" \
     -H "X-User-ID: student1" \
     -d '{"query": "Can you explain what BetterDB is?", "session_id": "demo"}'
```

### Step 4: Observe in BetterDB

- Key Analytics → `semantic:cache:*` → **w/TTL = 0**
- Memory grows with every new unique query
- No expiry = unbounded growth

### Question to ask Claude Code

```
"Why does my semantic cache have no TTL, and what happens to hit quality as data changes?"
```

### The bug in code (`rag/pipeline.py:98`)

```python
# BUG — no expire
r.hset(cache_key(query), mapping={...})

# FIX
r.hset(cache_key(query), mapping={...})
r.expire(cache_key(query), 60 * 60 * 24 * 7)  # 7 days

# Fix rag:doc:* too
r.hset(rag_doc_key(chunk), mapping={...})
r.expire(rag_doc_key(chunk), 60 * 60 * 24 * 30)  # 30 days
```

---

## Section 5 — Feature 3: Agent Memory Runaway

**What it demonstrates:** `langchain:memory:session:*` hash grows with every query — no TTL, no size limit.

### Step 1: Run multiple queries with same session_id

```bash
for Q in \
  "What is BetterDB?" \
  "How does it help with Redis debugging?" \
  "What are the 5 features?" \
  "Explain semantic cache TTL bug" \
  "What is agent memory observability?"; do
  curl -s -X POST http://localhost:8000/query \
       -H "Content-Type: application/json" \
       -H "X-User-ID: student1" \
       -d "{\"query\": \"$Q\", \"session_id\": \"runaway-demo\"}" | python3 -m json.tool | grep latency_ms
done
```

### Step 2: Observe in BetterDB

- Key Analytics → `langchain:memory:session:*`
- Key count stays **1** (same HASH key)
- **Memory grows** with each query (850B → 2KB → 4KB → ...)
- **w/TTL = 0** — grows forever

### Step 3: Check session contents directly

```bash
curl http://localhost:8000/stats
# Shows langchain:memory:session:runaway-demo key size
```

### Question to ask Claude Code

```
"Show agent memory observability — which session key is growing and what is the runaway pattern?"
```

### The bug in code (`rag/pipeline.py:126`)

```python
# BUG — no expire, no size limit
r.hset(session_key(session_id), f"msg_{idx}", json.dumps(...))

# FIX — rolling window (keep last 20 messages)
pipe = r.pipeline()
pipe.hset(session_key(session_id), f"msg_{idx}", json.dumps(...))
pipe.expire(session_key(session_id), 7200)  # 2 hour TTL
pipe.execute()
# + Add max field count check to trim old messages
```

---

## Section 6 — Feature 4: Rate Limiter Abuse Detection

**What it demonstrates:** Redis INCR+EXPIRE pattern for rate limiting. BetterDB shows the burst pattern.

### Step 1: Normal queries (under limit)

```bash
# 5 queries — all should return 200
for i in $(seq 1 5); do
  curl -s -o /dev/null -w "Query $i: HTTP %{http_code}\n" \
       -X POST http://localhost:8000/query \
       -H "Content-Type: application/json" \
       -H "X-User-ID: abuser" \
       -d '{"query": "test query '$i'", "session_id": "test"}'
done
```

### Step 2: Burst attack — trigger 429

Paste this entire block directly into terminal. Each `&` fires in background — all 20 launch simultaneously. `wait` holds until all finish.

```bash
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"What is BetterDB?","session_id":"default"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"How does semantic cache work?","session_id":"default"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"What is RAG pipeline?","session_id":"default"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"What is agent memory observability?","session_id":"default"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"How does BetterDB detect TTL bugs?","session_id":"default"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"What is rate limiter abuse detection?","session_id":"default"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"How does HGETALL cause latency?","session_id":"default"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"What is persistent slowlog?","session_id":"default"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"How does BetterDB use MCP server?","session_id":"default"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"What Redis keys does a RAG app generate?","session_id":"default"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"What is TTL coverage percentage?","session_id":"default"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"How does BetterDB anomaly detection work?","session_id":"default"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"What is semantic similarity threshold?","session_id":"default"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"How to fix memory fragmentation in Redis?","session_id":"default"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"What is the difference between hot keys and stale keys?","session_id":"default"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"How does BetterDB compare to RedisInsight?","session_id":"default"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"What is LangChain memory runaway?","session_id":"default"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"How does embedding cosine similarity work?","session_id":"default"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"What is the INCR EXPIRE pattern for rate limiting?","session_id":"default"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"How does BetterDB help with AI observability?","session_id":"default"}' &
wait && echo "All 20 done"
```

Expected output:
```
Query  1: HTTP 200
Query  2: HTTP 200
...
Query 10: HTTP 200
Query 11: HTTP 429   ← rate limit hit
Query 12: HTTP 429
...
```

### Step 3: Observe in BetterDB

- Key Analytics → `rate:limit:user:abuser:*`
- `rate:limit:user:abuser:minute` — **w/TTL = 1** (60s window), value = 20
- `rate:limit:user:abuser:hour` — **w/TTL = 1** (3600s window)
- Click **Trigger Collection** immediately after burst to capture minute key before TTL expires

### Question to ask Claude Code

```
"Show me per-client INCR/EXPIRE command breakdown for rate_limit:* keys. Any burst patterns?"
```

### The rate limit code (`rag/pipeline.py:49`)

```python
# Correct pattern — rate_limit keys DO have TTL (contrast with rag:doc:* which don't)
cnt = r.incr(f"rate_limit:user_{user_id}:minute")
if cnt == 1:
    r.expire(f"rate_limit:user_{user_id}:minute", 60)
if cnt > 10:
    raise HTTPException(429, "Rate limit exceeded")
```

---

## Section 6b — Burst Test: Combined Rate Limit + Session Runaway

**How this differs from Section 6:**

| | Section 6 | Section 6b (Burst Test) |
|---|---|---|
| `session_id` | `"default"` | `"burst:test"` |
| Purpose | Show 429 rate limit only | Show 429 + session memory growing simultaneously |
| BetterDB signals | `rate:limit:*` keys | `rate:limit:*` AND `langchain:memory:session:burst:test` both change |
| What you watch | Rate limit keys appear | Rate limit burst + session runaway in same test |

**In one burst you see 3 features at once:**
- **Feature 2** — `semantic:cache:*` grows (new unique queries cached, no TTL)
- **Feature 3** — `langchain:memory:session:burst:test` memory grows (same key, bigger HASH)
- **Feature 4** — queries 11–20 return `HTTP 429`

### Run the burst test

Paste entire block into terminal — all 20 fire simultaneously:

```bash
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"What is BetterDB?","session_id":"burst:test"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"How does semantic cache work?","session_id":"burst:test"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"What is RAG pipeline?","session_id":"burst:test"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"What is agent memory observability?","session_id":"burst:test"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"How does BetterDB detect TTL bugs?","session_id":"burst:test"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"What is rate limiter abuse detection?","session_id":"burst:test"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"How does HGETALL cause latency?","session_id":"burst:test"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"What is persistent slowlog?","session_id":"burst:test"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"How does BetterDB use MCP server?","session_id":"burst:test"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"What Redis keys does a RAG app generate?","session_id":"burst:test"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"What is TTL coverage percentage?","session_id":"burst:test"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"How does BetterDB anomaly detection work?","session_id":"burst:test"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"What is semantic similarity threshold?","session_id":"burst:test"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"How to fix memory fragmentation in Redis?","session_id":"burst:test"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"What is the difference between hot keys and stale keys?","session_id":"burst:test"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"How does BetterDB compare to RedisInsight?","session_id":"burst:test"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"What is LangChain memory runaway?","session_id":"burst:test"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"How does embedding cosine similarity work?","session_id":"burst:test"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"What is the INCR EXPIRE pattern for rate limiting?","session_id":"burst:test"}' &
curl -s -X POST http://localhost:8000/query -H "Content-Type: application/json" -H "X-User-ID: demo" -d '{"query":"How does BetterDB help with AI observability?","session_id":"burst:test"}' &
wait && echo "All 20 done"
```

### What to observe in BetterDB after Trigger Collection

| Pattern | Change | Feature |
|---|---|---|
| `rate:limit:user:demo:minute` | Appears with **w/TTL=1**, Avg Idle ~10s | Feature 4 — burst detected |
| `rate:limit:user:demo:hour` | Counter increments | Feature 4 — cumulative |
| `langchain:memory:session:burst:test` | **Memory grows** (same key, bigger HASH) | Feature 3 — runaway |
| `semantic:cache:*` | Count increases by unique queries | Feature 2 — unbounded cache |

### Key insight — rate limit vs session memory

```
rate:limit:user:demo:minute   w/TTL=1  ← RESETS every 60s (correct design)
langchain:memory:session:*    w/TTL=0  ← NEVER resets (the bug)
```

Rate limit = ephemeral by design. Session memory = permanent by mistake. BetterDB shows both in same dashboard row — `w/TTL` column tells the story.

### Question to ask Claude Code

```
"Show me the burst:test session — how much memory has it accumulated vs the rate limit keys?"
```

---

## Section 7 — Feature 5: RAG Pipeline Latency Attribution

**What it demonstrates:** HGETALL on large `rag:doc:*` keys is the Redis bottleneck in the RAG pipeline — not the LLM.

### Step 1: Measure HGETALL latency directly

```bash
python3 -c "
import time, statistics
from rag.config import get_redis

r = get_redis()
keys = list(r.scan_iter('rag:doc:*'))
print(f'Total rag:doc:* keys: {len(keys)}')

latencies = []
for key in keys:
    t0 = time.perf_counter()
    r.hgetall(key)
    latencies.append((time.perf_counter() - t0) * 1000)

latencies.sort()
print(f'Per-key HGETALL:  avg={statistics.mean(latencies):.1f}ms  p95={latencies[int(len(latencies)*0.95)]:.1f}ms  max={max(latencies):.1f}ms')

t0 = time.perf_counter()
for key in keys:
    r.hgetall(key)
print(f'Full scan ({len(keys)} keys): {(time.perf_counter()-t0)*1000:.0f}ms total')
"
```

Expected (Upstash us-east-1 from your machine):
```
Per-key HGETALL: avg=264ms  p95=272ms  max=297ms
Full scan (21 keys): 5939ms total
```

### Step 2: Compare — query response time breakdown

```bash
curl -X POST http://localhost:8000/query \
     -H "Content-Type: application/json" \
     -H "X-User-ID: student1" \
     -d '{"query": "What is the HGETALL bottleneck?", "session_id": "demo"}' \
     | python3 -m json.tool
```

Note the `latency_ms` field — most of it is Redis HGETALL, not LLM generation.

### Step 3: The RAG pipeline waterfall

```
Embedding call:     ~300ms  (OpenAI API)
Redis scan+HGETALL: ~6000ms ← BOTTLENECK (21 keys × 264ms)
LLM generation:     ~1200ms (OpenAI API)
────────────────────────────
Total:              ~7500ms
```

### Question to ask Claude Code

```
"Show BetterDB per-command latency breakdown for RAG pipeline. Identify HGETALL on rag:doc keys as the latency source."
```

### The fix

```python
# FIX 1: Pipeline — batch all HGETALLs
pipe = r.pipeline(transaction=False)
for key in keys:
    pipe.hgetall(key)
results = pipe.execute()
# Result: 21 × 264ms → ~300ms total (single round trip)

# FIX 2: Store only the embedding hash, retrieve content separately
# FIX 3: Use local Valkey — 0.1ms RTT vs 260ms cloud RTT
```

---

## Section 8 — Quick Reference: All curl Commands

```bash
# Health check
curl http://localhost:8000/health

# Ingest PDF
curl -F "file=@BetterDB_YouTube_Proposal.pdf" -H "X-User-ID: demo" http://localhost:8000/ingest

# Query (first time — cache miss)
curl -X POST http://localhost:8000/query \
     -H "Content-Type: application/json" -H "X-User-ID: demo" \
     -d '{"query": "What is BetterDB?", "session_id": "default"}'

# Query (same — cache hit)
curl -X POST http://localhost:8000/query \
     -H "Content-Type: application/json" -H "X-User-ID: demo" \
     -d '{"query": "What is BetterDB?", "session_id": "default"}'

# Stats
curl http://localhost:8000/stats

# Trigger rate limit (run this 11+ times rapidly)
for i in $(seq 1 15); do
  curl -s -o /dev/null -w "HTTP %{http_code}\n" \
       -X POST http://localhost:8000/query \
       -H "Content-Type: application/json" -H "X-User-ID: abuser" \
       -d '{"query": "test '$i'", "session_id": "test"}'
done
```

---

## Section 9 — All Claude Code MCP Questions

Ask these directly in Claude Code terminal after connecting BetterDB MCP:

### Feature 1 — Incident overview
```
"What are the slowest commands in the last 24h?"
"Show me memory breakdown by namespace"
"Who are the top clients by command count?"
"Show me any anomalies detected"
"Show the complete incident timeline"
```

### Feature 2 — TTL bug
```
"Why does my semantic cache have no TTL?"
"What happens to hit quality as underlying data changes?"
"Show me key analytics — which namespaces have zero TTL coverage?"
"How much memory will semantic_cache:* consume in 30 days at current growth?"
```

### Feature 3 — Agent memory
```
"Show agent memory observability — which session is the runaway?"
"How does BetterDB identify which session key caused the latency spike?"
"What is the memory growth pattern for langchain:memory:session:* keys?"
```

### Feature 4 — Rate limiter
```
"Show per-client INCR/EXPIRE command breakdown for rate_limit:* keys"
"Any burst patterns from a specific IP or user ID?"
"What is the 30-second burst window for the abuser user?"
```

### Feature 5 — RAG latency
```
"Show BetterDB per-command latency breakdown for RAG pipeline"
"Identify HGETALL commands on rag:doc:* as the latency source"
"Show before/after TTL fix reducing HGETALL from the slowlog"
"What is the p95 latency for HGETALL on rag:doc keys?"
```

---

## Section 10 — Key Patterns Reference

| Redis Key | Written by | TTL | BetterDB Feature |
|---|---|---|---|
| `rag:doc:{sha256}` | `POST /ingest` | **None (bug)** | Feature 2, 5 |
| `semantic_cache:{md5}` | `POST /query` (miss) | **None (bug)** | Feature 2 |
| `rate_limit:user_{id}:minute` | `POST /query` | 60s ✓ | Feature 4 |
| `rate_limit:user_{id}:hour` | `POST /query` | 3600s ✓ | Feature 4 |
| `langchain:memory:session:{id}` | `POST /query` | **None (bug)** | Feature 3 |

---

## Section 11 — Troubleshooting

| Problem | Cause | Fix |
|---|---|---|
| `WRONGPASS` in docker logs | Wrong Upstash password | Use write token from TCP tab (uncheck "Read-Only Token") |
| BetterDB connection red | Read-only token used | Uncheck "Read-Only Token" in Upstash TCP tab |
| All queries return 200 (no 429) | Sequential queries span >60s | Use parallel queries (asyncio script in Section 6) |
| `slowlog` returns empty | Upstash restricts SLOWLOG | Switch to local Valkey OR measure latency directly |
| `cache_similarity_distribution` fails | BetterDB JS SDK not used | Python only — use standard monitoring tools |
| Slow queries (~6s) | HGETALL RTT to Upstash us-east-1 | Switch to local Valkey (see Section 12) |
| Agent `WRONGPASS` for local Valkey | Password sent to passwordless Valkey | Remove `VALKEY_PASSWORD` env var from agent command |
| Agent can't reach `localhost` | Docker network isolation | Use `host.docker.internal` not `localhost` for `VALKEY_HOST` |
| `connection refused` on `host.docker.internal` | Linux Docker networking | Replace with `172.17.0.1` on Linux |

---

## Section 12 — Switching from Upstash to Local Valkey

Only 3 changes. Zero code changes in `rag/`.

### Change 1 — .env

```dotenv
# Comment out Upstash
# REDIS_URL=rediss://default:TOKEN@quiet-flea-79665.upstash.io:6379

# Use local Valkey
REDIS_URL=redis://localhost:6379
```

### Change 2 — Start local Valkey

```bash
docker-compose up -d
docker-compose ps   # wait for status: healthy
```

### Change 3 — Replace BetterDB agent

```bash
# Stop Upstash agent
docker rm -f betterdb-agent

# Start local Valkey agent
# (create new token in BetterDB → Manage Connections → + Add Connection → Via Agent)
docker run -d \
  --name betterdb-agent-local \
  -e VALKEY_HOST=host.docker.internal \
  -e VALKEY_PORT=6379 \
  -e BETTERDB_CLOUD_URL=wss://betterdb-test1.app.betterdb.com/agent/ws \
  -e BETTERDB_TOKEN=<new_agent_token> \
  betterdb/agent:latest

docker logs betterdb-agent-local
# Expected: [Agent] WebSocket connected, sending hello
```

### Verify connection

```bash
# Restart FastAPI (picks up new REDIS_URL from .env)
# Ctrl+C → uvicorn rag.main:app --reload --port 8000

curl http://localhost:8000/health
# redis: ok — now pointing to local Valkey
```

### What improves with local Valkey

```bash
python3 -c "
import time, statistics
from rag.config import get_redis
r = get_redis()
keys = list(r.scan_iter('rag:doc:*'))
latencies = []
for key in keys:
    t0 = time.perf_counter()
    r.hgetall(key)
    latencies.append((time.perf_counter() - t0) * 1000)
print(f'avg={statistics.mean(latencies):.2f}ms  max={max(latencies):.2f}ms')
"
# Upstash:      avg=264ms   max=297ms
# Local Valkey: avg=0.3ms   max=1.2ms
```

BetterDB slowlog now captures real HGETALL entries (Valkey configured with `slowlog-log-slower-than 100` in docker-compose.yml).
