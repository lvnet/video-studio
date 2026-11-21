"""ComfyUI prompt graph 构建器。

设计原则：
- 不硬编码任何具体模型权重或下载逻辑：节点类型仅作默认模板，用户部署的
  ComfyUI 装了什么节点就以 ``payload['workflow']`` 覆盖，零版权风险。
- 支持 t2v / i2v / r2v 三种生成模式，统一输出 ComfyUI 的 prompt dict
  （形如 ``{"1": {"class_type": ..., "inputs": {...}}}``）。
- 纯函数，便于单测：不做网络、不读文件。
"""

from __future__ import annotations

from typing import Any


def build_workflow(payload: dict[str, Any]) -> dict[str, Any]:
    """从 GenRequest.payload 构建 ComfyUI prompt graph。

    payload 约定：
    - workflow: 若提供，直接原样返回（用户自带节点图，最高优先）
    - mode: t2v / i2v / r2v
    - prompt / positive: 正向提示词
    - negative: 负向提示词
    - image / reference_images: 参考图文件名（i2v/r2v）
    - seed, steps, cfg, fps, frames, width, height: 生成参数
    - save_prefix: 输出文件前缀
    """
    if isinstance(payload.get("workflow"), dict):
        return payload["workflow"]

    mode = (payload.get("mode") or "t2v").lower()
    positive = payload.get("prompt") or payload.get("positive") or ""
    negative = payload.get("negative") or ""
    seed = int(payload.get("seed", 0))
    steps = int(payload.get("steps", 20))
    cfg = float(payload.get("cfg", 6.0))
    fps = int(payload.get("fps", 24))
    frames = int(payload.get("frames", 72))
    width = int(payload.get("width", 768))
    height = int(payload.get("height", 432))
    prefix = payload.get("save_prefix") or "vs"

    graph: dict[str, Any] = {
        "1": {"class_type": "CLIPTextEncode",
              "inputs": {"text": positive}},
        "2": {"class_type": "CLIPTextEncode",
              "inputs": {"text": negative}},
        "3": {"class_type": "EmptyLatentImage",
              "inputs": {"width": width, "height": height, "batch_size": 1}},
    }

    # 参考图节点（i2v/r2v）：只引用文件名，不复制图片内容
    ref = payload.get("image") or payload.get("reference_images")
    if mode in {"i2v", "r2v"} and ref:
        names = ref if isinstance(ref, list) else [ref]
        graph["4"] = {"class_type": "LoadImage",
                      "inputs": {"image": names[0]}}

    graph["10"] = {
        "class_type": "VSVideoGenerate",
        "inputs": {
            "positive": ["1", 0],
            "negative": ["2", 0],
            "latent": ["3", 0],
            "mode": mode,
            "seed": seed,
            "steps": steps,
            "cfg": cfg,
            "fps": fps,
            "frames": frames,
        },
    }
    if "4" in graph:
        graph["10"]["inputs"]["reference_image"] = ["4", 0]

    graph["20"] = {
        "class_type": "SaveVideo",
        "inputs": {"video": ["10", 0], "filename_prefix": prefix},
    }
    return graph


def outputs_from_history(history_entry: dict[str, Any]) -> list[dict[str, Any]]:
    """从 ComfyUI /history 输出里抽取产物文件描述。

    兼容常见 SaveVideo / SaveImage 输出 ``gifs``、``images`` 两类。
    返回 [{filename, subfolder, type}]。
    """
    out: list[dict[str, Any]] = []
    for node in (history_entry.get("outputs") or {}).values():
        for key in ("gifs", "images", "videos"):
            for item in node.get(key, []) or []:
                if isinstance(item, dict) and item.get("filename"):
                    out.append({
                        "filename": item["filename"],
                        "subfolder": item.get("subfolder", ""),
                        "type": item.get("type", "output"),
                    })
    return out
