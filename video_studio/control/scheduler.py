"""本地调度器：租约 / 幂等 / 重试（方案 09，不依赖 Redis）。

方案的租约用 Redis sorted set + Lua；单机本地模式用「内存 + 文件」实现同等语义，
把 Redis 这个外部依赖后置。核心语义：
- 幂等：同一 idempotency_key 重复提交不产生第二个任务。
- 租约：任务被派单后进入 leased，带租约过期时间；超时未续约则回收到 queued。
- 失败重试：回到 queued（而非 leased）重新派单，避免再次绑定坏节点；3 次进死信。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..compliance import AssetProvenance, check_asset_provenance, enforce
from ..models import Asset, Task, new_id, utcnow
from .state import MAX_RETRIES, TaskState

if TYPE_CHECKING:
    from ..models import LocalStore


LEASE_TTL_SECONDS = 60


@dataclass
class DispatchResult:
    task_id: str
    leased: bool
    reason: str = ""


class LocalScheduler:
    def __init__(self, store: "LocalStore", lease_ttl: int = LEASE_TTL_SECONDS):
        self.store = store
        self.lease_ttl = lease_ttl
        self._leases: dict[str, float] = {}  # task_id -> 到期时间戳

    def submit(self, shot_id: str, kind: str, params: dict,
               idempotency_key: str = "", mode: str = "t2v",
               backend_id: str = "") -> DispatchResult:
        """建任务；幂等键命中则复用已有任务。"""
        if idempotency_key:
            existing = self.store.find("tasks", idempotency_key=idempotency_key)
            if existing:
                return DispatchResult(existing[0]["id"], leased=False, reason="idempotent_hit")
        task = Task(
            id=new_id("task"), shot_id=shot_id, kind=kind, mode=mode,
            backend_id=backend_id, params_json=params,
            status=TaskState.CREATED.value, idempotency_key=idempotency_key,
        )
        self.store.add("tasks", task)
        return DispatchResult(task.id, leased=False, reason="created")

    def enqueue(self, task_id: str) -> None:
        self.store.update("tasks", task_id, status=TaskState.QUEUED.value)

    def dispatch(self, task_id: str) -> DispatchResult:
        """派单：queued → leased，设置租约。"""
        t = self.store.get("tasks", task_id)
        if t["status"] != TaskState.QUEUED.value:
            return DispatchResult(task_id, leased=False, reason=f"not_queued:{t['status']}")
        self.store.update("tasks", task_id, status=TaskState.LEASED.value)
        self._leases[task_id] = time.time() + self.lease_ttl
        return DispatchResult(task_id, leased=True, reason="leased")

    def reclaim_expired(self) -> list[str]:
        """回收过期租约：leased 且租约已过 → 回 queued。"""
        now = time.time()
        reclaimed: list[str] = []
        for task_id, expiry in list(self._leases.items()):
            if now > expiry:
                t = self.store.get("tasks", task_id)
                if t["status"] == TaskState.LEASED.value:
                    self.store.update("tasks", task_id, status=TaskState.QUEUED.value)
                    reclaimed.append(task_id)
                del self._leases[task_id]
        return reclaimed

    def on_failure(self, task_id: str, failure_code: str) -> str:
        """失败处理：未超上限回 queued 重派，否则进死信。返回新状态。"""
        t = self.store.get("tasks", task_id)
        attempt = t.get("attempt", 0) + 1
        if attempt >= MAX_RETRIES:
            new_state = TaskState.DEAD_LETTER.value
        else:
            new_state = TaskState.QUEUED.value
        self.store.update("tasks", task_id,
                          status=new_state, attempt=attempt, failure_code=failure_code)
        self._leases.pop(task_id, None)
        return new_state


def assert_asset_approvable(asset: Asset) -> None:
    """资产进入可用前的双重硬门槛：人工批准 + 来源合规。"""
    if not asset.approved:
        raise PermissionError(f"资产 {asset.id} 未批准，禁止被镜头引用")
    prov = AssetProvenance.from_dict(asset.provenance) if asset.provenance else None
    enforce(check_asset_provenance(prov))
