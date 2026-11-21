"""控制面测试：失败码 + 成本闸门 + 调度器（标准库 unittest）。"""

import tempfile
import time
import unittest

from video_studio.compliance import ComplianceBlocked
from video_studio.control import (
    BudgetExceeded,
    CostGate,
    EstimateError,
    LocalScheduler,
    PriceTable,
    RetryPolicy,
    TaskState,
    assert_asset_approvable,
    diagnose,
    estimate_cost,
    failure_spec,
    is_retryable,
    normalize,
)
from video_studio.models import (
    Asset,
    Episode,
    LocalStore,
    PriceRule,
    Project,
    Shot,
    Task,
)


class FailureTests(unittest.TestCase):
    def test_namespaces(self):
        self.assertEqual(failure_spec("PROVIDER_TIMEOUT").namespace, "PROVIDER_")
        self.assertEqual(failure_spec("PROMPT_COMPLIANCE").namespace, "PROMPT_")
        self.assertEqual(failure_spec("ASSET_NOT_APPROVED").namespace, "ASSET_")
        self.assertEqual(failure_spec("COST_BUDGET_EXCEEDED").namespace, "COST_")

    def test_retryable_classification(self):
        self.assertTrue(is_retryable("PROVIDER_TRANSIENT"))
        self.assertFalse(is_retryable("PROVIDER_INVALID_PARAMS"))
        self.assertFalse(is_retryable("PROMPT_COMPLIANCE"))
        self.assertEqual(failure_spec("PROVIDER_AUTH").retry, RetryPolicy.MANUAL)

    def test_diagnose_is_chinese_with_action(self):
        d = diagnose("PROVIDER_AUTH")
        self.assertIn("鉴权", d)
        self.assertIn("建议", d)

    def test_normalize_provider_errors(self):
        self.assertEqual(normalize("HTTP 429 rate limit exceeded"), "PROVIDER_TRANSIENT")
        self.assertEqual(normalize("read timeout"), "PROVIDER_TIMEOUT")
        self.assertEqual(normalize("invalid api key"), "PROVIDER_AUTH")
        self.assertEqual(normalize("something odd"), "UNKNOWN")


class PriceAndEstimateTests(unittest.TestCase):
    def test_price_table_exact_and_wildcard(self):
        pt = PriceTable([
            PriceRule(id="r1", backend_type="api", model="h3", unit="second", unit_price_cents=50),
            PriceRule(id="r2", backend_type="api", model="*", unit="second", unit_price_cents=30),
        ])
        self.assertEqual(pt.unit_price("api", "h3", "second"), 50)
        self.assertEqual(pt.unit_price("api", "other", "second"), 30)
        self.assertIsNone(pt.unit_price("local", "h3", "second"))

    def test_estimate_rounds_up(self):
        pt = PriceTable([
            PriceRule(id="r1", backend_type="api", model="h3", unit="second", unit_price_cents=50),
        ])
        self.assertEqual(estimate_cost(pt, "api", "h3", "second", 3.5), 175)
        with self.assertRaises(EstimateError):
            estimate_cost(pt, "api", "other", "second", 1)


class CostGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = LocalStore(self.tmp.name)
        self.s.add("projects", Project(id="p1", name="x", budget_total=10.0, budget_daily=6.0))
        self.s.add("episodes", Episode(id="e1", project_id="p1", index=1))
        self.s.add("shots", Shot(id="s1", episode_id="e1", index=1))
        self.s.add("tasks", Task(id="t1", shot_id="s1", kind="t2v"))
        self.pt = PriceTable([
            PriceRule(id="r1", backend_type="api", model="h3", unit="second", unit_price_cents=50),
        ])
        self.gate = CostGate(self.s, self.pt)

    def tearDown(self):
        self.tmp.cleanup()

    def test_check_within_budget(self):
        d = self.gate.check("p1", 100)
        self.assertTrue(d.allowed)
        self.assertEqual(d.limit_cents, 1000)

    def test_check_exceeds_total_budget(self):
        d = self.gate.check("p1", 1001)
        self.assertFalse(d.allowed)
        self.assertIn("total预算超限", d.detail)

    def test_check_exceeds_daily_budget(self):
        d = self.gate.check("p1", 601)  # > 600 日预算但 < 1000 总预算
        self.assertFalse(d.allowed)
        self.assertIn("daily预算超限", d.detail)

    def test_unlimited_when_zero(self):
        self.s.update("projects", "p1", budget_total=0.0, budget_daily=0.0)
        self.assertTrue(self.gate.check("p1", 10_000_000).allowed)

    def test_reserve_settle_flow(self):
        d = self.gate.reserve("p1", self.s.get("tasks", "t1") and Task.from_dict(self.s.get("tasks", "t1")), 200)
        self.assertTrue(d.allowed)
        self.assertEqual(self.s.ledger("p1").reserved_cents, 200)
        self.assertEqual(self.s.get("tasks", "t1")["cost_reserved_cents"], 200)
        # 结算：实际 180，多退少补
        self.gate.settle("p1", Task.from_dict(self.s.get("tasks", "t1")), 180)
        ledger = self.s.ledger("p1")
        self.assertEqual(ledger.reserved_cents, 0)
        self.assertEqual(ledger.spent_cents, 180)
        self.assertEqual(self.s.get("tasks", "t1")["cost_actual_cents"], 180)

    def test_reserve_then_release_on_failure(self):
        self.gate.reserve("p1", Task.from_dict(self.s.get("tasks", "t1")), 200)
        self.gate.release("p1", Task.from_dict(self.s.get("tasks", "t1")))
        ledger = self.s.ledger("p1")
        self.assertEqual(ledger.reserved_cents, 0)
        self.assertEqual(ledger.spent_cents, 0)

    def test_reserve_blocked_when_would_exceed(self):
        # 先花掉 900，剩余预算 100；再预留 200 应被拦
        ledger = self.s.ledger("p1")
        ledger.spent_cents = 900
        self.s.save_ledger(ledger)
        d = self.gate.reserve("p1", Task.from_dict(self.s.get("tasks", "t1")), 200)
        self.assertFalse(d.allowed)
        self.assertEqual(self.s.ledger("p1").reserved_cents, 0)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = LocalStore(self.tmp.name)
        self.s.add("projects", Project(id="p1", name="x"))
        self.s.add("episodes", Episode(id="e1", project_id="p1", index=1))
        self.s.add("shots", Shot(id="s1", episode_id="e1", index=1))
        self.sched = LocalScheduler(self.s, lease_ttl=1)

    def tearDown(self):
        self.tmp.cleanup()

    def test_idempotent_submit(self):
        r1 = self.sched.submit("s1", "t2v", {}, idempotency_key="k1")
        r2 = self.sched.submit("s1", "t2v", {}, idempotency_key="k1")
        self.assertEqual(r1.task_id, r2.task_id)
        self.assertEqual(r2.reason, "idempotent_hit")
        self.assertEqual(len(self.s.find("tasks")), 1)

    def test_dispatch_lease_and_reclaim(self):
        r = self.sched.submit("s1", "t2v", {})
        self.sched.enqueue(r.task_id)
        d = self.sched.dispatch(r.task_id)
        self.assertTrue(d.leased)
        self.assertEqual(self.s.get("tasks", r.task_id)["status"], TaskState.LEASED.value)
        # 未过期不回收
        self.assertEqual(self.sched.reclaim_expired(), [])
        # 过期后回收
        time.sleep(1.1)
        reclaimed = self.sched.reclaim_expired()
        self.assertIn(r.task_id, reclaimed)
        self.assertEqual(self.s.get("tasks", r.task_id)["status"], TaskState.QUEUED.value)

    def test_failure_retry_then_dead_letter(self):
        r = self.sched.submit("s1", "t2v", {})
        self.sched.enqueue(r.task_id)
        s1 = self.sched.on_failure(r.task_id, "PROVIDER_TRANSIENT")
        s2 = self.sched.on_failure(r.task_id, "PROVIDER_TRANSIENT")
        s3 = self.sched.on_failure(r.task_id, "PROVIDER_TRANSIENT")
        self.assertEqual(s1, TaskState.QUEUED.value)
        self.assertEqual(s2, TaskState.QUEUED.value)
        self.assertEqual(s3, TaskState.DEAD_LETTER.value)


class AssetGateTests(unittest.TestCase):
    def test_unapproved_asset_rejected(self):
        asset = Asset(id="a1", project_id="p1", type="role", name="x", approved=False)
        with self.assertRaises(PermissionError):
            assert_asset_approvable(asset)

    def test_approved_without_provenance_rejected(self):
        asset = Asset(id="a2", project_id="p1", type="role", name="x", approved=True)
        with self.assertRaises(ComplianceBlocked):
            assert_asset_approvable(asset)

    def test_approved_with_valid_provenance_passes(self):
        asset = Asset(id="a3", project_id="p1", type="role", name="x", approved=True,
                      provenance={"asset_id": "a3", "source": "self_created"})
        assert_asset_approvable(asset)  # 不抛异常


if __name__ == "__main__":
    unittest.main()
