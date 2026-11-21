"""成片装配：裁头杂音 + 拼接 + 片尾 CTA + 升清抽象（方案 06/04）。

设计成「纯逻辑 + ffmpeg 可选」：
- 裁头时长计算、拼接清单、片尾参数、升清策略都是纯逻辑，零依赖可测。
- 真正执行媒体处理时才调用 ffmpeg；未安装时给出明确的安装建议，不静默失败。

H3 开头约 0.12–0.16s 杂音，默认裁 0.14s。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class UpscaleMode(str, Enum):
    API_REBIRTH = "api_rebirth"      # API 模式：768P→2K 重生（外部算力，后置）
    LOCAL_SEEDVR2 = "local_seedvr2"  # 本地模式：SeedVR2 2× 两次（后置）


@dataclass
class TrimSpec:
    """裁头/尾参数。"""

    path: str
    start: float = 0.14             # 裁掉开头杂音（秒）
    end: float | None = None        # 裁掉尾部（秒），None 表示不裁


@dataclass
class CtaSpec:
    """片尾 CTA。"""

    text: str = ""
    duration: float = 3.0
    style: str = "simple"


@dataclass
class ComposePlan:
    """成片装配计划：可执行、可审查、可复现。"""

    trims: list[TrimSpec] = field(default_factory=list)
    concat_paths: list[str] = field(default_factory=list)
    srt_path: str = ""
    cta: CtaSpec | None = None
    upscale: UpscaleMode | None = None
    output: str = "final.mp4"

    def to_ffmpeg_script(self) -> list[str]:
        """生成 ffmpeg 命令序列（供有 ffmpeg 时执行；无依赖也能审查计划）。

        纯函数：不改自身状态，可重复调用得到相同结果。
        """
        cmds: list[str] = []
        trimmed_paths: list[str] = []
        for i, t in enumerate(self.trims):
            out = f"trimmed_{i}.mp4"
            parts = ["ffmpeg", "-y", "-i", t.path, "-ss", str(t.start)]
            if t.end is not None:
                parts += ["-to", str(t.end)]
            parts += ["-c", "copy", out]
            cmds.append(" ".join(parts))
            trimmed_paths.append(out)
        if trimmed_paths:
            list_file = "concat_list.txt"
            cmds.append(
                f"(printf \"file '%s'\\n\" {' '.join(trimmed_paths)}) > {list_file}"
            )
            parts = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file]
            if self.srt_path:
                parts += ["-vf", f"subtitles={self.srt_path}"]
            parts += ["-c", "copy", self.output]
            cmds.append(" ".join(parts))
        return cmds


def trim_head_noise(start: float = 0.14) -> float:
    """返回默认裁头时长（方案：H3 开头 0.12–0.16s 杂音）。"""
    if not (0.10 <= start <= 0.20):
        raise ValueError(f"裁头时长 {start}s 超出经验区间 0.10–0.20s")
    return start


def build_compose_plan(
    clip_paths: list[str],
    srt_path: str = "",
    cta_text: str = "",
    upscale: UpscaleMode | None = None,
    output: str = "final.mp4",
    trim_start: float = 0.14,
) -> ComposePlan:
    """根据片段列表构建装配计划。"""
    ts = trim_head_noise(trim_start)
    plan = ComposePlan(
        trims=[TrimSpec(path=p, start=ts) for p in clip_paths],
        concat_paths=list(clip_paths),
        srt_path=srt_path,
        cta=CtaSpec(text=cta_text) if cta_text else None,
        upscale=upscale,
        output=output,
    )
    return plan


def upscale_strategy(backend_kind: str) -> UpscaleMode:
    """后端无关的升清抽象：按后端类型选策略。真实执行后置。"""
    if backend_kind in {"cloud_api", "api"}:
        return UpscaleMode.API_REBIRTH
    if backend_kind in {"local_comfyui", "local_gpu"}:
        return UpscaleMode.LOCAL_SEEDVR2
    raise ValueError(f"未知后端类型：{backend_kind}")
