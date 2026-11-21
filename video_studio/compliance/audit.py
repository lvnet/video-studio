"""合规审计日志：不可篡改的留痕（append-only）。

记录每次合规相关的判定，供事后追溯「哪次生成用了什么提示词、什么素材、谁放行」。
设计要点：
- 只追加（append-only）：只允许追加记录，不提供修改/删除接口（防篡改）。
- 结构化：事件类型、主体、判定等级、命中详情、时间戳、可选上下文。
- 零依赖：JSONL 文件（每行一条 JSON）。
- 哈希链：每条记录带前一条的哈希，检测中间是否被篡改。

注意：文件级 append-only 是「君子协定」——有文件系统写权限的人仍能改文件；
哈希链能检测篡改但无法阻止。真要防篡改需外部 WORM 存储/签名服务。诚实标注边界。
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .guard import GuardReport, RiskLevel


@dataclass
class AuditEvent:
    seq: int                       # 自增序号
    ts: str                        # ISO 时间戳（UTC）
    event_type: str                # prompt_check / asset_check / content_check / enforce / ...
    subject: str                   # 主体标识（shot_id / asset_id / task_id）
    level: RiskLevel               # 判定等级
    passed: bool                   # 是否放行
    findings: list[dict[str, Any]] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)  # 附加上下文（如 mode）
    prev_hash: str = "0" * 16      # 前一条哈希
    hash: str = ""                 # 本条哈希

    def compute_hash(self) -> str:
        """基于本条内容 + prev_hash 计算哈希（不含 hash 字段本身）。"""
        payload = {k: v for k, v in asdict(self).items() if k != "hash"}
        blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


class AuditLog:
    """只追加审计日志。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()   # 多线程安全追加
        self._last_hash = self._read_last_hash()

    def _read_last_hash(self) -> str:
        if not self.path.exists():
            return "0" * 16
        last = "0" * 16
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        last = json.loads(line).get("hash", last)
                    except json.JSONDecodeError:
                        continue
        return last

    def _next_seq(self) -> int:
        if not self.path.exists():
            return 1
        count = 0
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    count += 1
        return count + 1

    def append(self, event_type: str, subject: str, report: GuardReport,
               context: dict[str, Any] | None = None) -> AuditEvent:
        """记录一次判定。返回写入的事件（含哈希）。"""
        with self._lock:
            event = AuditEvent(
                seq=self._next_seq(),
                ts=datetime.now(timezone.utc).isoformat(),
                event_type=event_type,
                subject=subject,
                level=report.level,
                passed=report.passed,
                findings=[asdict(f) for f in report.findings],
                context=context or {},
                prev_hash=self._last_hash,
            )
            event.hash = event.compute_hash()
            line = json.dumps(asdict(event), ensure_ascii=False, default=str)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
            self._last_hash = event.hash
            return event

    def iter_events(self) -> Iterator[AuditEvent]:
        """迭代全部事件（只读）。"""
        if not self.path.exists():
            return
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                d["level"] = RiskLevel(d["level"]) if "level" in d else RiskLevel.CLEAN
                yield AuditEvent(**d)

    def query(self, subject: str | None = None, level: RiskLevel | None = None,
              event_type: str | None = None) -> list[AuditEvent]:
        """按条件查询事件。"""
        result = []
        for e in self.iter_events():
            if subject is not None and e.subject != subject:
                continue
            if level is not None and e.level != level:
                continue
            if event_type is not None and e.event_type != event_type:
                continue
            result.append(e)
        return result

    def verify_integrity(self) -> tuple[bool, str]:
        """校验哈希链完整性：返回 (是否完好, 说明)。检测记录是否被中间篡改。"""
        prev = "0" * 16
        seq = 0
        for e in self.iter_events():
            seq += 1
            if e.seq != seq:
                return False, f"序号不连续：期望 {seq}，实际 {e.seq}"
            if e.prev_hash != prev:
                return False, f"第 {e.seq} 条 prev_hash 与上一条不符（疑似篡改或删除）"
            if e.hash != e.compute_hash():
                return False, f"第 {e.seq} 条 hash 校验失败（内容被修改）"
            prev = e.hash
        return True, f"哈希链完好，共 {seq} 条记录"
