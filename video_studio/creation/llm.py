"""LLM 能力可插拔桩（外部算力，后置实现）。

方案里的「AI 生成梗概 / 改写 / 续写」依赖 LLM 外部算力，按米老板要求放到最后。
这里只定义统一接口与本地桩实现，保证上层创作流程可先跑通、可测试。

未来接入真实 LLM 时，只需实现 LLMProvider 并在 get_provider() 注册，不改上层。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class RewriteResult:
    text: str
    before: str
    model: str = "stub"


class LLMProvider(ABC):
    @abstractmethod
    def generate_outline(self, synopsis: str, episodes: int) -> list[str]:
        """根据梗概生成分集大纲。"""

    @abstractmethod
    def rewrite(self, text: str, instruction: str) -> RewriteResult:
        """改写并保留前后对照。"""

    @abstractmethod
    def continue_script(self, context: str, n: int) -> str:
        """续写，读取前文上下文。"""


class StubLLMProvider(LLMProvider):
    """本地剧本生成引擎（确定性规则，零外部算力、零版权风险）。

    历史上这里是纯占位桩（"第X集大纲（基于梗概…）"），产出无法拆镜，
    导致八步流程看着全却都不出真东西。现改为调用 story_engine：
    按梗概生成结构化、可拆镜的分集正文，改写/续写也做真实规则处理。
    接口保持不变，未来接真实 LLM 仍只需 set_provider() 注入。
    """

    def generate_outline(self, synopsis: str, episodes: int) -> list[str]:
        from .story_engine import generate_outlines
        return generate_outlines(synopsis, episodes)

    def rewrite(self, text: str, instruction: str) -> RewriteResult:
        from .story_engine import rewrite_text
        return RewriteResult(
            text=rewrite_text(text, instruction),
            before=text,
            model="local_engine",
        )

    def continue_script(self, context: str, n: int) -> str:
        from .story_engine import continue_script
        return continue_script(context, n)


_provider: LLMProvider = StubLLMProvider()


def get_provider() -> LLMProvider:
    return _provider


def set_provider(provider: LLMProvider) -> None:
    """注入真实 LLM 后端（未来接入时调用）。"""
    global _provider
    _provider = provider
