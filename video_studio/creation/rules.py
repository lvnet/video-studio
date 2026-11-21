"""规则集热加载与版本化（方案 04 P1：改规则无需重启服务）。

RuleSet 存本地 JSON（由 LocalStore 管理），RuleRegistry 提供「读最新版」与
「热重载」。规则文件变更只需 reload()，不重启进程。

外部算力（LLM 生成/改写/续写）放在 llm.py 的桩实现里，本模块不依赖它。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..models import RuleSet, new_id
from .prompt_schema import DEFAULT_SCHEMA


class RuleRegistry:
    """规则集注册表，支持从文件热加载。"""

    def __init__(self, rules_dir: str | Path | None = None):
        self.rules_dir = Path(rules_dir) if rules_dir else None
        self._rules: dict[str, RuleSet] = {}
        self._default_id: str = ""

    def register(self, rule_set: RuleSet, set_default: bool = False) -> None:
        self._rules[rule_set.id] = rule_set
        if set_default or rule_set.is_default or not self._default_id:
            self._default_id = rule_set.id
            rule_set.is_default = True

    def get(self, rule_set_id: str) -> RuleSet | None:
        return self._rules.get(rule_set_id)

    def default(self) -> RuleSet:
        if not self._default_id:
            self._bootstrap_default()
        return self._rules[self._default_id]

    def _bootstrap_default(self) -> None:
        rs = RuleSet(
            id=new_id("rule"),
            name="H3 权威默认规则",
            version=1,
            schema_json=dict(DEFAULT_SCHEMA),
            is_default=True,
        )
        self.register(rs, set_default=True)

    def reload(self) -> int:
        """从 rules_dir 重新加载所有 *.json 规则文件，返回加载数量。

        文件格式即 RuleSet.to_dict()；版本号递增生成新 RuleSet，旧版本仍可通过 id 取。
        """
        if not self.rules_dir or not self.rules_dir.exists():
            return 0
        loaded = 0
        for p in sorted(self.rules_dir.glob("*.json")):
            try:
                data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            name = data.get("name", p.stem)
            # 每次 reload 生成新版本，保留可回溯
            existing = [r for r in self._rules.values() if r.name == name]
            version = max((r.version for r in existing), default=0) + 1
            rs = RuleSet(
                id=new_id("rule"),
                name=name,
                version=version,
                schema_json=data.get("schema_json", dict(DEFAULT_SCHEMA)),
                is_default=bool(data.get("is_default", False)),
            )
            self.register(rs, set_default=rs.is_default)
            loaded += 1
        return loaded
