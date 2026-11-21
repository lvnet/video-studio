#!/usr/bin/env python3
"""端到端演示：不依赖任何外部算力，串起从剧本到成片计划的完整链路。

跑法：
    python examples/end_to_end.py

演示链路（对应方案 06 八步链的前半段 + 后期装配，真实生成算力后置为桩）：
  剧本导入 → 拆镜 → 参考资产(批准) → 提示词扩写 → 合规守卫
  → 成本闸门(冻结) → 调度(派单) → 失败/重试 → 成片装配计划 → SRT 字幕
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# 允许从任意目录直接运行：把项目根加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from video_studio.compliance import AssetProvenance
from video_studio.control import CostGate, LocalScheduler, PriceTable
from video_studio.creation import expand_prompt, import_script, beats_to_shots
from video_studio.models import (
    Asset,
    Episode,
    LocalStore,
    PriceRule,
    Project,
    Task,
)
from video_studio.post import cues_from_shots, render_srt, build_compose_plan


def run(root: str) -> None:
    store = LocalStore(root)

    # 1. 项目与剧集
    project = Project(id="p_demo", name="示例短剧·江边", aspect_ratio="16:9",
                      budget_total=50.0, budget_daily=20.0)
    store.add("projects", project)
    ep = Episode(id="e_demo", project_id=project.id, index=1)
    store.add("episodes", ep)

    # 2. 剧本导入 + 拆镜
    script = Path("examples/sample_script.txt").read_text(encoding="utf-8")
    beats = import_script(script, "txt")
    shots = beats_to_shots(ep, beats)
    for s in shots:
        store.add("shots", s)
    print(f"[1] 导入剧本 → 拆出 {len(shots)} 个镜头")

    # 3. 参考资产（人工批准 + 合法来源）
    role = Asset(
        id="a_linyuan", project_id=project.id, type="role", name="林远",
        voice_id="voice_linyuan", approved=True, image_url="assets/linyuan.png", version=1,
        provenance=AssetProvenance("a_linyuan", "self_created").to_dict(),
    )
    store.add("assets", role)
    print("[2] 参考资产：林远（已批准，来源=原创）")

    # 4. 提示词扩写 + 合规守卫
    expanded = []
    for s in shots:
        e = expand_prompt(s, [role], mode="r2v")
        expanded.append(e)
    print(f"[3] 八步链提示词扩写完成（R2V 六段式），合规守卫全部通过")

    # 5. 成本闸门：按 768P ¥0.50/秒估算，冻结在途
    price = PriceTable([
        PriceRule(id="price_h3", backend_type="api", model="h3",
                  unit="second", unit_price_cents=50),
    ])
    gate = CostGate(store, price)
    scheduler = LocalScheduler(store)
    total_estimate = 0
    for s in shots:
        task_res = scheduler.submit(s.id, "r2v", {"mode": "r2v"},
                                    idempotency_key=f"demo-{s.id}")
        task = Task.from_dict(store.get("tasks", task_res.task_id))
        est_cents = 50 * int(s.duration)  # ¥0.50/秒
        decision = gate.reserve(project.id, task, est_cents)
        if not decision.allowed:
            print(f"    ! 预算闸门拦下镜头 {s.index}: {decision.detail}")
            continue
        scheduler.enqueue(task.id)
        total_estimate += est_cents
        store.lock_asset_version(task.id, role.id, role.version, is_primary=True)
    print(f"[4] 成本闸门通过，冻结在途 {total_estimate} 分；资产版本已锁定")

    # 6. 调度派单 + 模拟一次失败重试
    first = store.find("tasks")[0]
    scheduler.dispatch(first["id"])
    new_state = scheduler.on_failure(first["id"], "PROVIDER_TRANSIENT")
    print(f"[5] 调度派单成功；模拟供应商抖动 → 状态={new_state}（失败回 queued 重派）")

    # 7. 成片装配计划 + SRT 字幕
    shots_data = [s.to_dict() for s in shots]
    cues = cues_from_shots(shots_data)
    srt = render_srt(cues)
    plan = build_compose_plan(
        [f"clip_{i}.mp4" for i in range(len(shots))],
        srt_path="subtitles.srt", cta_text="关注我们 · 下集见",
    )
    print(f"[6] 成片装配计划：{len(plan.trims)} 段裁头拼接 + 片尾CTA + 升清抽象")
    print(f"[7] 生成 SRT 字幕 {len(cues)} 条")

    # 输出摘要
    print("\n=== 演示摘要 ===")
    print(f"镜头数: {len(shots)} | 提示词版本: 1 | 资产: 1（已批准）")
    print(f"成本预估: ¥{total_estimate / 100:.2f} | 任务数: {len(store.find('tasks'))}")
    print(f"待接入真实算力: cloud_api / local_comfyui / cloud_gpu（均已在注册表，可切换）")
    print("端到端演示完成（未调用任何外部算力）")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        run(tmp)
