import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from datetime import timedelta

from app import ApiError, PharmacovigilanceService, iso, parse_time, utcnow
import signal_rules


def case_body(dedupe, *, product="DrugA", event="肝损伤", region="CN", serious=False, fatal=False):
    return {
        "patient_ref": f"P-{dedupe}", "region": region, "product": product, "event_term": event,
        "source": "email", "dedupe_key": dedupe, "received_at": iso(utcnow()),
        "serious": serious, "fatal": fatal,
    }


class SignalFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = PharmacovigilanceService(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.tmp.cleanup()

    def add_cases(self, n, **kw):
        ids = []
        for i in range(n):
            ids.append(self.svc.create_case(
                "rep", "global_admin", "", case_body(f"k-{kw.get('event', 'e')}-{i}-{n}", **kw)
            )["case"]["id"])
        return ids

    def test_threshold_three_cases_creates_one_signal(self):
        self.add_cases(2, serious=True)
        scan = self.svc.scan_signals("rev-1", "medical_reviewer")
        self.assertEqual(scan["created_count"], 0)
        self.add_cases(1)
        scan = self.svc.scan_signals("rev-1", "medical_reviewer")
        self.assertEqual(scan["created_count"], 1)
        signal = scan["created"][0]
        self.assertEqual(signal["case_count"], 3)
        self.assertEqual(signal["serious_count"], 2)
        self.assertEqual(signal["fatal_count"], 0)
        self.assertEqual(signal["regions"], ["CN"])
        self.assertEqual(signal["status"], "open")
        # 重复检查不产生第二份，只刷新证据
        again = self.svc.scan_signals("rev-1", "medical_reviewer")
        self.assertEqual(again["created_count"], 0)
        self.assertEqual(again["updated_count"], 1)
        listing = self.svc.list_signals("medical_reviewer", "")
        self.assertEqual(len(listing["signals"]), 1)

    def test_single_fatal_case_creates_signal(self):
        self.add_cases(1, fatal=True, serious=True)
        scan = self.svc.scan_signals("rev-1", "medical_reviewer")
        self.assertEqual(scan["created_count"], 1)
        self.assertIn(signal_rules.FATAL_RULE, scan["created"][0]["rules"]["matched_rules"])

    def test_merged_source_follows_target_and_is_not_double_counted(self):
        ids = self.add_cases(3)
        # 第四个案例与产品+事件词同组合，随后被合并进首个目标案例
        extra = self.svc.create_case("rep", "global_admin", "", case_body("k-extra"))["case"]["id"]
        self.svc.scan_signals("rev-1", "medical_reviewer")
        self.svc.merge_cases(extra, "admin", "global_admin", {"target_case_id": ids[0]})
        self.svc.scan_signals("admin", "global_admin")
        detail = self.svc.get_signal(1, "global_admin", "")
        self.assertEqual(detail["signal"]["case_count"], 3)
        self.assertEqual(detail["evidence_total"], 3)
        evidence_ids = {e["case_id"] for e in detail["evidence"]}
        self.assertIn(ids[0], evidence_ids)
        self.assertNotIn(extra, evidence_ids)

    def test_regions_and_severity_aggregation(self):
        self.add_cases(2, region="CN", serious=True)
        self.add_cases(1, region="US", fatal=True, serious=True)
        signal = self.svc.scan_signals("rev", "medical_reviewer")["created"][0]
        self.assertEqual(signal["case_count"], 3)
        self.assertEqual(signal["serious_count"], 3)
        self.assertEqual(signal["fatal_count"], 1)
        self.assertEqual(signal["regions"], ["CN", "US"])

    def test_region_lead_sees_only_own_region_signal(self):
        self.add_cases(2, region="CN", serious=True)
        self.add_cases(1, region="US", fatal=True, serious=True)
        self.svc.scan_signals("rev", "medical_reviewer")
        cn = self.svc.list_signals("regional_lead", "CN")
        self.assertEqual(len(cn["signals"]), 1)
        detail = self.svc.get_signal(1, "regional_lead", "CN")
        self.assertEqual(len(detail["evidence"]), 2)
        self.assertEqual(detail["evidence_total"], 3)

    def test_region_isolation_for_unrelated_signal(self):
        self.add_cases(3, region="US", event="过敏")
        self.svc.scan_signals("rev", "medical_reviewer")
        self.assertEqual(self.svc.list_signals("regional_lead", "CN")["signals"], [])
        with self.assertRaises(ApiError) as ctx:
            self.svc.get_signal(1, "regional_lead", "CN")
        self.assertEqual(ctx.exception.status, 403)

    def test_reporter_cannot_scan(self):
        with self.assertRaises(ApiError) as ctx:
            self.svc.scan_signals("rep", "reporter")
        self.assertEqual(ctx.exception.status, 403)

    def test_decision_requires_rationale_and_cannot_repeat(self):
        ids = self.add_cases(3)
        self.svc.scan_signals("rev", "medical_reviewer")
        with self.assertRaises(ApiError) as ctx:
            self.svc.decide_signal(1, "rev", "medical_reviewer", "", {"decision": "confirmed"})
        self.assertEqual(ctx.exception.code, "rationale_required")
        result = self.svc.decide_signal(1, "rev", "medical_reviewer", "",
                                        {"decision": "rejected", "rationale": "三例均与产品无关"})
        self.assertEqual(result["signal"]["status"], "rejected")
        with self.assertRaises(ApiError) as ctx:
            self.svc.decide_signal(1, "rev", "medical_reviewer", "",
                                   {"decision": "confirmed", "rationale": "改判"})
        self.assertEqual(ctx.exception.code, "signal_decided")
        # 驳回信号不能登记措施
        with self.assertRaises(ApiError) as ctx:
            self.svc.register_action(1, "rev", "medical_reviewer", "",
                                     {"measure": "更新说明书", "owner": "o1",
                                      "due_at": iso(utcnow())})
        self.assertEqual(ctx.exception.code, "signal_not_confirmed")

    def test_confirm_register_action_and_overdue_reminder(self):
        self.add_cases(3)
        self.svc.scan_signals("rev", "medical_reviewer")
        due_past = iso(utcnow().replace(microsecond=0))
        past = iso(parse_time(due_past) - timedelta(days=2))
        future = iso(parse_time(due_past) + timedelta(days=9))
        # 确认并同时登记一条已逾期措施
        self.svc.decide_signal(1, "rev", "medical_reviewer", "",
                               {"decision": "confirmed", "rationale": "聚集性明确",
                                "measure": "发医务函", "owner": "owner-a", "due_at": past})
        detail = self.svc.get_signal(1, "global_admin", "")
        self.assertTrue(detail["signal"]["overdue"])
        overdue = self.svc.overdue_signals("medical_reviewer", "")
        self.assertEqual(overdue["count"], 1)
        action_id = detail["actions"][0]["id"]
        self.assertEqual(detail["actions"][0]["overdue"], 1)
        # 再登记一条未逾期措施
        second = self.svc.register_action(1, "admin", "global_admin", "",
                                          {"measure": "更新说明书", "owner": "owner-b", "due_at": future})
        self.assertEqual(second["action"]["overdue"], 0)
        # 完成逾期措施后页面不再提醒
        self.svc.complete_action(action_id, "admin", "global_admin", "", {})
        overdue = self.svc.overdue_signals("medical_reviewer", "")
        self.assertEqual(overdue["count"], 0)
        done = self.svc.get_signal(1, "global_admin", "")["actions"][0]
        self.assertEqual(done["status"], "completed")

    def test_region_lead_cannot_decide_or_register(self):
        self.add_cases(3)
        self.svc.scan_signals("rev", "medical_reviewer")
        with self.assertRaises(ApiError) as ctx:
            self.svc.decide_signal(1, "lead", "regional_lead", "CN",
                                   {"decision": "confirmed", "rationale": "x"})
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(ApiError) as ctx:
            self.svc.register_action(1, "lead", "regional_lead", "CN",
                                     {"measure": "m", "owner": "o", "due_at": iso(utcnow())})
        self.assertEqual(ctx.exception.status, 403)

    def test_new_cases_join_existing_signal_evidence(self):
        self.add_cases(3)
        self.svc.scan_signals("rev", "medical_reviewer")
        self.add_cases(2, serious=True)
        self.svc.scan_signals("admin", "global_admin")
        detail = self.svc.get_signal(1, "global_admin", "")
        self.assertEqual(detail["signal"]["case_count"], 5)
        self.assertEqual(detail["signal"]["serious_count"], 2)
        self.assertEqual(len(detail["decisions"]), 0)

    def test_grouping_is_case_and_whitespace_insensitive(self):
        self.add_cases(2, product="DrugA", event="肝损伤")
        self.svc.create_case("rep", "global_admin", "",
                             case_body("k-space", product="  druga ", event=" 肝损伤 "))
        signal = self.svc.scan_signals("rev", "medical_reviewer")["created"]
        self.assertEqual(len(signal), 1)
        self.assertEqual(signal[0]["case_count"], 3)
        self.assertEqual(signal[0]["product"], "DrugA")


if __name__ == "__main__":
    unittest.main()
