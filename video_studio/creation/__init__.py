"""创作层：八步制作链、剧本拆镜、权威提示词 Schema 引擎、规则热加载。"""

from .pipeline import PIPELINE, STEP_SPECS, Step, StepSpec, next_step
from .script import (
    RawBeat,
    beats_to_shots,
    import_script,
    missing_fields_report,
    parse_json,
    parse_markdown,
    parse_txt,
)
from .prompt_schema import (
    AUDIO_CAPABLE_MODES,
    DEFAULT_SCHEMA,
    SIX_SEGMENT_FIELDS,
    THREE_SEGMENT_FIELDS,
    ExpandedPrompt,
    SpeakerBinding,
    build_speaker_bindings,
    expand_prompt,
    validate_bindings,
)
from .rules import RuleRegistry
from .llm import LLMProvider, RewriteResult, StubLLMProvider, get_provider, set_provider
from .workflow import (
    FunctionPoint, WorkbenchStep, WORKBENCH_STEPS, steps_as_dicts, step_keys,
)

__all__ = [
    "PIPELINE", "STEP_SPECS", "Step", "StepSpec", "next_step",
    "FunctionPoint", "WorkbenchStep", "WORKBENCH_STEPS", "steps_as_dicts", "step_keys",
    "RawBeat", "beats_to_shots", "import_script", "missing_fields_report",
    "parse_json", "parse_markdown", "parse_txt",
    "AUDIO_CAPABLE_MODES", "DEFAULT_SCHEMA", "SIX_SEGMENT_FIELDS", "THREE_SEGMENT_FIELDS",
    "ExpandedPrompt", "SpeakerBinding", "build_speaker_bindings", "expand_prompt",
    "validate_bindings",
    "RuleRegistry",
    "LLMProvider", "RewriteResult", "StubLLMProvider", "get_provider", "set_provider",
]
