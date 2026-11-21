"""权威 H3 提示词 Schema 引擎（方案核心壁垒之一）。

把结构化镜头 + 参考资产，按权威 Schema 扩写成 H3 规范提示词。三个关键点：

1. 三段式完整字段：主体(subject) / 场景(scene) / 镜头语言(camera)，缺字段时以空字符串
   补齐而非省略，保证字段完整可回溯。
2. R2V 六段式完整包装：在三段式基础上补齐六段（主体定义、动作、场景、镜头、风格、
   负向约束），并把主参考写入 subject_definitions。
3. (S1)/(S2)↔voice_id 映射：含对白且模式支持音频时，才强制为每个说话人绑定 voice_id；
   无对白或模式不支持音频时不强制（方案明确："含对白且模式支持音频时才强制说话人 ID"）。

Schema 可版本化：RuleSet.version 记录版本，expand 结果里带上 rule_set_version 便于回溯。
LLM 扩写后置为可插拔桩（见 llm.py）；本引擎是确定性规则，不依赖任何外部算力。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..compliance import check_prompt, enforce
from ..models import Asset, RuleSet, Shot


# 三段式默认字段（保持字段完整）
THREE_SEGMENT_FIELDS = ("subject", "scene", "camera")

# 六段式默认字段（R2V 完整包装）
SIX_SEGMENT_FIELDS = (
    "subject_definitions",  # 主体定义（含主参考）
    "action",               # 动作
    "scene",                # 场景
    "camera",               # 镜头语言
    "style",                # 风格
    "negative",             # 负向约束
)

# 支持音频的模式（这些模式含对白时才强制 voice_id）
AUDIO_CAPABLE_MODES = frozenset({"t2v_audio", "r2v", "i2v_audio"})

# 默认规则集 schema（可被 RuleSet 覆盖，实现热加载）
DEFAULT_SCHEMA: dict[str, Any] = {
    "three_segment": list(THREE_SEGMENT_FIELDS),
    "six_segment": list(SIX_SEGMENT_FIELDS),
    "audio_capable_modes": sorted(AUDIO_CAPABLE_MODES),
    "speaker_tokens": ["S1", "S2", "S3", "S4"],
    "negative_default": "低质量, 变形, 水印, 字幕错误, 多余肢体",
    "style_default": " cinema, 3d render",
}


@dataclass
class SpeakerBinding:
    """说话人 → 音色锚点绑定。"""

    token: str            # S1 / S2 ...
    name: str             # 角色名
    voice_id: str         # 音色 ID


@dataclass
class ExpandedPrompt:
    """扩写结果。保持完整字段，便于版本化回溯。"""

    mode: str
    three_segment: dict[str, str] = field(default_factory=dict)
    six_segment: dict[str, str] = field(default_factory=dict)
    speaker_bindings: list[SpeakerBinding] = field(default_factory=list)
    rule_set_version: int = 0
    source_shot_id: str = ""

    def to_submission(self) -> str:
        """拼成最终提交给后端的提示词文本（后端无关）。"""
        lines: list[str] = []
        seg = self.six_segment or self.three_segment
        for k, v in seg.items():
            if v:
                lines.append(f"[{k}] {v}")
        for b in self.speaker_bindings:
            lines.append(f"({b.token}) {b.name} voice_id={b.voice_id}")
        return "\n".join(lines)


def _schema_from(rule_set: RuleSet | None) -> dict[str, Any]:
    if rule_set and rule_set.schema_json:
        return rule_set.schema_json
    return DEFAULT_SCHEMA


def build_speaker_bindings(
    dialogue: str,
    assets: list[Asset],
    mode: str,
    schema: dict[str, Any],
) -> list[SpeakerBinding]:
    """从对白里提取说话人并绑定 voice_id。

    只有「含对白 且 模式支持音频」时才强制 voice_id；否则返回空绑定。
    说话人按出现顺序分配 S1/S2...，从资产里找同名角色的 voice_id。
    """
    if not dialogue or mode not in schema.get("audio_capable_modes", []):
        return []

    # 提取「角色名：台词」里的角色名
    names: list[str] = []
    for line in dialogue.splitlines():
        m = re.match(r"^\s*([^：:]{1,12})[：:]", line)
        if m:
            name = m.group(1).strip()
            if name and name not in names:
                names.append(name)

    tokens = schema.get("speaker_tokens", ["S1", "S2"])
    voice_by_name = {a.name: a.voice_id for a in assets if a.type == "role" and a.voice_id}
    bindings: list[SpeakerBinding] = []
    for i, name in enumerate(names):
        token = tokens[i] if i < len(tokens) else f"S{i + 1}"
        bindings.append(SpeakerBinding(token=token, name=name, voice_id=voice_by_name.get(name, "")))
    return bindings


def expand_prompt(
    shot: Shot,
    assets: list[Asset],
    mode: str = "t2v",
    rule_set: RuleSet | None = None,
    enforce_compliance: bool = True,
) -> ExpandedPrompt:
    """把镜头按权威 Schema 扩写成 H3 提示词。

    - 三段式总是产出（字段完整）。
    - R2V 模式额外产出六段式，主参考写入 subject_definitions。
    - 对白按模式绑定说话人 voice_id。
    - 默认强制合规检查，命中红线直接拒绝（硬性要求零版权风险）。
    """
    schema = _schema_from(rule_set)

    # 三段式：主体来自动作/对白，场景来自 location，镜头语言留待规则补充
    subject = " ".join(filter(None, [shot.action, shot.dialogue]))
    scene = shot.location
    camera = ""
    three = {"subject": subject, "scene": scene, "camera": camera}

    # 合规检查：在扩写前对原始文本守卫，命中即拦
    if enforce_compliance:
        enforce(check_prompt(subject or scene or shot.prompt_text, subject=shot.id))

    bindings = build_speaker_bindings(shot.dialogue, assets, mode, schema)

    six: dict[str, str] = {}
    if mode == "r2v":
        primary = next((a for a in assets if a.approved), None)
        refs = [a for a in assets if a.approved]
        definitions = []
        if primary:
            definitions.append(
                f"主参考: {primary.name} (ref: {primary.image_url}, "
                f"asset_version={primary.version})"
            )
        for a in refs:
            if primary is None or a.id != primary.id:
                definitions.append(f"参考: {a.name} (ref: {a.image_url})")
        six = {
            "subject_definitions": "; ".join(definitions),
            "action": shot.action,
            "scene": scene,
            "camera": camera,
            "style": schema.get("style_default", ""),
            "negative": schema.get("negative_default", ""),
        }

    return ExpandedPrompt(
        mode=mode,
        three_segment=three,
        six_segment=six,
        speaker_bindings=bindings,
        rule_set_version=rule_set.version if rule_set else 0,
        source_shot_id=shot.id,
    )


def validate_bindings(expanded: ExpandedPrompt, mode: str) -> list[str]:
    """校验绑定完整性，返回问题列表。

    含对白且模式支持音频时，每个说话人必须有非空 voice_id（方案硬约束）。
    """
    problems: list[str] = []
    if mode in AUDIO_CAPABLE_MODES and expanded.speaker_bindings:
        for b in expanded.speaker_bindings:
            if not b.voice_id:
                problems.append(f"说话人 {b.token}({b.name}) 缺少 voice_id 绑定")
    return problems
