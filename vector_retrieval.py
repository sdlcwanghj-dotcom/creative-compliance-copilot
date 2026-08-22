"""Local vector retrieval over the archived legal corpus.

This module adds *semantic* recall on top of the existing BM25 lexical search
(``policy_retrieval.search_policy_query``) without touching the deterministic
evidence bindings in ``policy_retrieval.citations_for_findings`` — those remain
the sole authority for red-line rule citations (see ``review_graph`` invariants).

Design invariants:
- **Offline-reproducible first.** The embedder is optional and lazy. With no
  model configured (``EMBEDDING_MODEL_PATH`` unset or the ONNX runtime
  unavailable) every entry point degrades to pure BM25 — nothing raises.
- **No fuzzy authority.** Hybrid results carry an explicit ``citation_type``
  (``bm25`` / ``vector`` / ``hybrid``) and a fused ``score``; callers must treat
  vector hits as *candidate* recall for humans/LLM, never as bound evidence.
- The prebuilt index (``data/vector_index.json``) records the model id and
  dimension; a mismatch or a missing index silently falls back to BM25.
"""

import json
import math
import os
from pathlib import Path

from policy_retrieval import LEGAL_EXCERPTS, _tokens, search_policy_query


ROOT = Path(__file__).resolve().parent
INDEX_PATH = ROOT / "data" / "vector_index.json"

RAG_TOP_K = int(os.getenv("RAG_TOP_K", "4"))
RRF_K = 60  # Reciprocal Rank Fusion damping; standard default.


class _NullEmbedder:
    """Stand-in used whenever no local model is configured."""

    available = False
    dimension = 0

    def embed(self, texts):
        raise RuntimeError("embedding model is not configured")


def _load_embedder():
    """Return a usable embedder or ``_NullEmbedder`` — never raises.

    The concrete ONNX-backed embedder is intentionally loaded behind this
    guard so the app boots (and tests run) with no model present. Wire the real
    implementation here once the model choice (PRD Q1/Q2) is settled.
    """
    model_path = os.getenv("EMBEDDING_MODEL_PATH", "").strip()
    if not model_path:
        return _NullEmbedder()
    try:
        from onnx_embedder import OnnxEmbedder  # optional, added with the model
    except Exception:
        return _NullEmbedder()
    try:
        return OnnxEmbedder(model_path)
    except Exception:
        return _NullEmbedder()


_EMBEDDER = None


def get_embedder():
    global _EMBEDDER
    if _EMBEDDER is None:
        _EMBEDDER = _load_embedder()
    return _EMBEDDER


_INDEX = None


def load_index():
    """Load the prebuilt vector index, or ``None`` if unusable.

    Returns a dict ``{"model", "dimension", "records": [...]}`` where each record
    has ``id`` and ``vector``. A missing file, malformed JSON, or a
    model/dimension mismatch against the current embedder all degrade to
    ``None`` so callers fall back to BM25.
    """
    global _INDEX
    if _INDEX is not None:
        return _INDEX or None
    if not INDEX_PATH.exists():
        _INDEX = {}
        return None
    try:
        data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        records = data["records"]
        dimension = int(data["dimension"])
    except Exception:
        _INDEX = {}
        return None

    embedder = get_embedder()
    if embedder.available and embedder.dimension != dimension:
        _INDEX = {}
        return None

    _INDEX = {
        "model": data.get("model", ""),
        "dimension": dimension,
        "records": records,
    }
    return _INDEX


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def vector_search(query, limit=RAG_TOP_K):
    """Semantic top-k over the prebuilt index. Empty list when unavailable."""
    embedder = get_embedder()
    index = load_index()
    if not embedder.available or index is None:
        return []
    try:
        query_vector = embedder.embed([query])[0]
    except Exception:
        return []
    scored = []
    excerpt_by_id = {item["id"]: item for item in LEGAL_EXCERPTS}
    for record in index["records"]:
        similarity = _cosine(query_vector, record["vector"])
        if similarity <= 0:
            continue
        excerpt = excerpt_by_id.get(record["id"])
        if excerpt is None:
            continue
        scored.append({
            **excerpt,
            "citation_type": "vector",
            "score": round(similarity, 4),
        })
    scored.sort(key=lambda item: item["score"], reverse=True)
    return scored[:limit]


def _rrf(*ranked_lists, k=RRF_K):
    """Reciprocal Rank Fusion across result lists keyed by ``id``.

    Cross-corpus scores (BM25 vs cosine) are not directly comparable, so we fuse
    by rank rather than by raw score. Returns items merged by id with a
    ``score`` set to the fused value and ``citation_type`` reflecting overlap.
    """
    fused = {}
    for ranked in ranked_lists:
        for rank, item in enumerate(ranked):
            item_id = item["id"]
            contribution = 1.0 / (k + rank + 1)
            if item_id not in fused:
                fused[item_id] = {"item": dict(item), "score": 0.0, "types": set()}
            fused[item_id]["score"] += contribution
            fused[item_id]["types"].add(item.get("citation_type", "bm25"))
    merged = []
    for entry in fused.values():
        item = entry["item"]
        types = entry["types"]
        item["citation_type"] = "hybrid" if len(types) > 1 else next(iter(types))
        item["score"] = round(entry["score"], 5)
        merged.append(item)
    merged.sort(key=lambda item: item["score"], reverse=True)
    return merged


def hybrid_search(query, limit=8):
    """Fuse BM25 + vector recall over the excerpt corpus.

    Returns items shaped like ``search_policy_query`` output (excerpt fields plus
    ``citation_type`` and ``score``). Falls back to pure BM25 when no vector
    index/model is present, so behaviour is unchanged in the offline path.
    """
    if not _tokens(query):
        return []
    lexical = search_policy_query(query, limit=limit)
    semantic = vector_search(query, limit=limit)
    if not semantic:
        return lexical[:limit]
    if not lexical:
        return semantic[:limit]
    return _rrf(lexical, semantic)[:limit]

