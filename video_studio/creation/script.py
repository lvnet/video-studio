"""剧本导入与自动拆镜（方案 06：剧本 → 拆镜）。

支持三种格式导入：纯文本 txt、JSON、Markdown。导入后自动拆段成结构化镜头，
缺字段会被标记，供人工补全。LLM 生成/改写/续写后置为可插拔桩（见 llm.py）。

拆镜是「可编辑、可重跑、可追溯」的：本模块只产出候选镜头，不覆盖人工修改。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..models import Episode, Shot, new_id


@dataclass
class RawBeat:
    """从剧本解析出的一个原始节拍。"""

    index: int
    location: str = ""
    action: str = ""
    dialogue: str = ""
    raw: str = ""

    @property
    def is_complete(self) -> bool:
        """至少要有动作或对白之一，否则视为缺字段。"""
        return bool(self.action or self.dialogue)


def parse_txt(text: str) -> list[RawBeat]:
    """解析纯文本剧本。

    约定简单格式：
      - 以「场景：」/「地点：」开头行 → location
      - 以「（」包裹或普通叙述行 → action
      - 形如「角色名：台词」或「角色名:台词」 → dialogue
      - 空行分隔节拍；无空行时每个非空行视为一个节拍
    """
    beats: list[RawBeat] = []
    cur = RawBeat(index=0)
    idx = 0

    def flush():
        nonlocal cur, idx
        if cur.action or cur.dialogue or cur.location:
            cur.index = idx
            beats.append(cur)
            idx += 1
        cur = RawBeat(index=idx)

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            flush()
            continue
        if re.match(r"^(场景|地点)[：:]", line):
            cur.location = re.sub(r"^(场景|地点)[：:]\s*", "", line)
        elif re.match(r"^[^：:（）()]{1,12}[：:]", line) and not line.startswith("（"):
            # 角色名：台词
            name, rest = re.split(r"[：:]", line, maxsplit=1)
            cur.dialogue = (cur.dialogue + f"{name.strip()}：{rest.strip()}\n").strip()
        else:
            cur.action = (cur.action + line + "\n").strip()
    flush()
    return beats


def parse_markdown(text: str) -> list[RawBeat]:
    """解析 Markdown 剧本：以标题(#)或水平分割(---)分节拍，其余同 txt 规则。"""
    segments: list[str] = []
    buf: list[str] = []
    for line in text.splitlines():
        if re.match(r"^#{1,6}\s", line) or line.strip() == "---":
            if buf:
                segments.append("\n".join(buf))
                buf = []
        else:
            buf.append(line)
    if buf:
        segments.append("\n".join(buf))
    beats: list[RawBeat] = []
    for i, seg in enumerate(segments):
        parsed = parse_txt(seg)
        for b in parsed:
            b.index = len(beats)
            beats.append(b)
    return beats


def parse_json(data: str | dict) -> list[RawBeat]:
    """解析 JSON 剧本。接受 {"beats":[...]} 或顶层列表。"""
    obj = json.loads(data) if isinstance(data, str) else data
    if isinstance(obj, dict):
        items = obj.get("beats", obj.get("shots", []))
    elif isinstance(obj, list):
        items = obj
    else:
        items = []
    beats = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        beats.append(RawBeat(
            index=i,
            location=str(item.get("location", "")),
            action=str(item.get("action", "")),
            dialogue=str(item.get("dialogue", "")),
            raw=json.dumps(item, ensure_ascii=False),
        ))
    return beats


def import_script(text: str, fmt: str = "txt") -> list[RawBeat]:
    """按格式导入剧本，返回原始节拍列表。"""
    fmt = fmt.lower()
    if fmt in {"txt", "text"}:
        return parse_txt(text)
    if fmt in {"md", "markdown"}:
        return parse_markdown(text)
    if fmt == "json":
        return parse_json(text)
    raise ValueError(f"不支持的剧本格式：{fmt}")


def beats_to_shots(episode: Episode, beats: list[RawBeat],
                   duration: float = 5.0) -> list[Shot]:
    """把原始节拍转成镜头实体。缺字段的节拍仍生成镜头，但 prompt_text 标注缺失。"""
    shots: list[Shot] = []
    for i, beat in enumerate(beats):
        missing = []
        if not beat.action:
            missing.append("动作")
        if not beat.dialogue:
            missing.append("对白")
        note = f"[缺字段：{','.join(missing)}]" if missing else ""
        shot = Shot(
            id=new_id("shot"),
            episode_id=episode.id,
            index=i,
            location=beat.location,
            action=beat.action,
            dialogue=beat.dialogue,
            prompt_text=note,
            duration=duration,
        )
        shots.append(shot)
    return shots


def missing_fields_report(shots: list[Shot]) -> list[dict[str, Any]]:
    """给前端高亮用：列出每个镜头的缺失字段。"""
    report = []
    for s in shots:
        missing = []
        if not s.action:
            missing.append("action")
        if not s.dialogue:
            missing.append("dialogue")
        if not s.location:
            missing.append("location")
        if missing:
            report.append({"shot_id": s.id, "index": s.index, "missing": missing})
    return report
