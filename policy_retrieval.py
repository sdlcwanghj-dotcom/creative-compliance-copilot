import json
import math
import re
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def _load_json(name):
    return json.loads((ROOT / "data" / name).read_text(encoding="utf-8"))


LEGAL_EXCERPTS = _load_json("legal_excerpts.json")
RULE_EVIDENCE = _load_json("rule_evidence.json")
EXCERPT_BY_ID = {item["id"]: item for item in LEGAL_EXCERPTS}


def validate_evidence_bindings(known_rule_ids):
    errors = []
    for rule_id, chunk_ids in RULE_EVIDENCE.items():
        if rule_id not in known_rule_ids:
            errors.append(f"Unknown rule in evidence map: {rule_id}")
        for chunk_id in chunk_ids:
            if chunk_id not in EXCERPT_BY_ID:
                errors.append(f"Unknown evidence chunk for {rule_id}: {chunk_id}")
    return errors


def citations_for_findings(findings, limit=6):
    """Return only explicitly bound evidence. No synthetic relevance score."""
    results = []
    seen = set()
    for finding in findings:
        rule_id = finding["policy_id"]
        for chunk_id in RULE_EVIDENCE.get(rule_id, []):
            if chunk_id in seen:
                existing = next(item for item in results if item["id"] == chunk_id)
                if rule_id not in existing["supports_rule_ids"]:
                    existing["supports_rule_ids"].append(rule_id)
                continue
            excerpt = EXCERPT_BY_ID.get(chunk_id)
            if excerpt is None:
                continue
            seen.add(chunk_id)
            results.append({
                **excerpt,
                "citation_type": "deterministic",
                "supports_rule_ids": [rule_id],
                "score": None,
            })
            if len(results) >= limit:
                return results
    return results


def unsupported_rule_ids(findings):
    return sorted({
        finding["policy_id"]
        for finding in findings
        if not RULE_EVIDENCE.get(finding["policy_id"])
    })


def _tokens(text):
    normalized = re.sub(r"\s+", "", text.lower())
    ascii_words = re.findall(r"[a-z0-9]+", normalized)
    chinese = "".join(re.findall(r"[\u4e00-\u9fff]", normalized))
    grams = [chinese[index:index + 2] for index in range(max(0, len(chinese) - 1))]
    return ascii_words + grams


def search_policy_query(query, limit=8, min_score=1.2):
    """BM25 retrieval for an auditor's free-text policy search."""
    query_tokens = _tokens(query)
    if not query_tokens:
        return []
    documents = [
        _tokens(" ".join([item["title"], item["article"], item["excerpt"], *item["keywords"]]))
        for item in LEGAL_EXCERPTS
    ]
    document_count = len(documents)
    avg_length = sum(map(len, documents)) / max(1, document_count)
    document_frequency = Counter()
    for document in documents:
        document_frequency.update(set(document))

    k1, b = 1.5, 0.75
    ranked = []
    for item, document in zip(LEGAL_EXCERPTS, documents):
        frequencies = Counter(document)
        score = 0.0
        for token in query_tokens:
            frequency = frequencies[token]
            if not frequency:
                continue
            df = document_frequency[token]
            idf = math.log(1 + (document_count - df + 0.5) / (df + 0.5))
            denominator = frequency + k1 * (1 - b + b * len(document) / max(1, avg_length))
            score += idf * frequency * (k1 + 1) / denominator
        if score >= min_score:
            ranked.append({**item, "citation_type": "bm25", "score": round(score, 3)})
    return sorted(ranked, key=lambda item: item["score"], reverse=True)[:limit]
