"""Build the semantic vector index over the legal excerpt corpus.

Reads ``data/legal_excerpts.json`` and writes ``data/vector_index.json`` in the
shape ``vector_retrieval.load_index`` expects::

    {"model": <id>, "dimension": <int>, "records": [{"id": ..., "vector": [...]}]}

Requires a configured local embedder (``EMBEDDING_MODEL_PATH`` + ONNX runtime).
Without one it exits non-zero and writes nothing — the app then stays on the
BM25 fallback path, so running this is optional until a model is provisioned.

Usage:
    EMBEDDING_MODEL_PATH=/path/to/model python scripts/build_vector_index.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from policy_retrieval import LEGAL_EXCERPTS  # noqa: E402
from vector_retrieval import INDEX_PATH, get_embedder  # noqa: E402


def _embed_text(excerpt):
    """Concatenate the fields a reviewer would search on into one passage."""
    keywords = " ".join(excerpt.get("keywords", []))
    parts = [
        excerpt.get("title", ""),
        excerpt.get("article", ""),
        keywords,
        excerpt.get("excerpt", ""),
        excerpt.get("interpretation", ""),
    ]
    return "\n".join(part for part in parts if part)


def main():
    embedder = get_embedder()
    if not embedder.available:
        print(
            "No embedding model configured (set EMBEDDING_MODEL_PATH). "
            "Nothing written; app stays on BM25 fallback.",
            file=sys.stderr,
        )
        return 1

    texts = [_embed_text(item) for item in LEGAL_EXCERPTS]
    vectors = embedder.embed(texts)
    records = [
        {"id": item["id"], "vector": [round(float(value), 6) for value in vector]}
        for item, vector in zip(LEGAL_EXCERPTS, vectors)
    ]

    payload = {
        "model": getattr(embedder, "model_id", ""),
        "dimension": embedder.dimension,
        "records": records,
    }
    INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Wrote {len(records)} vectors (dim={embedder.dimension}) to {INDEX_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
