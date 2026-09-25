from __future__ import annotations

import hashlib
import io
import json
import time
from datetime import datetime, timezone

import numpy as np
import pypdf
from fastapi import HTTPException

from .config import get_openai, get_redis, get_settings

# ── Key pattern constants ─────────────────────────────────────────────────────
# Students see exactly which Redis command writes which key.


def rag_doc_key(chunk: str) -> str:
    return f"rag:doc:{hashlib.sha256(chunk.encode()).hexdigest()}"


def cache_key(query: str) -> str:
    return f"semantic_cache:{hashlib.md5(query.encode()).hexdigest()}"


def rate_key(user_id: str, window: str) -> str:
    return f"rate_limit:user_{user_id}:{window}"


def session_key(session_id: str) -> str:
    return f"langchain:memory:session:{session_id}"


# ── Helpers ───────────────────────────────────────────────────────────────────


def cosine_similarity(a: list[float], b: list[float]) -> float:
    va, vb = np.array(a, dtype=np.float32), np.array(b, dtype=np.float32)
    denom = np.linalg.norm(va) * np.linalg.norm(vb)
    if denom == 0:
        return 0.0
    return float(np.dot(va, vb) / denom)


def _chunk_text(text: str, size: int, overlap: int) -> list[str]:
    chunks, start = [], 0
    while start < len(text):
        end = start + size
        chunks.append(text[start:end].strip())
        start += size - overlap
    return [c for c in chunks if len(c) > 20]


# ── Rate limiting ─────────────────────────────────────────────────────────────


def check_rate_limit(user_id: str) -> None:
    """INCR + EXPIRE pattern. rate_limit keys DO get TTL (contrast with rag:doc:* which don't)."""
    s = get_settings()
    r = get_redis()

    minute_k = rate_key(user_id, "minute")
    cnt = r.incr(minute_k)
    if cnt == 1:
        r.expire(minute_k, 60)

    hour_k = rate_key(user_id, "hour")
    cnt_h = r.incr(hour_k)
    if cnt_h == 1:
        r.expire(hour_k, 3600)

    if cnt > s.rate_limit_minute:
        raise HTTPException(status_code=429, detail=f"Rate limit exceeded: {cnt}/{s.rate_limit_minute} per minute")


# ── Ingestion ─────────────────────────────────────────────────────────────────


async def ingest_pdf(file_bytes: bytes, filename: str) -> dict:
    """PDF → chunks → embeddings → rag:doc:* keys in Redis. NO TTL (Feature 2 bug)."""
    s = get_settings()
    r = get_redis()
    client = get_openai()

    # Extract text
    reader = pypdf.PdfReader(io.BytesIO(file_bytes))
    full_text = "\n".join(page.extract_text() or "" for page in reader.pages)
    if not full_text.strip():
        raise HTTPException(status_code=400, detail="Could not extract text from PDF")

    # Chunk
    chunks = _chunk_text(full_text, s.chunk_size, s.chunk_overlap)

    # Batch embed (OpenAI allows up to 2048 inputs per call)
    batch_size = 100
    all_embeddings: list[list[float]] = []
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i : i + batch_size]
        resp = await client.embeddings.create(model=s.embedding_model, input=batch)
        all_embeddings.extend([item.embedding for item in resp.data])

    # Store each chunk as rag:doc:{sha256} HSET — NO expire (TTL=-1, the bug)
    pipe = r.pipeline(transaction=False)
    keys_written = []
    for idx, (chunk, vec) in enumerate(zip(chunks, all_embeddings)):
        key = rag_doc_key(chunk)
        pipe.hset(
            key,
            mapping={
                "content": chunk,
                "embedding": json.dumps(vec),
                "source": filename,
                "chunk_idx": str(idx),
                "chunk_size": str(len(chunk)),
            },
        )
        keys_written.append(key)
    pipe.execute()

    return {
        "chunks_stored": len(chunks),
        "source": filename,
        "keys_preview": keys_written[:3],
    }


# ── Semantic cache ────────────────────────────────────────────────────────────


