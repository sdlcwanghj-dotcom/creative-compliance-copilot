import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=False)


ALLOWED_TOOLS = {
    "classify_ad_industry",
    "check_forbidden_words",
    "retrieve_applicable_policy",
    "search_similar_cases",
    "analyze_semantic_risk",
    "generate_compliant_rewrite",
}


class LLMConfigurationError(RuntimeError):
    pass


class ReviewLLMAgent:
    """Optional model-backed planner for an OpenAI-compatible chat endpoint."""

    def __init__(self):
        self.api_key = os.getenv("LLM_API_KEY", "").strip()
        self.model = os.getenv("LLM_MODEL", "").strip()
        self.base_url = (
            os.getenv("LLM_API_BASE")
            or os.getenv("LLM_BASE_URL")
            or "https://api.openai.com/v1"
        ).rstrip("/")
        self.timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", "30"))

    @property
    def available(self):
        return bool(self.api_key and self.model)

    def _json_completion(self, system_prompt, payload):
        if not self.available:
            raise LLMConfigurationError("LLM_API_KEY or LLM_MODEL is not configured")
        response = requests.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={
                "model": self.model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return json.loads(content)

    def plan_tools(self, context):
        result = self._json_completion(
            """You route an ad pre-review workflow. Return JSON with keys tools and summary.
tools must be a subset of the supplied allowed_tools. Always include deterministic checks and
policy retrieval. Select image/OCR tools only when appropriate. Do not provide chain-of-thought.""",
            {**context, "allowed_tools": sorted(ALLOWED_TOOLS)},
        )
        tools = [tool for tool in result.get("tools", []) if tool in ALLOWED_TOOLS]
        return {"tools": tools, "summary": str(result.get("summary", "模型已选择审核工具"))[:200]}

    def analyze_semantic_risk(self, context, allowed_rule_ids):
        result = self._json_completion(
            """You assist human ad reviewers. Identify implicit semantic risks missed by exact
rules. Return JSON {violations:[...]}. Each violation must contain category, evidence, reason,
rule_id, severity(high|medium|low), confidence(0..1). Evidence must be an exact substring of
the supplied copy. Use only allowed_rule_ids. Do not invent policy or product facts. Return an
empty list when uncertain.""",
            {**context, "allowed_rule_ids": sorted(allowed_rule_ids)},
        )
        validated = []
        copy = context.get("copy", "")
        for item in result.get("violations", []):
            evidence = str(item.get("evidence", ""))
            rule_id = item.get("rule_id")
            severity = item.get("severity")
            if not evidence or evidence not in copy or rule_id not in allowed_rule_ids:
                continue
            if severity not in {"high", "medium", "low"}:
                continue
            validated.append({
                "category": str(item.get("category", "语义风险"))[:80],
                "evidence": evidence,
                "reason": str(item.get("reason", "需人工核验"))[:300],
                "rule_id": rule_id,
                "severity": severity,
                "confidence": max(0.0, min(1.0, float(item.get("confidence", 0.5)))),
            })
        return validated
