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
    """本地桩：不调用任何外部算力，返回可预测的占位结果，供流程测试。"""

    def generate_outline(self, synopsis: str, episodes: int) -> list[str]:
        return [f"第{i + 1}集大纲（基于梗概：{synopsis[:20]}…）" for i in range(episodes)]

    def rewrite(self, text: str, instruction: str) -> RewriteResult:
        return RewriteResult(
            text=f"[改写:{instruction}] {text}",
            before=text,
            model="stub",
        )

    def continue_script(self, context: str, n: int) -> str:
        return f"{context}\n[续写段落 ×{n}（桩）]"


_provider: LLMProvider = StubLLMProvider()


def get_provider() -> LLMProvider:
    return _provider


def set_provider(provider: LLMProvider) -> None:
    """注入真实 LLM 后端（未来接入时调用）。"""
    global _provider
    _provider = provider
