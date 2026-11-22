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


# --- 短剧创作案格式识别 ------------------------------------------------------
# 真实短剧原稿常是"分集创作案"：带制作元信息、Markdown 场景标题、加粗角色对白、
# 引用块镜头指示。旧解析器把这些当成普通台词/动作，拆出的镜头一塌糊涂。
# 下面这组正则专门识别这些行，让制作标注归制作标注、场景归场景、对白归对白。

# #### 场景 1　夜 / 傅氏宴会厅 / 金色灯光，管弦乐轰鸣
_MD_SCENE_RE = re.compile(r"^#{1,6}\s*场景\s*\d+\s*(.*)$")
# **司仪** *（高亢）*：台词　/　**苏晚**：台词
_MD_DIALOGUE_RE = re.compile(
    r"^\*\*([^*]{1,16})\*\*\s*(?:\*[（(]([^）)]*)[）)]\*\s*)?[：:]\s*(.*)$"
)
# > 镜头：... / 特写：... / 全景：...（镜头调度语言，归动作而非对白）
_CAMERA_RE = re.compile(
    r"^(?:>\s*)?(镜头|特写|全景|中景|近景|远景|空镜|航拍|推镜|拉镜|摇镜|跟拍|特写镜头)[：:]\s*(.*)$"
)
# - **时长**：2分40秒　等制作元信息行
_META_RE = re.compile(r"^[-*]\s*\*\*[^*]+\*\*\s*[：:]")
# **开场钩子** / **集尾钩子** / **下集预告：**...　等段落标记与预告
_SECTION_MARK_RE = re.compile(r"^\*{1,4}[^*]+\*{1,4}[：:]?\s*$")
_PREVIEW_RE = re.compile(r"^\*{0,4}(下集预告|预告|下集)[^*]*[：:]")


def _scene_location(rest: str) -> str:
    """从「夜 / 傅氏宴会厅 / 金色灯光」里取地点（第二段），否则取首段。"""
    rest = (rest or "").strip()
    parts = [p.strip() for p in re.split(r"\s*[/／]\s*", rest) if p.strip()]
    return parts[1] if len(parts) >= 2 else (parts[0] if parts else rest)


def _strip_emphasis(text: str) -> str:
    """去掉行内 Markdown 强调星号，保留文字。"""
    return re.sub(r"\*+", "", text or "").strip()


