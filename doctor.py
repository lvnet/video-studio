#!/usr/bin/env python3
"""一键环境自检：逐项检查并给出修复建议。

用法：
    python doctor.py

设计原则：只检查不修改；缺依赖时逐项给出可执行的修复建议；返回码 0=通过。
外部算力（ffmpeg / API 密钥）标为「可选」，缺失不阻断本地核心功能。
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
from dataclasses import dataclass


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    required: bool = True


def check_python() -> CheckResult:
    ok = sys.version_info >= (3, 11)
    return CheckResult(
        "Python 版本", ok,
        f"当前 {sys.version.split()[0]}；需 >=3.11" if not ok else f"{sys.version.split()[0]}",
    )


def check_package(pkg: str, name: str) -> CheckResult:
    ok = importlib.util.find_spec(pkg) is not None
    return CheckResult(
        name, ok,
        "已安装" if ok else f"未安装；运行：python -m pip install {pkg}",
    )


def check_ffmpeg() -> CheckResult:
    ok = shutil.which("ffmpeg") is not None
    return CheckResult(
        "ffmpeg（可选，成片导出需要）", ok,
        "已找到" if ok else "未找到；后期成片导出需安装 ffmpeg（核心流程不受影响）",
        required=False,
    )


def check_write_permission() -> CheckResult:
    import os
    import tempfile
    try:
        with tempfile.NamedTemporaryFile(delete=True):
            pass
        return CheckResult("本地写权限", True, "可写")
    except OSError as e:
        return CheckResult("本地写权限", False, f"不可写：{e}")


def check_core_imports() -> CheckResult:
    try:
        from video_studio.compliance import check_prompt
        from video_studio.control import CostGate
        from video_studio.creation import expand_prompt
        from video_studio.models import LocalStore
        from video_studio.post import build_compose_plan
        return CheckResult("核心模块导入", True, "全部可导入")
    except Exception as e:
        return CheckResult("核心模块导入", False, f"导入失败：{e}")


def check_sample_script() -> CheckResult:
    from pathlib import Path
    p = Path("examples/sample_script.txt")
    ok = p.exists()
    return CheckResult(
        "示例剧本", ok,
        "examples/sample_script.txt 存在" if ok else "缺少示例剧本",
    )


def main() -> int:
    print("=== AI 短剧工坊 · 环境自检 ===\n")
    checks = [
        check_python(),
        check_write_permission(),
        check_core_imports(),
        check_package("video_studio", "video_studio 包"),
        check_ffmpeg(),
        check_sample_script(),
    ]
    for c in checks:
        flag = "✓" if c.ok else ("✗" if c.required else "!")
        print(f"[{flag}] {c.name}: {c.detail}")

    required_failed = [c for c in checks if not c.ok and c.required]
    print("\n" + "=" * 50)
    if required_failed:
        print(f"自检未通过：{len(required_failed)} 项必需检查失败")
        return 1
    print("自检通过：核心功能可用；可选依赖（ffmpeg/外部算力）按需配置")
    return 0


if __name__ == "__main__":
    sys.exit(main())
