import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, PharmacovigilanceService, iso, utcnow
from signals import DEFAULT_RULES, SignalRules, list_signals, scan, get_signal, decide, register_action, complete_action


class SignalFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = PharmacovigilanceService(Path(self.tmp.name) / "test.db")
        self.repo = self.svc.repo

    def tearDown(self):
        self.tmp.cleanup()

    def intake(self, n, *, product="DrugA", event="肝损伤", region="CN",
               serious=False, fatal=False, dedupe=None):
        return self.svc.create_case(
            f"reporter-{region.lower()}", "reporter", region,
            {"patient_ref": f"P-{n}", "region": region, "product": product, "event_term": event,
             "source": "email", "dedupe_key": dedupe or f"key-{n}",
             "received_at": iso(utcnow()), "serious": serious, "fatal": fatal},
        )["case"]

    def test_threshold_and_idempotent_scan(self):
        self.intake(1)
        self.intake(2)
        first = scan(self.repo, "reviewer-1", "medical_reviewer")
        self.assertEqual(first["created"], [])
        self.intake(3, serious=True)
        result = scan(self.repo, "reviewer-1", "medical_reviewer")
        self.assertEqual(len(result["created"]), 1)
        sig = result["created"][0]
        self.assertEqual(sig["case_count"], 3)
        self.assertEqual(sig["serious_count"], 1)
        self.assertEqual(sig["fatal_count"], 0)
        self.assertIn("case_threshold", __import__("json").loads(sig["trigger_reasons_json"]))
        # 重复检查不产生第二份，只刷新统计
        again = scan(self.repo, "reviewer-1", "medical_reviewer")
        self.assertEqual(again["created"], [])
        self.assertEqual(again["refreshed"], [sig["id"]])
        self.assertEqual(len(list_signals(self.repo, "global_admin", "")), 1)

    def test_merged_source_follows_target_without_double_count(self):
        a = self.intake(1)
        b = self.intake(2)
        c = self.intake(3)
        # b、c 合并进 a
        self.svc.merge_cases(b["id"], "admin-1", "global_admin", {"target_case_id": a["id"]})
        self.svc.merge_cases(c["id"], "admin-1", "global_admin", {"target_case_id": a["id"]})
        result = scan(self.repo, "reviewer-1", "medical_reviewer")
        self.assertEqual(result["created"], [])  # 有效案例只剩 1 例，不到阈值
        detail = self.svc.get_case(a["id"], "global_admin", "")
        # 合并来源的 intakes 已跟随目标案例
        self.assertEqual(len(detail["intakes"]), 3)
        # 再补两例独立案例，达到阈值
        self.intake(4)
        self.intake(5)
        result = scan(self.repo, "reviewer-1", "medical_reviewer")
        self.assertEqual(len(result["created"]), 1)
        self.assertEqual(result["created"][0]["case_count"], 3)

    def test_fatal_single_case_triggers_signal(self):
        self.intake(1, serious=True, fatal=True)
        result = scan(self.repo, "reviewer-1", "medical_reviewer")
        self.assertEqual(len(result["created"]), 1)
        reasons = __import__("json").loads(result["created"][0]["trigger_reasons_json"])
        self.assertEqual(reasons, ["fatal"])

    def test_regions_aggregated_and_region_lead_scoped(self):
        self.intake(1, region="CN")
        self.intake(2, region="US")
        self.intake(3, region="US", serious=True)
        sig_id = scan(self.repo, "reviewer-1", "medical_reviewer")["created"][0]["id"]
        detail = get_signal(self.repo, sig_id, "regional_lead", "US")
        # 区域负责人只看本区域证据：合并跟随口径不变
        self.assertEqual(len(detail["evidence"]), 2)
        self.assertTrue(all(e["region"] == "US" for e in detail["evidence"]))
        self.assertEqual(len(list_signals(self.repo, "regional_lead", "CN")), 1)
        # 无关区域看不到该信号
        self.assertEqual(list_signals(self.repo, "regional_lead", "EU"), [])
        # reporter 无权查看
        with self.assertRaises(ApiError) as ctx:
            list_signals(self.repo, "reporter", "CN")
        self.assertEqual(ctx.exception.status, 403)
        # 区域负责人不能执行检查或判定
        with self.assertRaises(ApiError):
            scan(self.repo, "lead-cn", "regional_lead")

    def test_decide_requires_rationale_and_is_one_shot(self):
        for n in range(3):
            self.intake(10 + n)
        sig_id = scan(self.repo, "reviewer-1", "medical_reviewer")["created"][0]["id"]
        with self.assertRaises(ApiError) as ctx:
            decide(self.repo, sig_id, "reviewer-1", "medical_reviewer",
                   {"decision": "confirmed", "rationale": ""})
        self.assertEqual(ctx.exception.code, "rationale_required")
        decide(self.repo, sig_id, "reviewer-1", "medical_reviewer",
               {"decision": "confirmed", "rationale": "三例肝损伤时间聚集"})
        with self.assertRaises(ApiError) as ctx:
            decide(self.repo, sig_id, "reviewer-1", "medical_reviewer",
                   {"decision": "rejected", "rationale": "改主意"})
        self.assertEqual(ctx.exception.code, "signal_decided")
        with self.assertRaises(ApiError) as ctx:
            decide(self.repo, sig_id, "lead-cn", "regional_lead",
                   {"decision": "confirmed", "rationale": "越权"})
        self.assertEqual(ctx.exception.status, 403)

    def test_actions_only_after_confirmation_and_overdue_reminder(self):
        for n in range(3):
            self.intake(20 + n)
        sig_id = scan(self.repo, "reviewer-1", "medical_reviewer")["created"][0]["id"]
        # 未确认不能登记措施
        with self.assertRaises(ApiError) as ctx:
            register_action(self.repo, sig_id, "lead-cn", "regional_lead", "CN",
                            {"measure": "发函", "owner": "张三", "due_at": iso(utcnow())})
        self.assertEqual(ctx.exception.code, "signal_not_confirmed")
        decide(self.repo, sig_id, "reviewer-1", "medical_reviewer",
               {"decision": "confirmed", "rationale": "符合阈值"})
        past = (utcnow() - timedelta(days=1)).date().isoformat()
        action = register_action(self.repo, sig_id, "lead-cn", "regional_lead", "CN",
                                 {"measure": "更新说明书", "owner": "李四", "due_at": past})
        self.assertTrue(action["overdue"])
        signals = {s["id"]: s for s in list_signals(self.repo, "medical_reviewer", "")}
        self.assertTrue(signals[sig_id]["has_overdue"])
        detail = get_signal(self.repo, sig_id, "global_admin", "")
        self.assertEqual(detail["actions"][0]["measure"], "更新说明书")
        self.assertEqual(detail["decisions"][0]["rationale"], "符合阈值")
        self.assertEqual(len(detail["evidence"]), 3)
        # 完成措施：首次生效、重复完成幂等；完成后不再逾期
        first = complete_action(self.repo, action["id"], "lead-cn", "regional_lead", "CN")
        self.assertFalse(first["idempotent"])
        again = complete_action(self.repo, action["id"], "lead-cn", "regional_lead", "CN")
        self.assertTrue(again["idempotent"])
        signals = {s["id"]: s for s in list_signals(self.repo, "medical_reviewer", "")}
        self.assertFalse(signals[sig_id]["has_overdue"])
        with self.assertRaises(ApiError) as ctx:
            complete_action(self.repo, 999, "admin-1", "global_admin", "")
        self.assertEqual(ctx.exception.code, "action_not_found")

    def test_rules_are_separately_maintained(self):
        # 判定规则可调，与数据/页面解耦
        loose = SignalRules(min_cases=1, fatal_trigger=False)
        self.intake(30)
        result = scan(self.repo, "reviewer-1", "medical_reviewer", loose)
        self.assertEqual(len(result["created"]), 1)
        self.assertEqual(result["rules"]["min_cases"], 1)
        self.assertEqual(DEFAULT_RULES.min_cases, 3)


if __name__ == "__main__":
    unittest.main()
