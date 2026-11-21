"""后期成片：字幕、裁头、拼接、片尾 CTA、升清抽象。"""

from .subtitles import SubtitleCue, build_cues, cues_from_shots, render_srt, srt_time
from .compose import (
    ComposePlan,
    CtaSpec,
    TrimSpec,
    UpscaleMode,
    build_compose_plan,
    trim_head_noise,
    upscale_strategy,
)

__all__ = [
    "SubtitleCue", "build_cues", "cues_from_shots", "render_srt", "srt_time",
    "ComposePlan", "CtaSpec", "TrimSpec", "UpscaleMode",
    "build_compose_plan", "trim_head_noise", "upscale_strategy",
]
