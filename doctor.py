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


def check_workbench() -> CheckResult:
    from pathlib import Path
    needed = ["web/index.html", "web/app.js", "web/style.css",
              "web/api_server.py", "start.py"]
    missing = [f for f in needed if not Path(f).exists()]
    ok = not missing
    return CheckResult(
        "Web 工作台", ok,
        "前端 + API 服务 + 启动脚本齐全" if ok else f"缺少文件：{', '.join(missing)}",
    )


def check_api_import() -> CheckResult:
    import os
    import tempfile
    os.environ.setdefault("VS_DATA_DIR", tempfile.mkdtemp())
    try:
        from web.api_server import ROUTES  # noqa: F401
        return CheckResult("API 服务可导入", True, f"{len(ROUTES)} 条路由就绪")
    except Exception as e:
        return CheckResult("API 服务可导入", False, f"导入失败：{e}")


def check_new_views() -> CheckResult:
    """校验新工作台信息架构的关键端点与前端视图标识是否就绪。"""
    import os
    import tempfile
    os.environ.setdefault("VS_DATA_DIR", tempfile.mkdtemp())
    expected = [
        "/api/episodes/tree", "/api/script/outline", "/api/preview/check",
        "/api/submit/batch", "/api/records/export", "/api/compose/records",
        "/api/video-config", "/api/experts", "/api/asset-stats",
    ]
    try:
        from web.api_server import ROUTES
        patterns = " ".join(p if isinstance(p, str) else p.pattern for _, p, _ in ROUTES)
        missing = [ep for ep in expected if ep not in patterns]
        ok = not missing
        return CheckResult(
            "工作台与配置端点", ok,
            "9 个生产流程端点齐全" if ok else f"缺少端点：{', '.join(missing)}",
        )
    except Exception as e:
        return CheckResult("工作台与配置端点", False, f"检查失败：{e}")


def check_eight_steps() -> CheckResult:
    """校验八步制作流程定义与每步功能点端点是否齐全。"""
    import os
    import tempfile
    os.environ.setdefault("VS_DATA_DIR", tempfile.mkdtemp())
    expected = [
        "/api/workflow/steps", "/api/projects/{id}/settings",
        "/api/script/generate-outline", "/api/script/import-book", "/api/script/rewrite",
        "/api/shots/split", "/api/shots/missing", "/api/shots/{id}/refs",
        "/api/candidates/generate", "/api/outputs/{id}/adopt",
        "/api/tasks/failures", "/api/tasks/{id}/retry", "/api/finalize/plan",
    ]
    try:
        from web.api_server import ROUTES
        from video_studio.creation import WORKBENCH_STEPS
        patterns = " ".join(p if isinstance(p, str) else p.pattern for _, p, _ in ROUTES)
        # 路由是正则，用关键片段匹配（忽略 {id} 占位）
        probes = [
            "/api/workflow/steps", "/api/projects/", "/api/script/generate-outline",
            "/api/script/import-book", "/api/script/rewrite", "/api/shots/split",
            "/api/shots/missing", "/api/shots/", "/api/candidates/generate",
            "/api/outputs/", "/api/tasks/failures", "/api/tasks/", "/api/finalize/plan",
        ]
        missing = [p for p in probes if p not in patterns]
        step_ok = len(WORKBENCH_STEPS) == 8 and all(len(s.points) >= 2 for s in WORKBENCH_STEPS)
        ok = not missing and step_ok
        detail = (f"8 步 / {sum(len(s.points) for s in WORKBENCH_STEPS)} 功能点齐全"
                  if ok else f"缺少端点：{', '.join(missing)}；步骤定义完整={step_ok}")
        return CheckResult("八步流程与功能点", ok, detail)
    except Exception as e:
        return CheckResult("八步流程与功能点", False, f"检查失败：{e}")


def check_compute_interfaces() -> CheckResult:
    """校验三种算力接口做实：默认桩、ComfyUI 全套、配置安全、注册表。"""
    try:
        from video_studio.compute import (
            CloudApiBackend, CloudGpuBackend, ComputeConfig, FakeHttpClient,
            GenRequest, LocalComfyUIBackend, default_registry,
        )
        from web.api_server import ROUTES

        # 1) 三种后端默认桩可跑通 submit → poll → fetch
        req = GenRequest(task_id="doc", step="video", payload={"prompt": "原创", "mode": "t2v"})
        chains_ok = True
        for b in [CloudApiBackend(), LocalComfyUIBackend(), CloudGpuBackend()]:
            try:
                tid = b.submit(req)
                for _ in range(8):
                    if b.poll(tid).state in {"succeeded", "failed"}:
                        break
                b.fetch(tid)
            except Exception:
                chains_ok = False

        # 2) ComfyUI 全套 HTTP 端点在真实协议模式下可注入验证
        fake = FakeHttpClient({
            ("GET", "/system_stats"): {"system": {}},
            ("GET", "/object_info"): {"A": {}},
            ("POST", "/prompt"): {"prompt_id": "p1"},
            ("GET", "/history/p1"): {"p1": {"status": {"status_str": "success", "completed": True},
                                             "outputs": {}}},
            ("GET", "/view"): b"v",
            ("GET", "/queue"): {"queue_running": [], "queue_pending": []},
            ("POST", "/interrupt"): {},
        })
        comfy = LocalComfyUIBackend(
            config=ComputeConfig(kind="local_comfyui", dry_run=False), client=fake)
        comfy_ok = (comfy.health() and comfy.capabilities().get("node_count") == 1
                    and comfy.submit(req) == "p1" and comfy.poll("p1").state == "succeeded"
                    and bool(comfy.queue() is not None))

        # 3) 注册表三后端 + 配置只存引用
        reg = default_registry()
        reg_ok = len(reg.available()) == 3 and reg.status()["active"] != ""

        # 4) 算力管理 API 端点齐全
        patterns = " ".join(p if isinstance(p, str) else p.pattern for _, p, _ in ROUTES)
        api_probes = ["/api/compute/configs", "/api/compute/switch",
                      "/api/compute/health", "/api/compute/simulate", "/api/comfyui/workflow"]
        api_missing = [p for p in api_probes if p not in patterns]

        ok = chains_ok and comfy_ok and reg_ok and not api_missing
        detail = (f"3 后端桩链路 + ComfyUI 6 接口 + 注册表 + {len(api_probes)} 算力 API 齐全"
                  if ok else f"链路={chains_ok} ComfyUI={comfy_ok} 注册表={reg_ok} "
                             f"缺端点={','.join(api_missing)}")
        return CheckResult("算力接口（桩/ComfyUI/云GPU）", ok, detail)
    except Exception as e:
        return CheckResult("算力接口（桩/ComfyUI/云GPU）", False, f"检查失败：{e}")


def main() -> int:
    print("=== AI 短剧工坊 · 环境自检 ===\n")
    checks = [
        check_python(),
        check_write_permission(),
        check_core_imports(),
        check_package("video_studio", "video_studio 包"),
        check_ffmpeg(),
        check_sample_script(),
        check_workbench(),
        check_api_import(),
        check_new_views(),
        check_eight_steps(),
        check_compute_interfaces(),
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
