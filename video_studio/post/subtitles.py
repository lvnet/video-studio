"""SRT 字幕生成（方案 06：精修成片 → 字幕 SRT）。

纯逻辑，零外部依赖。把镜头对白 + 时间轴转成标准 SRT。
时间基准基于每个镜头的时长顺序累加，可传入起始偏移。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass
class SubtitleCue:
    index: int
    start: float          # 秒
    end: float            # 秒
    text: str


def srt_time(seconds: float) -> str:
    """秒 → SRT 时间戳 HH:MM:SS,mmm。"""
    if seconds < 0:
        seconds = 0.0
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_cues(segments: Iterable[tuple[float, float, str]]) -> list[SubtitleCue]:
    """从 (start, end, text) 序列生成带序号的字幕条目。"""
    return [SubtitleCue(i + 1, s, e, t) for i, (s, e, t) in enumerate(segments)]


def cues_from_shots(shots: list[dict]) -> list[SubtitleCue]:
    """从镜头列表生成字幕：按 shot.duration 顺序累加时间轴，用 dialogue 作文本。"""
    cues: list[SubtitleCue] = []
    cursor = 0.0
    for shot in shots:
        dur = float(shot.get("duration", 5.0))
        text = str(shot.get("dialogue", "")).strip()
        if text:
            cues.append(SubtitleCue(len(cues) + 1, cursor, cursor + dur, text))
        cursor += dur
    return cues


def render_srt(cues: Iterable[SubtitleCue]) -> str:
    """渲染成 SRT 文本。"""
    blocks = []
    for c in cues:
        blocks.append(
            f"{c.index}\n{srt_time(c.start)} --> {srt_time(c.end)}\n{c.text}\n"
        )
    return "\n".join(blocks)