async def semantic_cache_lookup(query_embedding: list[float]) -> str | None:
    """SCAN semantic_cache:* → cosine similarity → return cached response if hit."""
    s = get_settings()
    r = get_redis()
    best_sim, best_response = 0.0, None

    for key in r.scan_iter("semantic_cache:*"):
        if r.type(key) != "hash":
            continue
        entry = r.hgetall(key)
        if not entry.get("embedding"):
            continue
        sim = cosine_similarity(query_embedding, json.loads(entry["embedding"]))
        if sim > best_sim:
            best_sim = sim
            best_response = entry.get("response")

    if best_sim >= s.cache_threshold and best_response:
        return best_response
    return None


def store_cache_entry(query: str, query_embedding: list[float], response: str, model: str) -> None:
    """Write semantic_cache:{md5} HSET — NO expire (Feature 2 TTL bug)."""
    r = get_redis()
    r.hset(
        cache_key(query),
        mapping={
            "query": query,
            "embedding": json.dumps(query_embedding),
            "response": response,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "model": model,
        },
    )


# ── Retrieval ─────────────────────────────────────────────────────────────────


def retrieve_docs(query_embedding: list[float], top_k: int = 3) -> list[str]:
    """SCAN rag:doc:* → HGETALL each → cosine similarity → top-k chunks.
    HGETALL on large keys is Feature 5: the Redis latency bottleneck.
    """
    r = get_redis()
    scores: list[tuple[float, str]] = []

    for key in r.scan_iter("rag:doc:*"):
        entry = r.hgetall(key)  # ← HGETALL on 17-19KB key (Feature 5)
        if not entry.get("embedding"):
            continue
        sim = cosine_similarity(query_embedding, json.loads(entry["embedding"]))
        scores.append((sim, entry.get("content", "")))

    scores.sort(key=lambda x: x[0], reverse=True)
    return [content for _, content in scores[:top_k]]


# ── Session memory ────────────────────────────────────────────────────────────


def store_session_message(session_id: str, role: str, content: str) -> None:
    """HSET langchain:memory:session:{id} — NO expire (Feature 3 runaway memory)."""
    r = get_redis()
    key = session_key(session_id)
    idx = r.hlen(key)
    r.hset(key, f"msg_{idx}", json.dumps({"role": role, "content": content}))


# ── Full RAG pipeline ─────────────────────────────────────────────────────────


async def query_rag(query: str, user_id: str, session_id: str) -> dict:
    """Full pipeline: rate limit → embed → cache check → retrieve → LLM → cache store → session."""
    s = get_settings()
    client = get_openai()
    t0 = time.perf_counter()

    # 1. Rate limit (Feature 4)
    check_rate_limit(user_id)

    # 2. Embed query
    embed_resp = await client.embeddings.create(model=s.embedding_model, input=[query])
    query_embedding = embed_resp.data[0].embedding

    # 3. Semantic cache lookup (Feature 2)
    cached = await semantic_cache_lookup(query_embedding)
    if cached:
        store_session_message(session_id, "user", query)
        store_session_message(session_id, "assistant", cached)
        return {
            "response": cached,
            "cache_hit": True,
            "session_id": session_id,
            "docs_used": 0,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
        }

    # 4. Retrieve docs (Feature 5 — HGETALL latency)
    docs = retrieve_docs(query_embedding, top_k=3)
    if not docs:
        raise HTTPException(status_code=404, detail="No documents ingested yet. Call /ingest first.")

    # 5. LLM generation
    context = "\n\n---\n\n".join(docs)
    messages = [
        {"role": "system", "content": "Answer the question using only the context provided. Be concise."},
        {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {query}"},
    ]
    completion = await client.chat.completions.create(model=s.openai_model, messages=messages, max_tokens=512)
    response = completion.choices[0].message.content or ""

    # 6. Store in semantic cache — NO expire (Feature 2 TTL bug)
    store_cache_entry(query, query_embedding, response, s.openai_model)

    # 7. Store session messages — NO expire (Feature 3 runaway)
    store_session_message(session_id, "user", query)
    store_session_message(session_id, "assistant", response)

    return {
        "response": response,
        "cache_hit": False,
        "session_id": session_id,
        "docs_used": len(docs),
        "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
    }
