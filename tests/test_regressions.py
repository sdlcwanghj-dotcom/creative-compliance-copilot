import os
import tempfile
import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest

import audit_store


class ReviewStoreRegressionTests(unittest.TestCase):
    def setUp(self):
        self.original_db_path = audit_store.DB_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        audit_store.DB_PATH = Path(self.temp_dir.name) / "review.db"
        audit_store.initialize_database()

    def tearDown(self):
        audit_store.DB_PATH = self.original_db_path
        self.temp_dir.cleanup()

    def test_closed_ticket_cannot_receive_second_decision(self):
        ticket = {
            "工单号": "TEST-001", "广告主": "测试广告主", "行业": "护肤品",
            "商品": "测试商品", "文案": "日常护理体验", "风险": "低",
            "优先级": "P2", "状态": "待领取", "机器结论": "通过",
            "命中规则": "", "提交时间": "2026-08-13 10:00",
        }
        audit_store.create_ticket(ticket)
        audit_store.save_human_decision("TEST-001", "审核员 A", "已通过", "", ticket["文案"], "通过")
        with self.assertRaises(ValueError):
            audit_store.save_human_decision("TEST-001", "审核员 A", "已驳回", "", ticket["文案"], "通过")
        self.assertEqual(audit_store.list_tickets()[0]["状态"], "已通过")

    def test_similar_cases_drop_weak_matches(self):
        self.assertEqual(audit_store.search_similar_cases("unrelated test text"), [])
        exact_copy = "7天淡化所有斑点，恢复婴儿肌"
        matches = audit_store.search_similar_cases(exact_copy)
        self.assertEqual(matches[0]["score"], 1.0)
        self.assertTrue(all(row["score"] >= 0.12 for row in matches))


class ReviewAppRegressionTests(unittest.TestCase):
    def make_app(self):
        os.environ["LLM_API_KEY"] = ""
        return AppTest.from_file("app.py", default_timeout=30).run()

    def test_closed_workbench_disables_decisions(self):
        app = self.make_app()
        app.button[0].click().run()
        decision_buttons = [button for button in app.button if button.label in {"通过", "要求整改", "驳回", "升级复核"}]
        self.assertEqual(len(decision_buttons), 4)
        self.assertTrue(all(button.disabled for button in decision_buttons))

    def test_default_rewrite_is_a_complete_message(self):
        app = self.make_app()
        app.button[0].click().run()
        app.radio[0].set_value("机器预审").run()
        rewritten = [item.value for item in app.markdown if 'class="recommend"' in item.value]
        self.assertEqual(len(rewritten), 1)
        self.assertNotIn("斑点，让你重获", rewritten[0])
        self.assertNotIn("现在下单元", rewritten[0])

    def test_logout_removes_sensitive_session_state(self):
        app = self.make_app()
        app.button[0].click().run()
        app.session_state["current_report"] = {"secret": True}
        app.session_state["ocr_text"] = "secret"
        app.button[-1].click().run()
        state = app.session_state.filtered_state
        self.assertIsNone(state.get("current_user"))
        self.assertFalse(state.get("current_report"))
        self.assertNotIn("ocr_text", state)


if __name__ == "__main__":
    unittest.main()
