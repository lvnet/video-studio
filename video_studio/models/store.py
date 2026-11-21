"""本地 JSON 持久化（单机模式，零外部依赖）。

用 PostgreSQL 做事实源是方案对多机/并发场景的建议；但开源 MVP 的单用户本地
模式先用文件存储，避免引入数据库这个外部依赖，符合「外部依赖后置」的要求。
接口按 Repository 设计，未来替换为 SQLAlchemy 实现不改上层。

关键业务约束集中在这里，防止数据一致性被上层误操作破坏：
- adopt_output：采用版唯一真源，设置某输出为 adopted 时，同一 task 的其他输出自动取消。
- lock_asset_version：任务引用资产时锁定版本（写 TaskAsset），复现一致性。
- 写文件用「临时文件 + 原子替换」，避免中途崩溃写坏数据。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

from .entities import (
    ENTITY_TYPES,
    Asset,
    CostLedger,
    EntityMixin,
    Output,
    Project,
    Task,
    TaskAsset,
    utcnow,
)


class StoreError(KeyError):
    pass


class LocalStore:
    """一个根目录 = 一个项目数据集。每类实体一个 JSON 文件。"""

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, dict[str, dict[str, Any]]] = {}
        for name in ENTITY_TYPES:
            self._cache[name] = self._load(name)

    # ---- 基础读写 ----
    def _path(self, name: str) -> Path:
        return self.root / f"{name}.json"

    def _load(self, name: str) -> dict[str, dict[str, Any]]:
        p = self._path(name)
        if not p.exists():
            return {}
        with open(p, "r", encoding="utf-8") as f:
            try:
                data = json.load(f)
            except json.JSONDecodeError:
                return {}
        # 兼容两种格式：{"items":[...]} 或直接 {id: {...}}
        if isinstance(data, dict) and "items" in data:
            return {item["id"]: item for item in data["items"]}
        if isinstance(data, dict):
            return data
        return {}

    def _flush(self, name: str) -> None:
        """原子写：先写临时文件再替换。

        Windows 上临时文件/目标文件可能被杀毒软件短暂占用，os.replace 会偶发
        PermissionError / OSError。这里做短退避重试，彻底消除这类偶发失败。
        """
        p = self._path(name)
        data = {"items": list(self._cache[name].values())}
        fd, tmp = tempfile.mkstemp(dir=str(self.root), suffix=".tmp", text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            last_exc = None
            for attempt in range(8):
                try:
                    os.replace(tmp, p)
                    return
                except OSError as exc:  # Windows 文件锁偶发
                    last_exc = exc
                    time.sleep(0.01 * (attempt + 1))
            raise last_exc
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    # ---- 通用 CRUD ----
    def add(self, name: str, entity: EntityMixin) -> None:
        d = entity.to_dict()
        eid = d.get("id")
        if not eid:
            raise StoreError("实体缺少 id")
        if eid in self._cache[name]:
            raise StoreError(f"{name} 中已存在 id={eid}")
        self._cache[name][eid] = d
        self._flush(name)

    def get(self, name: str, eid: str) -> dict[str, Any]:
        if eid not in self._cache[name]:
            raise StoreError(f"{name} 中不存在 id={eid}")
        return self._cache[name][eid]

    def update(self, entity: str, eid: str, **changes: Any) -> dict[str, Any]:
        if eid not in self._cache[entity]:
            raise StoreError(f"{entity} 中不存在 id={eid}")
        self._cache[entity][eid].update(changes)
        self._flush(entity)
        return self._cache[entity][eid]

    def delete(self, name: str, eid: str) -> None:
        if eid not in self._cache[name]:
            raise StoreError(f"{name} 中不存在 id={eid}")
        del self._cache[name][eid]
        self._flush(name)

    def all(self, name: str) -> list[dict[str, Any]]:
        return list(self._cache[name].values())

    def find(self, entity: str, **filters: Any) -> list[dict[str, Any]]:
        return [
            d for d in self._cache[entity].values()
            if all(d.get(k) == v for k, v in filters.items())
        ]

    # ---- 业务约束：采用版唯一真源 ----
    def adopt_output(self, output_id: str) -> None:
        """设置某输出为采用版，并把同一 task 的其他输出取消采用。

        同时同步 shot.adopted_task_id（冗余字段），保证两处一致。
        """
        out = self.get("outputs", output_id)
        task = self.get("tasks", out["task_id"])

        # 取消同 task 下其他采用版
        for other in self.find("outputs", task_id=out["task_id"]):
            if other["id"] != output_id and other.get("is_adopted"):
                self.update("outputs", other["id"], is_adopted=False)

        self.update("outputs", output_id, is_adopted=True)

        # 同步镜头冗余字段
        shot_id = task["shot_id"]
        if self._exists("shots", shot_id):
            self.update("shots", shot_id, adopted_task_id=out["task_id"])

    def adopted_output(self, task_id: str) -> dict[str, Any] | None:
        adopted = self.find("outputs", task_id=task_id, is_adopted=True)
        return adopted[0] if adopted else None

    # ---- 业务约束：资产版本锁定 ----
    def lock_asset_version(self, task_id: str, asset_id: str, version: int,
                           is_primary: bool = False) -> TaskAsset:
        """把任务引用的资产版本固化下来，供复现一致性校验。"""
        from .entities import new_id
        ta = TaskAsset(
            id=new_id("ta"), task_id=task_id, asset_id=asset_id,
            asset_version=version, is_primary=is_primary,
        )
        self.add("task_assets", ta)
        return ta

    def asset_versions_for_task(self, task_id: str) -> list[dict[str, Any]]:
        return self.find("task_assets", task_id=task_id)

    # ---- 成本账本 ----
    def ledger(self, project_id: str) -> CostLedger:
        try:
            data = self.get("cost_ledgers", project_id)
        except StoreError:
            ledger = CostLedger(id=project_id)
            self.add("cost_ledgers", ledger)
            return ledger
        return CostLedger.from_dict(data)

    def save_ledger(self, ledger: CostLedger) -> None:
        d = ledger.to_dict()
        d["updated_at"] = utcnow()
        if ledger.id in self._cache["cost_ledgers"]:
            self._cache["cost_ledgers"][ledger.id] = d
        else:
            self._cache["cost_ledgers"][ledger.id] = d
        self._flush("cost_ledgers")

    def _exists(self, name: str, eid: str) -> bool:
        return eid in self._cache[name]

    # ---- 示例数据 ----
    @classmethod
    def demo(cls, root: str | os.PathLike[str]) -> "LocalStore":
        """初始化一个带原创示例项目的空数据集（示例内容均为原创，零版权风险）。"""
        store = cls(root)
        if store.find("projects"):
            return store
        p = Project(id="proj_demo", name="示例短剧·雨夜", aspect_ratio="9:16")
        store.add("projects", p)
        return store
