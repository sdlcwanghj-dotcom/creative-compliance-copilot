"""LangGraph orchestration layer for the ad pre-review workflow.

This module owns *orchestration only*. Every deterministic control
(forbidden-word scan, policy citation binding, price/spec checks) and the
optional LLM calls live in the host app and are passed in as ``deps`` at build
time. That keeps this module free of Streamlit and of the rule catalog, so the
graph can be unit-tested in isolation.

Design invariants (mirror the human-facing compliance guarantees):
- Deterministic nodes are plain functions; the graph never replaces a red-line
  rule with a model call.
- The LLM tool planner is a *real* conditional edge: when the plan does not
  request ``analyze_semantic_risk`` the semantic node is skipped.
- With no API key (or LLM unrequested) the conditional edges short-circuit to
  the deterministic path, preserving the offline reproducible flow.
- Human adjudication is intentionally NOT in this graph; the checkpointer only
  replays analysis runs, never the audited final decision.
"""

import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional
from typing_extensions import TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver


SEVERITY_TO_CN = {"high": "高", "medium": "中", "low": "低"}


class ReviewState(TypedDict, total=False):
    # Inputs (all JSON-serializable — no Streamlit UploadedFile here).
    copy: str
    landing: str
    industry_input: str
    product: str
    audience: str
    has_image: bool
    image_size: int
    qualification: bool
    brand_terms: str
    use_llm: bool
    llm_available: bool
    # Intermediate model state.
    llm_plan: Optional[dict]
    llm_error: Optional[str]
    llm_findings: list
    llm_context_snippets: list
    # Outputs consumed by the UI.
    report: Optional[dict]
    rag_results: list
    similar_cases: list
    rewrites: dict
    trace: list


@dataclass
class ReviewDeps:
    """Deterministic controls + LLM agent class injected by the host app."""

    scan_material: Callable[..., dict]
    retrieve_applicable_policy: Callable[..., list]
    retrieve_context_snippets: Callable[..., list]
    search_similar_cases: Callable[..., list]
    generate_compliant_rewrite: Callable[..., dict]
    add_finding: Callable[..., None]
    get_policy_ids: Callable[[], set]
    llm_agent_class: Any


def _llm_context(state: ReviewState) -> dict:
    return {
        "copy": state["copy"],
        "landing_page": state["landing"],
        "selected_industry": state["industry_input"],
        "product": state["product"],
        "audience": state["audience"],
        "has_image": state.get("has_image", False),
        "qualification_verified": state.get("qualification", False),
    }


def _build_nodes(deps: ReviewDeps):
    def plan(state: ReviewState) -> dict:
        agent = deps.llm_agent_class()
        try:
            plan_result = agent.plan_tools(_llm_context(state))
            return {"llm_plan": plan_result, "llm_error": None}
        except Exception as error:
            return {
                "llm_plan": None,
                "llm_error": f"模型规划不可用，已降级为确定性流程：{type(error).__name__}",
            }

    def deterministic_scan(state: ReviewState) -> dict:
        report = deps.scan_material(
            state["copy"], state["landing"], state["industry_input"],
            state["product"], state["audience"], state.get("image_size", 0),
            state.get("qualification", False), state.get("brand_terms", ""),
        )
        return {"report": report}

    def retrieve_context(state: ReviewState) -> dict:
        """RAG recall of statute text to ground the semantic node.

        Best-effort: any failure yields empty snippets so ``semantic_llm`` still
        runs (equivalent to the pre-RAG behaviour)."""
        try:
            snippets = deps.retrieve_context_snippets(state["copy"])
        except Exception:
            snippets = []
        return {"llm_context_snippets": snippets}

    def semantic_llm(state: ReviewState) -> dict:
        agent = deps.llm_agent_class()
        try:
            findings = agent.analyze_semantic_risk(
                _llm_context(state), deps.get_policy_ids(),
                context_snippets=state.get("llm_context_snippets", []),
            )
            return {"llm_findings": findings, "llm_error": None}
        except Exception as error:
            return {
                "llm_findings": [],
                "llm_error": f"模型语义分析不可用，已保留确定性结果：{type(error).__name__}",
            }

    def merge_llm_findings(state: ReviewState) -> dict:
        report = state["report"]
        llm_findings = state.get("llm_findings", [])
        for finding in llm_findings:
            deps.add_finding(
                report["findings"], finding["category"], finding["evidence"],
                finding["reason"], SEVERITY_TO_CN[finding["severity"]],
                finding["rule_id"], "LLM 语义分析",
            )
        if llm_findings:
            report["risk_score"] = min(99, report["risk_score"] + 11 * len(llm_findings))
            if any(item["severity"] == "high" for item in llm_findings):
                report["decision"] = "人工审核"
                report["human_review_required"] = True
            elif report["decision"] == "通过":
                report["decision"] = "修改后提交"
        return {"report": report}

    def retrieve_policy(state: ReviewState) -> dict:
        results = deps.retrieve_applicable_policy(
            state["copy"], state["report"]["findings"], limit=3
        )
        return {"rag_results": results}

    def search_cases(state: ReviewState) -> dict:
        cases = deps.search_similar_cases(
            state["copy"], industry=state["report"]["industry"], limit=3
        )
        return {"similar_cases": cases}

    def rewrite(state: ReviewState) -> dict:
        variants = deps.generate_compliant_rewrite(
            state["copy"], state["product"], state["audience"],
            state["report"]["findings"],
        )
        return {"rewrites": variants}

    def finalize(state: ReviewState) -> dict:
        report = state["report"]
        llm_plan = state.get("llm_plan")
        llm_error = state.get("llm_error")
        used_llm = bool(llm_plan and not llm_error)
        trace = [
            {"tool": "classify_ad_industry", "status": "completed", "summary": f"行业：{report['industry']}（{report['industry_source']}）"},
            {"tool": "check_forbidden_words", "status": "completed", "summary": f"命中 {len(report['findings'])} 个风险项"},
            {"tool": "retrieve_applicable_policy", "status": "completed", "summary": f"召回 {len(state.get('rag_results', []))} 条政策片段"},
            {"tool": "search_similar_cases", "status": "completed", "summary": f"召回 {len(state.get('similar_cases', []))} 条历史案例"},
            {"tool": "analyze_semantic_risk", "status": "completed", "summary": "完成隐含语义风险分析"},
            {"tool": "generate_compliant_rewrite", "status": "completed", "summary": "生成 3 个受约束版本"},
        ]
        if llm_plan:
            trace.insert(0, {"tool": "llm_plan_tools", "status": "completed", "summary": llm_plan["summary"]})
        elif llm_error:
            trace.insert(0, {"tool": "llm_plan_tools", "status": "degraded", "summary": llm_error})
        else:
            summary = (
                "未配置模型，运行可复现的离线审核流程"
                if not state.get("llm_available")
                else "未请求模型增强，运行快速确定性审核流程"
            )
            trace.insert(0, {"tool": "llm_plan_tools", "status": "skipped", "summary": summary})
        report["execution_mode"] = "LLM Agent 增强" if used_llm else "离线确定性流程"
        return {"report": report, "trace": trace}

    return {
        "plan": plan,
        "deterministic_scan": deterministic_scan,
        "retrieve_context": retrieve_context,
        "semantic_llm": semantic_llm,
        "merge_llm_findings": merge_llm_findings,
        "retrieve_policy": retrieve_policy,
        "search_cases": search_cases,
        "rewrite": rewrite,
        "finalize": finalize,
    }


