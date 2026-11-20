"""八步制作链。

创意 → 剧本 → 角色设定 → 分镜 → 关键帧 → 动画/视频 → 配音/字幕 → 合成成片

每一步的产物都是带版本的 Artifact，失败可定位、可重试、可续跑。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Step(str, Enum):
    IDEA = "idea"                 # 创意
    SCRIPT = "script"             # 剧本
    CHARACTER = "character"       # 角色设定
    STORYBOARD = "storyboard"     # 分镜
    KEYFRAME = "keyframe"         # 关键帧
    VIDEO = "video"               # 动画/视频
    AUDIO = "audio"               # 配音/字幕
    COMPOSE = "compose"           # 合成成片


PIPELINE: list[Step] = [
    Step.IDEA,
    Step.SCRIPT,
    Step.CHARACTER,
    Step.STORYBOARD,
    Step.KEYFRAME,
    Step.VIDEO,
    Step.AUDIO,
    Step.COMPOSE,
]


@dataclass(frozen=True)
class StepSpec:
    step: Step
    name: str
    # 该步默认使用的算力后端类型（可由项目配置覆盖）
    preferred_backend: str | None = None


STEP_SPECS: list[StepSpec] = [
    StepSpec(Step.IDEA, "创意", None),           # 纯文本，控制面内完成
    StepSpec(Step.SCRIPT, "剧本", None),         # 文本生成
    StepSpec(Step.CHARACTER, "角色设定", "cloud_api"),
    StepSpec(Step.STORYBOARD, "分镜", "cloud_api"),
    StepSpec(Step.KEYFRAME, "关键帧", "cloud_api"),
    StepSpec(Step.VIDEO, "动画/视频", "cloud_api"),
    StepSpec(Step.AUDIO, "配音/字幕", "cloud_api"),
    StepSpec(Step.COMPOSE, "合成成片", "local_comfyui"),
]


def next_step(current: Step) -> Step | None:
    idx = PIPELINE.index(current)
    return PIPELINE[idx + 1] if idx + 1 < len(PIPELINE) else None
