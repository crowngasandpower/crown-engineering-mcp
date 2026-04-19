"""Team memory API — shared vector store for Claude Code.

Engineers (via the MCP server) call POST /remember to store a piece of team
knowledge and POST /recall to retrieve semantically similar items. Embeddings
are produced by Voyage AI and persisted in Qdrant.

Defaults use voyage-3-large (1024 dims) for both ingest and query — these two
operations must share an embedding space, and voyage-3-large is the only
voyage-3 variant we can safely use on both sides. `INGEST_MODEL` and
`QUERY_MODEL` are env-configurable for a future move to the voyage-4 family,
which is designed for query/ingest model splits at different price points.
"""

import os
import uuid
from datetime import datetime, timezone

import voyageai
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from qdrant_client import QdrantClient
from qdrant_client.http.models import Distance, PointStruct, VectorParams

QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY")
VOYAGE_API_KEY = os.environ.get("VOYAGE_API_KEY")
COLLECTION = os.environ.get("COLLECTION_NAME", "team-knowledge")
# Same model on both sides until voyage-4 family availability is confirmed —
# voyage-3 and voyage-3-lite are NOT in the same embedding space, so mixing them
# corrupts retrieval. voyage-3-large is 1024-dim and good enough for both.
INGEST_MODEL = os.environ.get("INGEST_MODEL", "voyage-3-large")
QUERY_MODEL = os.environ.get("QUERY_MODEL", "voyage-3-large")
EMBED_DIM = int(os.environ.get("EMBED_DIM", "1024"))

if not VOYAGE_API_KEY:
    raise RuntimeError("VOYAGE_API_KEY is required")

voyage = voyageai.Client(api_key=VOYAGE_API_KEY)
qdrant = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)

app = FastAPI(title="Crown Memory API")


def _ensure_collection() -> None:
    existing = [c.name for c in qdrant.get_collections().collections]
    if COLLECTION not in existing:
        qdrant.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
        )


@app.on_event("startup")
def on_startup() -> None:
    _ensure_collection()


class RememberRequest(BaseModel):
    content: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    source: str | None = None
    author: str | None = None


class RememberResponse(BaseModel):
    id: str
    stored_at: str


class RecallRequest(BaseModel):
    query: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=50)
    tags: list[str] | None = None


class RecallHit(BaseModel):
    id: str
    score: float
    content: str
    tags: list[str]
    source: str | None = None
    author: str | None = None
    stored_at: str


class RecallResponse(BaseModel):
    hits: list[RecallHit]


@app.get("/health")
def health() -> dict:
    try:
        info = qdrant.get_collection(COLLECTION)
        return {"ok": True, "collection": COLLECTION, "points": info.points_count}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"qdrant unreachable: {exc}")


@app.post("/remember", response_model=RememberResponse)
def remember(req: RememberRequest) -> RememberResponse:
    vec = voyage.embed(
        [req.content], model=INGEST_MODEL, input_type="document"
    ).embeddings[0]
    point_id = str(uuid.uuid4())
    stored_at = datetime.now(timezone.utc).isoformat()
    qdrant.upsert(
        collection_name=COLLECTION,
        points=[
            PointStruct(
                id=point_id,
                vector=vec,
                payload={
                    "content": req.content,
                    "tags": req.tags,
                    "source": req.source,
                    "author": req.author,
                    "stored_at": stored_at,
                },
            )
        ],
    )
    return RememberResponse(id=point_id, stored_at=stored_at)


@app.post("/recall", response_model=RecallResponse)
def recall(req: RecallRequest) -> RecallResponse:
    vec = voyage.embed(
        [req.query], model=QUERY_MODEL, input_type="query"
    ).embeddings[0]

    query_filter = None
    if req.tags:
        from qdrant_client.http.models import FieldCondition, Filter, MatchAny
        query_filter = Filter(
            must=[FieldCondition(key="tags", match=MatchAny(any=req.tags))]
        )

    results = qdrant.search(
        collection_name=COLLECTION,
        query_vector=vec,
        limit=req.top_k,
        query_filter=query_filter,
        with_payload=True,
    )
    hits = [
        RecallHit(
            id=str(r.id),
            score=r.score,
            content=r.payload.get("content", ""),
            tags=r.payload.get("tags", []) or [],
            source=r.payload.get("source"),
            author=r.payload.get("author"),
            stored_at=r.payload.get("stored_at", ""),
        )
        for r in results
    ]
    return RecallResponse(hits=hits)


@app.get("/stats")
def stats() -> dict:
    info = qdrant.get_collection(COLLECTION)
    return {
        "collection": COLLECTION,
        "points": info.points_count,
        "vectors_count": info.vectors_count,
        "ingest_model": INGEST_MODEL,
        "query_model": QUERY_MODEL,
    }