def _wants_llm(state: ReviewState) -> bool:
    return bool(state.get("use_llm") and state.get("llm_available"))


def _route_from_start(state: ReviewState) -> str:
    return "plan" if _wants_llm(state) else "deterministic_scan"


def _route_after_scan(state: ReviewState) -> str:
    """The tool planner is a real gate: skip the semantic node unless the
    model both succeeded and explicitly requested analyze_semantic_risk."""
    plan_result = state.get("llm_plan")
    if (
        _wants_llm(state)
        and state.get("llm_error") is None
        and plan_result
        and "analyze_semantic_risk" in plan_result.get("tools", [])
    ):
        return "retrieve_context"
    return "retrieve_policy"


def build_review_graph(deps: ReviewDeps, checkpointer=None):
    nodes = _build_nodes(deps)
    builder = StateGraph(ReviewState)
    for name, fn in nodes.items():
        builder.add_node(name, fn)

    builder.add_conditional_edges(
        START, _route_from_start, {"plan": "plan", "deterministic_scan": "deterministic_scan"}
    )
    builder.add_edge("plan", "deterministic_scan")
    builder.add_conditional_edges(
        "deterministic_scan", _route_after_scan,
        {"retrieve_context": "retrieve_context", "retrieve_policy": "retrieve_policy"},
    )
    builder.add_edge("retrieve_context", "semantic_llm")
    builder.add_edge("semantic_llm", "merge_llm_findings")
    builder.add_edge("merge_llm_findings", "retrieve_policy")
    builder.add_edge("retrieve_policy", "search_cases")
    builder.add_edge("search_cases", "rewrite")
    builder.add_edge("rewrite", "finalize")
    builder.add_edge("finalize", END)

    return builder.compile(checkpointer=checkpointer or MemorySaver())


def run_review(graph, deps: ReviewDeps, *, copy, landing, industry, product,
               audience, image_size=0, qualification=False, brand_terms="",
               use_llm=False):
    """Invoke the compiled graph and map the result back to the legacy shape
    ``{report, rag_results, similar_cases, rewrites, trace}`` the UI expects."""
    llm_available = deps.llm_agent_class().available
    initial: ReviewState = {
        "copy": copy, "landing": landing, "industry_input": industry,
        "product": product, "audience": audience, "has_image": image_size > 0,
        "image_size": image_size, "qualification": qualification,
        "brand_terms": brand_terms, "use_llm": use_llm,
        "llm_available": llm_available,
        "llm_plan": None, "llm_error": None, "llm_findings": [],
        "trace": [],
    }
    config = {"configurable": {"thread_id": uuid.uuid4().hex}}
    final = graph.invoke(initial, config=config)
    return {
        "report": final["report"],
        "rag_results": final.get("rag_results", []),
        "similar_cases": final.get("similar_cases", []),
        "rewrites": final.get("rewrites", {}),
        "trace": final.get("trace", []),
    }


