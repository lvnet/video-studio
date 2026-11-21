"""累计成本闸门（方案核心壁垒之一，防账单失控）。

方案 09 的三件事：
  ① 提交前做 (预估 + 已花 + 在途冻结 ≤ 上限) 原子校验，预算与单价表均可配置；
  ② 提交时冻结预留、成功转实际、失败释放，任务记录三段成本；
  ③ 所有生成结果留存任务日志，异常消耗可回溯。

实现要点：
- PriceTable 提供可配置单价，避免硬编码刊例价。
- CostLedger 按项目跟踪 spent（已花）/ reserved（在途冻结）。
- Gate 的 check/commit/release/settle 全部基于本地账本，事务性由「重新载入 + 原子写」
  保证；本地单进程下等价于原子。
- 预算上限为 0 表示不限。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..models import CostLedger, PriceRule, Task, utcnow
from .failure import spec as failure_spec

if TYPE_CHECKING:
    from ..models import LocalStore


class BudgetExceeded(Exception):
    pass


class EstimateError(Exception):
    pass


@dataclass
class GateDecision:
    allowed: bool
    estimate_cents: int
    spent_cents: int
    reserved_cents: int
    limit_cents: int
    detail: str = ""


class PriceTable:
    """可配置单价表。按 (backend_type, model, unit) 查价。"""

    def __init__(self, rules: list[PriceRule] | None = None):
        self._rules = rules or []

    def add(self, rule: PriceRule) -> None:
        self._rules.append(rule)

    def unit_price(self, backend_type: str, model: str, unit: str) -> int | None:
        # 精确匹配优先，其次同 backend+unit 通配 model="*"
        for r in self._rules:
            if r.backend_type == backend_type and r.model == model and r.unit == unit:
                return r.unit_price_cents
        for r in self._rules:
            if r.backend_type == backend_type and r.model == "*" and r.unit == unit:
                return r.unit_price_cents
        return None


def estimate_cost(price_table: PriceTable, backend_type: str, model: str,
                  unit: str, quantity: float) -> int:
    """按单价表估算成本（分），向上取整避免低估。"""
    import math
    p = price_table.unit_price(backend_type, model, unit)
    if p is None:
        raise EstimateError(f"单价表缺少 {backend_type}/{model} 的 {unit} 计价")
    return math.ceil(p * quantity)


class CostGate:
    """闸门：校验 → 冻结 → 结算/释放。"""

    def __init__(self, store: "LocalStore", price_table: PriceTable | None = None):
        self.store = store
        self.price_table = price_table or PriceTable()

    def _limit(self, project_id: str, daily: bool = False) -> int:
        project = self.store.get("projects", project_id)
        yuan = project.get("budget_daily" if daily else "budget_total", 0.0)
        return int(round(yuan * 100))

    def check(self, project_id: str, estimate_cents: int) -> GateDecision:
        """提交前原子校验：(预估 + 已花 + 在途冻结 ≤ 上限)。

        同时检查总预算与日预算；任一超限即拒绝。上限为 0 表示不限。
        """
        ledger = self.store.ledger(project_id)
        total = estimate_cents + ledger.spent_cents + ledger.reserved_cents
        reasons: list[str] = []

        for daily, label in ((False, "total"), (True, "daily")):
            limit = self._limit(project_id, daily=daily)
            if limit > 0 and total > limit:
                reasons.append(
                    f"{label}预算超限：预估{estimate_cents} + 已花{ledger.spent_cents} "
                    f"+ 在途{ledger.reserved_cents} = {total} 分 > 上限 {limit} 分"
                )

        allowed = not reasons
        return GateDecision(
            allowed=allowed,
            estimate_cents=estimate_cents,
            spent_cents=ledger.spent_cents,
            reserved_cents=ledger.reserved_cents,
            limit_cents=self._limit(project_id),
            detail="；".join(reasons),
        )

    def reserve(self, project_id: str, task: Task, estimate_cents: int) -> GateDecision:
        """提交时冻结预留：先校验，通过则把预估计入在途冻结并写回任务三段成本。"""
        decision = self.check(project_id, estimate_cents)
        if not decision.allowed:
            return decision
        ledger = self.store.ledger(project_id)
        ledger.reserved_cents += estimate_cents
        self.store.save_ledger(ledger)
        self.store.update("tasks", task.id,
                          cost_estimate_cents=estimate_cents,
                          cost_reserved_cents=estimate_cents)
        return decision

    def release(self, project_id: str, task: Task) -> None:
        """失败释放：把在途冻结退回。"""
        task_now = self.store.get("tasks", task.id)
        reserved = task_now.get("cost_reserved_cents", 0)
        if not reserved:
            return
        ledger = self.store.ledger(project_id)
        ledger.reserved_cents = max(0, ledger.reserved_cents - reserved)
        self.store.save_ledger(ledger)
        self.store.update("tasks", task.id, cost_reserved_cents=0,
                          failure_code=task_now.get("failure_code", ""))

    def settle(self, project_id: str, task: Task, actual_cents: int) -> None:
        """成功结算：在途冻结转实际。多退少补。"""
        task_now = self.store.get("tasks", task.id)
        reserved = task_now.get("cost_reserved_cents", 0)
        ledger = self.store.ledger(project_id)
        ledger.reserved_cents = max(0, ledger.reserved_cents - reserved)
        ledger.spent_cents += actual_cents
        self.store.save_ledger(ledger)
        self.store.update("tasks", task.id,
                          cost_reserved_cents=0, cost_actual_cents=actual_cents)
