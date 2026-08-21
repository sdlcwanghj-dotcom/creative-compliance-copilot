import unittest

from review_graph import ReviewDeps, build_review_graph, run_review


def _base_report():
    return {
        "findings": [],
        "risk_score": 10,
        "decision": "通过",
        "human_review_required": False,
        "industry": "护肤品",
        "industry_source": "关键词匹配",
    }


def _fake_scan(copy, landing, industry, product, audience, image_size,
               qualification, brand_terms):
    report = _base_report()
    report["copy"] = copy
    report["product"] = product
    report["image_size"] = image_size
    return report


def _fake_add_finding(findings, category, evidence, reason, severity_cn,
                      rule_id, source):
    findings.append({
        "category": category, "evidence": evidence, "reason": reason,
        "severity": severity_cn, "policy_id": rule_id, "source": source,
    })


def _make_deps(agent_class):
    return ReviewDeps(
        scan_material=_fake_scan,
        retrieve_applicable_policy=lambda copy, findings, limit=3: [{"id": "P1"}],
        search_similar_cases=lambda copy, industry=None, limit=3: [{"score": 1.0}],
        generate_compliant_rewrite=lambda copy, product, audience, findings: {"safe": copy},
        add_finding=_fake_add_finding,
        get_policy_ids=lambda: {"AD-001", "AD-002"},
        llm_agent_class=agent_class,
    )


class OfflineAgent:
    available = False

    def plan_tools(self, context):
        raise AssertionError("plan_tools must not run when LLM is unavailable")

    def analyze_semantic_risk(self, context, allowed_rule_ids):
        raise AssertionError("semantic node must not run when LLM is unavailable")


class SemanticAgent:
    available = True

    def plan_tools(self, context):
        return {"tools": ["analyze_semantic_risk"], "summary": "已选择语义分析"}

    def analyze_semantic_risk(self, context, allowed_rule_ids):
        return [{
            "category": "隐含承诺", "evidence": "婴儿肌", "reason": "暗示疗效",
            "severity": "high", "rule_id": "AD-001",
        }]


class PlannerSkipsSemanticAgent:
    available = True

    def plan_tools(self, context):
        return {"tools": ["retrieve_applicable_policy"], "summary": "仅确定性检查"}

    def analyze_semantic_risk(self, context, allowed_rule_ids):
        raise AssertionError("semantic node must be gated out by the planner")


class PlannerRaisesAgent:
    available = True

    def plan_tools(self, context):
        raise RuntimeError("endpoint down")

    def analyze_semantic_risk(self, context, allowed_rule_ids):
        raise AssertionError("semantic node must not run after a planner failure")


class ReviewGraphTests(unittest.TestCase):
    def _run(self, agent_class, use_llm):
        deps = _make_deps(agent_class)
        graph = build_review_graph(deps)
        return run_review(
            graph, deps, copy="7天淡化斑点恢复婴儿肌", landing="以页面为准",
            industry="护肤品", product="精华", audience="大众", image_size=0,
            qualification=False, brand_terms="", use_llm=use_llm,
        )

    def test_offline_path_skips_llm_and_stays_deterministic(self):
        result = self._run(OfflineAgent, use_llm=True)
        self.assertEqual(result["report"]["findings"], [])
        self.assertEqual(result["report"]["execution_mode"], "离线确定性流程")
        self.assertEqual(result["trace"][0]["status"], "skipped")
        self.assertEqual(result["rag_results"], [{"id": "P1"}])
        self.assertEqual(result["rewrites"], {"safe": "7天淡化斑点恢复婴儿肌"})

    def test_use_llm_false_short_circuits_even_when_available(self):
        result = self._run(SemanticAgent, use_llm=False)
        self.assertEqual(result["report"]["findings"], [])
        self.assertEqual(result["report"]["execution_mode"], "离线确定性流程")

    def test_semantic_findings_merge_and_escalate(self):
        result = self._run(SemanticAgent, use_llm=True)
        report = result["report"]
        self.assertEqual(len(report["findings"]), 1)
        self.assertEqual(report["findings"][0]["severity"], "高")
        self.assertEqual(report["findings"][0]["source"], "LLM 语义分析")
        self.assertEqual(report["decision"], "人工审核")
        self.assertTrue(report["human_review_required"])
        self.assertEqual(report["risk_score"], 21)
        self.assertEqual(report["execution_mode"], "LLM Agent 增强")

    def test_planner_gates_out_semantic_node(self):
        result = self._run(PlannerSkipsSemanticAgent, use_llm=True)
        self.assertEqual(result["report"]["findings"], [])
        self.assertEqual(result["report"]["execution_mode"], "LLM Agent 增强")

    def test_planner_failure_degrades_to_deterministic(self):
        result = self._run(PlannerRaisesAgent, use_llm=True)
        self.assertEqual(result["report"]["findings"], [])
        self.assertEqual(result["report"]["execution_mode"], "离线确定性流程")
        self.assertEqual(result["trace"][0]["status"], "degraded")


if __name__ == "__main__":
    unittest.main()
