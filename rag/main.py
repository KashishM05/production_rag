from __future__ import annotations

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .config import get_redis, get_settings
from .pipeline import ingest_pdf, query_rag

app = FastAPI(title="BetterDB RAG Demo", version="1.0.0")


# ── Request / Response models ─────────────────────────────────────────────────


class QueryRequest(BaseModel):
    query: str
    session_id: str = "default"


# ── Endpoints ─────────────────────────────────────────────────────────────────


@app.get("/health")
def health():
    """Redis ping + key count per namespace."""
    r = get_redis()
    try:
        r.ping()
        redis_ok = True
    except Exception as e:
        redis_ok = False

    namespaces = ["rag:doc:*", "semantic_cache:*", "rate_limit:*", "langchain:memory:session:*"]
    counts = {}
    if redis_ok:
        for pattern in namespaces:
            counts[pattern.rstrip("*")] = sum(1 for _ in r.scan_iter(pattern))

    return {"redis": "ok" if redis_ok else "error", "key_counts": counts}


@app.post("/ingest")
async def ingest(
    file: UploadFile = File(...),
    x_user_id: str = Header(default="demo"),
):
    """Upload a PDF → chunk → embed → store as rag:doc:* keys in Redis.
    Keys written with NO TTL (intentional — demonstrates Feature 2 TTL bug).
    """
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported")

    content = await file.read()
    if len(content) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="PDF too large (max 20 MB)")

    result = await ingest_pdf(content, file.filename)
    return JSONResponse(content=result, status_code=201)


@app.post("/query")
async def query(
    body: QueryRequest,
    x_user_id: str = Header(default="demo"),
):
    """Query the RAG pipeline.

    Pipeline:
      1. Rate limit check  → rate_limit:user_{id}:minute / :hour  (Feature 4)
      2. Embed query
      3. Semantic cache lookup  → semantic_cache:*  (Feature 2)
      4. Retrieve top-3 docs   → HGETALL rag:doc:*  (Feature 5)
      5. LLM generation
      6. Cache store            → semantic_cache:*  NO TTL  (Feature 2)
      7. Session write          → langchain:memory:session:*  NO TTL  (Feature 3)
    """
    if not body.query.strip():
        raise HTTPException(status_code=400, detail="Query cannot be empty")

    result = await query_rag(body.query, x_user_id, body.session_id)
    return result


@app.get("/stats")
def stats():
    """Scan Redis and return key counts + sample keys for all 4 namespaces."""
    r = get_redis()
    s = get_settings()

    namespaces = {
        "rag:doc": "rag:doc:*",
        "semantic_cache": "semantic_cache:*",
        "rate_limit": "rate_limit:*",
        "langchain:memory:session": "langchain:memory:session:*",
    }

    result = {}
    for ns, pattern in namespaces.items():
        keys = list(r.scan_iter(pattern))
        sample = keys[:3]
        ttls = {k: r.ttl(k) for k in sample}
        result[ns] = {
            "count": len(keys),
            "sample_keys": sample,
            "sample_ttls": ttls,
        }

    result["_config"] = {
        "embedding_model": s.embedding_model,
        "llm_model": s.openai_model,
        "cache_threshold": s.cache_threshold,
        "rate_limit_minute": s.rate_limit_minute,
    }

    return result