def normalize_screenplay_text(text: str) -> str:
    """把短剧创作案正文规范成干净可读的剧本文本（供展示与存储）。

    真实原稿里的 Markdown 标记（`**加粗**`、`#### 场景`、`> 镜头`、`*（情绪）*`）
    直接展示给用户就是"乱糟糟"。这里逐行清洗为统一格式：
      - 制作元信息（时长/主要场景/本集任务）、纯段落标记、下集预告 → 丢弃
      - `#### 场景 N　夜 / 地点 / 氛围` → `场景：地点`
      - `**角色** *（情绪）*：台词` → `角色（情绪）：台词`
      - `> 镜头：…` → `镜头：…`
      - 普通叙述行去掉行内强调星号后原样保留
      - 合并多余空行（连续空行压成一个）
    规范化后的文本仍可被 ``parse_txt`` 正确拆镜（格式兼容）。
    """
    out: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            # 连续空行压成一个
            if out and out[-1] != "":
                out.append("")
            continue

        # 制作元信息 / 段落标记 / 预告 → 丢弃
        if _META_RE.match(line) or _SECTION_MARK_RE.match(line) or _PREVIEW_RE.match(line):
            continue

        # Markdown 场景标题 → 场景：地点
        m = _MD_SCENE_RE.match(line)
        if m:
            loc = _scene_location(m.group(1))
            out.append(f"场景：{loc}" if loc else "")
            continue

        # 创作案对白 → 角色（情绪）：台词
        m = _MD_DIALOGUE_RE.match(line)
        if m:
            name, mood, content = m.group(1).strip(), m.group(2), m.group(3)
            speaker = f"{name}（{mood.strip()}）" if mood else name
            out.append(f"{speaker}：{_strip_emphasis(content)}")
            continue

        # 镜头调度：去掉引用符，保留「镜头：…/特写：…」
        m = _CAMERA_RE.match(line)
        if m:
            out.append(f"{m.group(1)}：{_strip_emphasis(m.group(2))}")
            continue

        # 其它行：去掉行内强调星号
        out.append(_strip_emphasis(line))

    # 去掉首尾空行
    while out and out[0] == "":
        out.pop(0)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


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
    """解析纯文本剧本，同时兼容简单格式与短剧创作案格式。

    简单格式（原有约定）：
      - 「场景：」/「地点：」开头行 → location
      - 「角色名：台词」→ dialogue
      - 普通叙述行 → action；空行分隔节拍

    短剧创作案格式（新增兼容）：
      - 「#### 场景 N　夜 / 地点 / 氛围」→ location（取地点段），并开新节拍
      - 「**角色** *（情绪）*：台词」→ dialogue
      - 「> 镜头：…」「特写：…」等镜头调度 → action
      - 「- **时长**：…」「**开场钩子**」「下集预告」→ 制作标注，丢弃
    """
    beats: list[RawBeat] = []
    cur = RawBeat(index=0)
    idx = 0
    pending_location = ""  # 场景标题的地点暂存，挂到下一个有内容的节拍上

    def flush():
        nonlocal cur, idx
        if cur.action or cur.dialogue:
            cur.index = idx
            beats.append(cur)
            idx += 1
            cur = RawBeat(index=idx)
        elif cur.location:
            # 只有地点没有内容：不成孤立空镜头，把地点传给下一节拍
            cur = RawBeat(index=idx, location=cur.location)
        else:
            cur = RawBeat(index=idx)

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            flush()
            continue

        # 制作元信息 / 段落标记 / 预告 → 丢弃，不进任何镜头
        if _META_RE.match(line) or _SECTION_MARK_RE.match(line) or _PREVIEW_RE.match(line):
            continue

        # Markdown 场景标题：#### 场景 N　夜 / 地点 / 氛围 → 新场景，开新节拍
        # 地点暂存在 pending_location，挂到紧随其后的第一段内容，避免空镜头。
        m = _MD_SCENE_RE.match(line)
        if m:
            flush()
            pending_location = _scene_location(m.group(1))
            continue

        # 消费暂存的场景地点：挂到当前节拍（此时它通常是刚 flush 出的空节拍）
        if pending_location:
            if not cur.location:
                cur.location = pending_location
            else:
                # 当前节拍已有别的地点，先落袋，再以场景地点开新节拍
                flush()
                cur.location = pending_location
            pending_location = ""

        # 简单场景行：场景：/地点：
        m = re.match(r"^(场景|地点)[：:]\s*(.*)$", line)
        if m:
            cur.location = m.group(2).strip()
            continue

        # 创作案对白：**角色** *（情绪）*：台词
        m = _MD_DIALOGUE_RE.match(line)
        if m:
            name, mood, content = m.group(1).strip(), m.group(2), m.group(3)
            speaker = f"{name}（{mood.strip()}）" if mood else name
            cur.dialogue = (cur.dialogue + f"{speaker}：{_strip_emphasis(content)}\n").strip()
            continue

        # 镜头调度语言：> 镜头：… / 特写：… → 归动作
        m = _CAMERA_RE.match(line)
        if m:
            cur.action = (cur.action + f"{m.group(1)}：{_strip_emphasis(m.group(2))}\n").strip()
            continue

        # 简单对白：角色名：台词（排除括号开头的动作说明）
        if re.match(r"^[^：:（）()]{1,12}[：:]", line) and not line.startswith("（"):
            name, rest = re.split(r"[：:]", line, maxsplit=1)
            cur.dialogue = (cur.dialogue + f"{name.strip()}：{_strip_emphasis(rest)}\n").strip()
        else:
            # 普通叙述（含 "画面右侧…" 等）→ 动作
            cur.action = (cur.action + _strip_emphasis(line) + "\n").strip()
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
