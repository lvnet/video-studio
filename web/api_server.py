"""工作台 HTTP API 服务层（零第三方依赖，仅标准库）。

把 video_studio 核心库的能力暴露成 RESTful API，供本地单页工作台调用。
设计原则：
- 零外部依赖：http.server + JSON，clone 即可运行。
- 单一数据目录：由环境变量 VS_DATA_DIR 指定，默认 <项目根>/workspace_data。
- 不接真实算力：任务提交、调度均为本地桩，成本控制面完整生效。
- 合规前置：提示词扩写、资产批准都经过守卫并写审计日志。
- 错误统一：所有响应为 JSON，异常映射为清晰的状态码与中文消息。

路由表在 ROUTES 中集中声明；新增端点只需加一条 (method, pattern, handler)。
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sys
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

# 允许从任意位置以脚本方式运行
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from video_studio.compliance import (  # noqa: E402
    AssetProvenance,
    ComplianceBlocked,
    RiskLevel,
    check_asset_provenance,
    check_prompt,
    screen_content,
)
from video_studio.compliance.audit import AuditLog  # noqa: E402
from video_studio.compute import BackendKind, default_registry  # noqa: E402
from video_studio.control import (  # noqa: E402
    CostGate,
    LocalScheduler,
    PriceTable,
    TaskState,
    diagnose,
    failure_spec,
)
from video_studio.creation import (  # noqa: E402
    PIPELINE,
    STEP_SPECS,
    WORKBENCH_STEPS,
    beats_to_shots,
    expand_prompt,
    import_script,
    missing_fields_report,
    steps_as_dicts,
)
from video_studio.creation.llm import get_provider  # noqa: E402
from video_studio.models import (  # noqa: E402
    Asset,
    ComposeRecord,
    Episode,
    Expert,
    LocalStore,
    Output,
    PriceRule,
    Project,
    ScriptVersion,
    Shot,
    Task,
    VideoConfig,
    new_id,
    utcnow,
)
from video_studio.post import (  # noqa: E402
    build_compose_plan,
    cues_from_shots,
    render_srt,
    upscale_strategy,
)

DATA_DIR = Path(os.environ.get("VS_DATA_DIR", str(ROOT / "workspace_data")))
STATIC_DIR = Path(__file__).resolve().parent

# 默认单价表（演示口径，可在 UI 查看；真实计价后置）
DEFAULT_PRICE_RULES: list[PriceRule] = [
    PriceRule(id="price_h3_768p", backend_type="api", model="h3",
              unit="second", unit_price_cents=50),
]


class AppState:
    """进程级共享状态：store / scheduler / 审计日志 / 成本闸门。"""

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.store = LocalStore(data_dir)
        self.scheduler = LocalScheduler(self.store)
        self.price_table = PriceTable(DEFAULT_PRICE_RULES)
        self.gate = CostGate(self.store, self.price_table)
        self.audit_log = AuditLog(data_dir / "audit.jsonl")
        # 算力注册表：默认桩，接口一致可切换；不存任何密钥明文
        self.registry = default_registry()


STATE: AppState | None = None


def get_state() -> AppState:
    global STATE
    if STATE is None:
        STATE = AppState(DATA_DIR)
    return STATE


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(message)


def _body_bytes(handler: "Handler") -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or 0)
    if length == 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ApiError(400, "请求体不是合法 JSON")
    if not isinstance(data, dict):
        raise ApiError(400, "请求体必须是 JSON 对象")
    return data


def _require(body: dict, *keys: str) -> None:
    missing = [k for k in keys if not body.get(k)]
    if missing:
        raise ApiError(400, f"缺少必填字段：{', '.join(missing)}")


def _audit(state: AppState, event_type: str, subject: str, report: Any,
           context: dict | None = None) -> None:
    try:
        state.audit_log.append(event_type, subject, report, context)
    except Exception:
        # 审计写失败不应阻断主流程，但会在响应中不留痕——单机边界内可接受
        pass


# ---------------------------------------------------------------------------
# 各端点处理函数：签名 (handler, match, body, query) -> dict
# ---------------------------------------------------------------------------

def h_health(handler, m, body, q):
    return {"ok": True, "service": "video-studio workbench", "data_dir": str(DATA_DIR)}


def h_pipeline(handler, m, body, q):
    return {
        "steps": [s.value for s in PIPELINE],
        "specs": [
            {"step": sp.step.value, "name": sp.name,
             "preferred_backend": sp.preferred_backend}
            for sp in STEP_SPECS
        ],
    }


def h_backends(handler, m, body, q):
    """算力后端全景：能力 / 健康 / 配置 / 激活状态（密钥不明文）。"""
    status = get_state().registry.status()
    status["note"] = (
        "三种后端接口已做实；默认内存桩、零外连、零成本。"
        "要接真实算力须设 VS_COMPUTE_DRY_RUN=0 并配置端点与密钥引用。"
    )
    return status


def h_compute_configs(handler, m, body, q):
    """算力配置安全视图：只含 credentials_ref 引用名，绝不含密钥明文。"""
    return {"configs": get_state().registry.configs()}


def h_compute_switch(handler, m, body, q):
    """切换当前激活后端（上层剧本/提示词/项目数据不变）。"""
    _require(body, "kind")
    try:
        backend = get_state().registry.set_active(body["kind"])
    except (KeyError, ValueError) as e:
        raise ApiError(400, f"未知后端：{body['kind']}") from e
    _audit(get_state(), "compute.switch", backend.kind.value, {"kind": backend.kind.value})
    return {"active": body["kind"], "capabilities": backend.capabilities()}


def h_compute_health(handler, m, body, q):
    """对指定后端做健康检查（桩模式恒为就绪，不发真实请求）。"""
    kind = q.get("kind", [None])[0] or body.get("kind")
    if not kind:
        raise ApiError(400, "缺少 kind")
    try:
        backend = get_state().registry.get(kind)
    except (KeyError, ValueError) as e:
        raise ApiError(400, f"未知后端：{kind}") from e
    return {"kind": kind, "healthy": backend.health(),
            "capabilities": backend.capabilities()}


def h_comfyui_workflow(handler, m, body, q):
    """构建 ComfyUI prompt graph（纯函数预览，不提交、不外连）。"""
    from video_studio.compute import build_workflow
    payload = body.get("payload", body)
    return {"workflow": build_workflow(payload)}


def h_compute_simulate(handler, m, body, q):
    """用桩后端跑一次完整生成链路（submit→poll→fetch），验证接口可用。

    仅使用内存桩，不调用任何真实 API，零成本。用于工作台「接口自检」。
    """
    from video_studio.compute import CloudApiBackend, CloudGpuBackend, GenRequest, LocalComfyUIBackend
    kind = body.get("kind", "local_comfyui")
    makers = {
        "local_comfyui": LocalComfyUIBackend,
        "cloud_api": CloudApiBackend,
        "cloud_gpu": CloudGpuBackend,
    }
    if kind not in makers:
        raise ApiError(400, f"未知后端：{kind}")
    backend = makers[kind]()  # 默认桩
    req = GenRequest(task_id="sim-" + new_id("x")[:8], step="video",
                     payload={"prompt": "接口自检原创样例", "mode": body.get("mode", "t2v"),
                              "frames": 24, "resolution": "768P"})
    tid = backend.submit(req)
    states = []
    for _ in range(8):
        p = backend.poll(tid)
        states.append(p.state)
        if p.state in {"succeeded", "failed"}:
            break
    artifact = backend.fetch(tid) if states[-1] == "succeeded" else None
    return {
        "kind": kind, "task_id": tid, "states": states,
        "artifact": {"storage_uri": artifact.storage_uri, "sha256": artifact.sha256,
                     "size_bytes": artifact.size_bytes} if artifact else None,
        "note": "内存桩自检，未调用真实算力，零成本。",
    }


def h_projects_list(handler, m, body, q):
    return {"projects": get_state().store.all("projects")}


def h_projects_create(handler, m, body, q):
    _require(body, "name")
    state = get_state()
    p = Project(
        id=new_id("proj"), name=body["name"],
        aspect_ratio=body.get("aspect_ratio", "9:16"),
        style_preset=body.get("style_preset", "realistic_3d"),
        budget_total=float(body.get("budget_total", 0) or 0),
        budget_daily=float(body.get("budget_daily", 0) or 0),
    )
    state.store.add("projects", p)
    return {"project": p.to_dict()}


def h_projects_get(handler, m, body, q):
    pid = m.group(1)
    state = get_state()
    proj = state.store.get("projects", pid)
    eps = state.store.find("episodes", project_id=pid)
    assets = state.store.find("assets", project_id=pid)
    return {"project": proj, "episodes": eps, "assets": assets}


def h_episodes_list(handler, m, body, q):
    pid = q.get("project_id", [None])[0]
    state = get_state()
    eps = state.store.find("episodes", project_id=pid) if pid else state.store.all("episodes")
    return {"episodes": eps}


def h_episodes_create(handler, m, body, q):
    _require(body, "project_id", "index")
    state = get_state()
    ep = Episode(
        id=new_id("ep"), project_id=body["project_id"], index=int(body["index"]),
        title=body.get("title", ""), summary=body.get("summary", ""),
        duration_target=float(body.get("duration_target", 60) or 60),
    )
    state.store.add("episodes", ep)
    return {"episode": ep.to_dict()}


def _cascade_delete_episode(state, eid: str) -> int:
    """删除单集及其全部下游数据，避免留下孤儿记录。返回删除的关联记录数。

    注意：审计日志保留（合规要求可追溯），不随剧集删除。
    """
    removed = 0
    shot_ids = {s["id"] for s in state.store.find("shots", episode_id=eid)}
    task_ids = {t["id"] for t in state.store.all("tasks") if t.get("shot_id") in shot_ids}

    def _purge(name: str, predicate) -> None:
        nonlocal removed
        for rec in list(state.store.all(name)):
            if predicate(rec):
                state.store.delete(name, rec["id"])
                removed += 1

    _purge("outputs", lambda r: r.get("task_id") in task_ids or r.get("shot_id") in shot_ids)
    _purge("tasks", lambda r: r.get("id") in task_ids or r.get("shot_id") in shot_ids)
    _purge("prompts", lambda r: r.get("shot_id") in shot_ids or r.get("episode_id") == eid)
    _purge("shots", lambda r: r.get("episode_id") == eid)
    _purge("script_versions", lambda r: r.get("episode_id") == eid)
    return removed


def h_episode_update(handler, m, body, q):
    """修改单集：集序号 / 标题 / 目标时长 / 状态。集序号冲突时与目标集自动交换。"""
    eid = m.group(1)
    state = get_state()
    ep = state.store.get("episodes", eid)
    changes: dict = {}
    if "title" in body:
        changes["title"] = str(body["title"]).strip()
    if "duration_target" in body:
        changes["duration_target"] = float(body["duration_target"] or 60)
    if "status" in body:
        changes["status"] = str(body["status"]).strip()
    if "index" in body:
        new_index = int(body["index"] or 0)
        if new_index < 1:
            raise ApiError(400, "集序号至少为 1")
        old_index = int(ep.get("index", 0))
        if new_index != old_index:
            for other in state.store.find("episodes", project_id=ep["project_id"]):
                if other["id"] != eid and int(other.get("index", 0)) == new_index:
                    state.store.update("episodes", other["id"], index=old_index)
                    break
            changes["index"] = new_index
    if changes:
        state.store.update("episodes", eid, **changes)
    return {"episode": state.store.get("episodes", eid),
            "episodes": state.store.find("episodes", project_id=ep["project_id"])}


def h_episode_delete(handler, m, body, q):
    """删除单集并级联清理其镜头/提示词/任务/输出/剧本版本。"""
    eid = m.group(1)
    state = get_state()
    ep = state.store.get("episodes", eid)
    pid = ep["project_id"]
    removed = _cascade_delete_episode(state, eid)
    state.store.delete("episodes", eid)
    return {"deleted": eid, "removed_related": removed,
            "episodes": state.store.find("episodes", project_id=pid)}


def h_script_import(handler, m, body, q):
    """导入剧本 → 自动拆镜，落库并返回镜头 + 缺字段报告。"""
    _require(body, "episode_id", "text")
    state = get_state()
    fmt = body.get("format", "txt")
    try:
        beats = import_script(body["text"], fmt)
    except ValueError as e:
        raise ApiError(400, str(e))
    ep_data = state.store.get("episodes", body["episode_id"])
    ep = Episode.from_dict(ep_data)
    ep.script_text = body["text"]
    state.store.update("episodes", ep.id, script_text=body["text"])
    shots = beats_to_shots(ep, beats, duration=float(body.get("duration", 5) or 5))
    for s in shots:
        state.store.add("shots", s)
    report = missing_fields_report(shots)
    return {
        "beats": len(beats),
        "shots": [s.to_dict() for s in shots],
        "missing_fields": report,
    }


def h_shots_list(handler, m, body, q):
    eid = q.get("episode_id", [None])[0]
    state = get_state()
    shots = state.store.find("shots", episode_id=eid) if eid else state.store.all("shots")
    shots = sorted(shots, key=lambda s: s.get("index", 0))
    tasks = state.store.find("tasks")
    task_by_shot = {t["shot_id"]: t for t in tasks}
    # 用副本，避免污染 store 缓存（all/find 返回的是内部 dict 的引用）
    result = []
    for s in shots:
        item = dict(s)
        t = task_by_shot.get(s["id"])
        item["task_status"] = t["status"] if t else ""
        result.append(item)
    return {"shots": result}


def h_assets_list(handler, m, body, q):
    pid = q.get("project_id", [None])[0]
    state = get_state()
    assets = state.store.find("assets", project_id=pid) if pid else state.store.all("assets")
    return {"assets": assets}


def h_assets_create(handler, m, body, q):
    _require(body, "project_id", "name", "type")
    state = get_state()
    source = body.get("source", "self_created")
    prov = AssetProvenance(asset_id="", source=source,
                           rights_holder=body.get("rights_holder", ""),
                           license_ref=body.get("license_ref", ""))
    asset = Asset(
        id=new_id("asset"), project_id=body["project_id"], type=body["type"],
        name=body["name"], voice_id=body.get("voice_id", ""),
        image_url=body.get("image_url", ""), approved=False,
        appearance_json=body.get("appearance_json", {}),
        provenance=prov.to_dict(),
    )
    state.store.add("assets", asset)
    return {"asset": asset.to_dict()}


def h_assets_approve(handler, m, body, q):
    """资产批准硬门槛：来源合规检查通过才可批准。"""
    aid = m.group(1)
    state = get_state()
    data = state.store.get("assets", aid)
    prov = AssetProvenance.from_dict(data["provenance"]) if data.get("provenance") else None
    report = check_asset_provenance(prov)
    _audit(state, "asset_check", aid, report, {"action": "approve"})
    if not report.passed:
        raise ApiError(403, "资产来源不合规：" + "；".join(f.message for f in report.findings))
    state.store.update("assets", aid, approved=True)
    return {"asset": state.store.get("assets", aid), "audit": "已记录"}


def h_prompts_expand(handler, m, body, q):
    """镜头 → 权威 H3 提示词扩写（三段式/六段式），合规守卫前置。"""
    _require(body, "shot_id")
    state = get_state()
    mode = body.get("mode", "t2v")
    shot_data = state.store.get("shots", body["shot_id"])
    from video_studio.models import Shot
    shot = Shot.from_dict(shot_data)
    assets = [Asset.from_dict(a) for a in state.store.find("assets", project_id="")]
    # 按镜头所属剧集 → 项目筛选资产；简化为全部已批准资产
    assets = [Asset.from_dict(a) for a in state.store.all("assets") if a.get("approved")]
    try:
        expanded = expand_prompt(shot, assets, mode=mode)
    except ComplianceBlocked as e:
        report = check_prompt(shot.action or shot.dialogue or shot.prompt_text,
                              subject=shot.id)
        _audit(state, "prompt_check", shot.id, report, {"mode": mode, "blocked": True})
        raise ApiError(422, f"合规守卫拦截：{e}")
    report = check_prompt(shot.action or shot.dialogue or shot.prompt_text, subject=shot.id)
    _audit(state, "prompt_check", shot.id, report, {"mode": mode})
    # 保存提示词版本链
    from video_studio.models import Prompt
    existing = state.store.find("prompts", shot_id=shot.id)
    prompt = Prompt(
        id=new_id("prompt"), shot_id=shot.id, version=len(existing) + 1,
        content=expanded.to_submission(),
        parent_id=existing[-1]["id"] if existing else "",
    )
    state.store.add("prompts", prompt)
    return {
        "expanded": asdict(expanded),
        "prompt_version": prompt.to_dict(),
        "compliance": {"passed": report.passed, "level": report.level.value},
    }


def h_prompts_list(handler, m, body, q):
    sid = q.get("shot_id", [None])[0]
    state = get_state()
    ps = state.store.find("prompts", shot_id=sid) if sid else state.store.all("prompts")
    return {"prompts": ps}


def h_compliance_check(handler, m, body, q):
    """通用文本合规检查：版权守卫 + 内容安全过滤，并写审计。"""
    _require(body, "text")
    state = get_state()
    subject = body.get("subject", "manual")
    guard = check_prompt(body["text"], subject=subject)
    safety = screen_content(body["text"], subject=subject)
    _audit(state, "content_check", subject, safety, {"manual": True})
    return {
        "guard": {"passed": guard.passed, "level": guard.level.value,
                  "findings": [asdict(f) for f in guard.findings]},
        "safety": {"passed": safety.passed, "level": safety.level.value,
                   "findings": [asdict(f) for f in safety.findings]},
        "passed": guard.passed and safety.passed,
    }


def h_audit_list(handler, m, body, q):
    state = get_state()
    subject = q.get("subject", [None])[0]
    etype = q.get("event_type", [None])[0]
    events = state.audit_log.query(subject=subject, event_type=etype)
    return {
        "events": [asdict(e) for e in events],
    }


def h_audit_verify(handler, m, body, q):
    ok, msg = get_state().audit_log.verify_integrity()
    return {"intact": ok, "detail": msg}


def h_cost_overview(handler, m, body, q):
    pid = q.get("project_id", [None])[0]
    state = get_state()
    if not pid:
        projects = state.store.all("projects")
        pid = projects[0]["id"] if projects else ""
    if not pid:
        return {"project_id": "", "ledger": None, "tasks": []}
    ledger = state.store.ledger(pid)
    tasks = [
        t for t in state.store.find("tasks")
        if t.get("shot_id") in {s["id"] for s in _shots_of_project(state, pid)}
    ]
    return {"project_id": pid, "ledger": asdict(ledger), "tasks": tasks,
            "price_rules": [asdict(r) for r in DEFAULT_PRICE_RULES]}


def _shots_of_project(state: AppState, project_id: str) -> list[dict]:
    ep_ids = {e["id"] for e in state.store.find("episodes", project_id=project_id)}
    return [s for s in state.store.all("shots") if s.get("episode_id") in ep_ids]


def h_cost_check(handler, m, body, q):
    _require(body, "project_id", "estimate_cents")
    state = get_state()
    d = state.gate.check(body["project_id"], int(body["estimate_cents"]))
    return asdict(d)


def h_tasks_list(handler, m, body, q):
    state = get_state()
    tasks = state.store.all("tasks")
    return {"tasks": tasks}


def h_tasks_submit(handler, m, body, q):
    """提交任务：合规检查 → 成本闸门冻结 → 入队（真实算力后置）。"""
    _require(body, "shot_id", "project_id")
    state = get_state()
    shot = state.store.get("shots", body["shot_id"])
    mode = body.get("mode", "t2v")
    # 合规前置
    report = check_prompt(shot.get("action") or shot.get("dialogue") or shot.get("prompt_text", ""),
                          subject=shot["id"])
    _audit(state, "prompt_check", shot["id"], report, {"mode": mode, "on_submit": True})
    if not report.passed:
        raise ApiError(422, "合规守卫拦截，禁止提交：" + "；".join(f.message for f in report.findings))
    est = int(body.get("estimate_cents", 0))
    res = state.scheduler.submit(shot["id"], mode, {"mode": mode},
                                  idempotency_key=body.get("idempotency_key", ""),
                                  mode=mode, backend_id=body.get("backend_id", ""))
    task = state.store.get("tasks", res.task_id)
    from video_studio.models import Task
    t = Task.from_dict(task)
    if est > 0:
        decision = state.gate.reserve(body["project_id"], t, est)
        if not decision.allowed:
            raise ApiError(422, "成本闸门拦截：" + decision.detail)
        task = state.store.get("tasks", res.task_id)
    if res.reason != "idempotent_hit":
        state.scheduler.enqueue(res.task_id)
        task = state.store.get("tasks", res.task_id)
    return {"task": task, "idempotent": res.reason == "idempotent_hit"}


def h_tasks_dispatch(handler, m, body, q):
    tid = m.group(1)
    state = get_state()
    res = state.scheduler.dispatch(tid)
    return asdict(res)


def h_tasks_fail(handler, m, body, q):
    """模拟失败：验证失败码归类与重试/死信逻辑。"""
    tid = m.group(1)
    state = get_state()
    code = body.get("failure_code", "PROVIDER_TRANSIENT")
    new_state = state.scheduler.on_failure(tid, code)
    return {"task": state.store.get("tasks", tid), "new_state": new_state,
            "diagnosis": diagnose(code), "spec": asdict(failure_spec(code))}


def h_failure_codes(handler, m, body, q):
    from video_studio.control.failure import FAILURE_SPECS
    return {"codes": {k: asdict(v) for k, v in FAILURE_SPECS.items()}}


def h_compose_plan(handler, m, body, q):
    """生成成片装配计划 + SRT 字幕（纯逻辑，不执行 ffmpeg）。"""
    _require(body, "shots")
    shots = body["shots"]
    state = get_state()
    cues = cues_from_shots(shots)
    srt = render_srt(cues)
    clips = [s.get("output_url") or f"clip_{s.get('index', i)}.mp4"
             for i, s in enumerate(shots)]
    plan = build_compose_plan(clips, srt_path="subtitles.srt",
                              cta_text=body.get("cta_text", "关注我们 · 下集见"))
    return {"cues": [asdict(c) for c in cues], "srt": srt, "plan": asdict(plan)}


# --- 剧本 / 分集管理增强 ---
def h_episodes_tree(handler, m, body, q):
    """分集目录：每集带镜头数、提示词数、制作设置，供左侧剧集树展示。"""
    pid = q.get("project_id", [None])[0]
    state = get_state()
    eps = state.store.find("episodes", project_id=pid) if pid else state.store.all("episodes")
    eps = sorted(eps, key=lambda e: e.get("index", 0))
    tree = []
    for e in eps:
        shots = state.store.find("shots", episode_id=e["id"])
        prompt_ids = {s["id"] for s in shots}
        prompts = [p for p in state.store.all("prompts") if p.get("shot_id") in prompt_ids]
        approved = sum(1 for p in prompts if p.get("approved"))
        tree.append({
            **e,
            "shot_count": len(shots),
            "prompt_count": len(prompts),
            "approved_count": approved,
            "status": "已拆镜" if shots else ("已导入" if e.get("script_text") else "待制作"),
        })
    return {"episodes": tree}


def h_script_outline(handler, m, body, q):
    """改写剧本：按镜头聚合成「分段」结构（段/动作/对白/情绪），供改写剧本视图。

    这是把已拆镜的镜头按每 N 个镜头聚成一段的展示视角，纯读取+聚合，不改数据。
    """
    eid = q.get("episode_id", [None])[0]
    per_seg = int(q.get("per_segment", [3])[0] or 3)
    state = get_state()
    shots = sorted(state.store.find("shots", episode_id=eid), key=lambda s: s.get("index", 0)) if eid else []
    segments = []
    for i in range(0, len(shots), per_seg):
        group = shots[i:i + per_seg]
        segments.append({
            "index": len(segments) + 1,
            "location": group[0].get("location", ""),
            "duration": round(sum(float(s.get("duration", 5)) for s in group), 1),
            "action": "；".join(s.get("action", "") for s in group if s.get("action")),
            "dialogue": "；".join(s.get("dialogue", "") for s in group if s.get("dialogue")),
            "emotion": "—",
            "shot_ids": [s["id"] for s in group],
        })
    outline = [
        {"key": "需求定位", "value": "原创短剧，科幻悬疑题材"},
        {"key": "目标受众", "value": "泛娱乐短剧观众"},
        {"key": "核心驱动力", "value": "信息差与身份反转"},
        {"key": "故事类型", "value": "科幻 / 悬疑"},
        {"key": "核心架构", "value": "单集悬念 + 集尾钩子"},
        {"key": "故事主题", "value": "记忆、身份与选择"},
        {"key": "故事走向", "value": "层层递进，结尾留钩子"},
    ]
    return {"segments": segments, "outline": outline, "total_shots": len(shots)}


# --- 提示词批量 / 版本 ---
def h_prompts_approve(handler, m, body, q):
    """批准/评分提示词版本：版本链只保留一个 approved，并记录评分。"""
    _require(body, "prompt_id")
    pid = body["prompt_id"]
    state = get_state()
    target = state.store.get("prompts", pid)
    # 同镜头其他版本取消 approved
    for p in state.store.find("prompts", shot_id=target["shot_id"]):
        if p["id"] != pid and p.get("approved"):
            state.store.update("prompts", p["id"], approved=False)
    changes = {"approved": True}
    if "score" in body:
        changes["score"] = float(body["score"])
    state.store.update("prompts", pid, **changes)
    report = check_prompt(target.get("content", ""), subject=pid)
    _audit(state, "prompt_approve", pid, report, {"score": body.get("score")})
    return {"prompt": state.store.get("prompts", pid)}


def h_prompts_batch(handler, m, body, q):
    """批量扩写：把一个剧集下所有镜头按给定模式扩写成 H3 提示词（合规前置）。"""
    _require(body, "episode_id")
    eid = body["episode_id"]
    mode = body.get("mode", "r2v")
    state = get_state()
    shots = sorted(state.store.find("shots", episode_id=eid), key=lambda s: s.get("index", 0))
    from video_studio.models import Prompt, Shot
    assets = [Asset.from_dict(a) for a in state.store.all("assets") if a.get("approved")]
    done, blocked = [], []
    for s in shots:
        shot = Shot.from_dict(s)
        existing = state.store.find("prompts", shot_id=shot.id)
        try:
            expanded = expand_prompt(shot, assets, mode=mode)
        except ComplianceBlocked:
            blocked.append(shot.id)
            continue
        prompt = Prompt(
            id=new_id("prompt"), shot_id=shot.id, version=len(existing) + 1,
            content=expanded.to_submission(),
            parent_id=existing[-1]["id"] if existing else "",
        )
        state.store.add("prompts", prompt)
        done.append(prompt.to_dict())
    return {"generated": len(done), "blocked": blocked, "prompts": done}


# --- 预览确认：连线检查 ---
def h_preview_check(handler, m, body, q):
    """连线检查：逐镜头核对名称/时长/参考图/提示词完整性，缺失高亮。"""
    eid = q.get("episode_id", [None])[0]
    state = get_state()
    shots = sorted(state.store.find("shots", episode_id=eid), key=lambda s: s.get("index", 0)) if eid else []
    prompts = {p["shot_id"]: p for p in state.store.all("prompts")}
    assets = {a["id"]: a for a in state.store.all("assets")}
    rows = []
    for s in shots:
        p = prompts.get(s["id"])
        ref_ids = s.get("asset_refs", [])
        refs = [assets[a] for a in ref_ids if a in assets]
        missing = []
        if not (s.get("location") or s.get("action")):
            missing.append("名称/内容")
        if not s.get("duration"):
            missing.append("时长")
        if not refs:
            missing.append("参考图")
        if not p:
            missing.append("提示词")
        rows.append({
            "shot_id": s["id"], "index": s.get("index"),
            "location": s.get("location", ""), "duration": s.get("duration", 5),
            "mode": "R2V 语义参考",
            "refs": [{"id": a["id"], "name": a.get("name", ""),
                      "image_url": a.get("image_url", "")} for a in refs],
            "ref_count": len(refs),
            "prompt_ready": bool(p),
            "complete": not missing,
            "missing": missing,
        })
    complete = sum(1 for r in rows if r["complete"])
    return {"rows": rows, "complete": complete, "total": len(rows),
            "all_ready": len(rows) > 0 and complete == len(rows)}


# --- 提交生成（批量，桩） ---
def h_submit_batch(handler, m, body, q):
    """批量提交：链式锁 / 连接审核 / 成片模式，全部走本地桩与合规+成本闸门。"""
    _require(body, "episode_id", "project_id")
    eid, pid = body["episode_id"], body["project_id"]
    state = get_state()
    shots = sorted(state.store.find("shots", episode_id=eid), key=lambda s: s.get("index", 0))
    mode = body.get("mode", "r2v")
    steps = int(body.get("steps", 20))
    resolution = body.get("resolution", "768p")
    chain_lock = bool(body.get("chain_lock", True))
    connect_review = bool(body.get("connect_review", False))
    est = int(body.get("estimate_cents", 50))
    results = []
    prev_task_id = ""
    for s in shots:
        report = check_prompt(s.get("action") or s.get("dialogue") or s.get("prompt_text", ""),
                              subject=s["id"])
        if not report.passed:
            results.append({"shot_index": s.get("index"), "status": "blocked",
                            "reason": "合规拦截"})
            continue
        from video_studio.models import Task
        # 链式锁：上段未完成时记录依赖但不阻断（桩内全部立即完成）
        dependency = prev_task_id if chain_lock else ""
        res = state.scheduler.submit(
            s["id"], mode,
            {"mode": mode, "steps": steps, "resolution": resolution,
             "dependency": dependency, "connect_review": connect_review},
            mode=mode,
        )
        t = state.store.get("tasks", res.task_id)
        task = Task.from_dict(t)
        if est > 0:
            decision = state.gate.reserve(pid, task, est)
            if not decision.allowed:
                results.append({"shot_index": s.get("index"), "status": "blocked",
                                "reason": "成本闸门拦截"})
                continue
        state.scheduler.enqueue(res.task_id)
        # 桩：直接派单并标记已完成，产出采用版
        state.scheduler.dispatch(res.task_id)
        state.store.update("tasks", res.task_id, status="succeeded")
        from video_studio.models import Output
        out = Output(id=new_id("out"), task_id=res.task_id,
                     url=f"outputs/{res.task_id}.mp4", duration=float(s.get("duration", 5)),
                     resolution=resolution, version=1)
        state.store.add("outputs", out)
        state.store.adopt_output(out.id)
        prev_task_id = res.task_id
        results.append({"shot_index": s.get("index"), "status": "succeeded",
                        "task_id": res.task_id})
    return {"results": results,
            "succeeded": sum(1 for r in results if r["status"] == "succeeded"),
            "total": len(shots),
            "note": "全部为本地桩，未调用真实算力、未产生真实费用。"}


# --- 记录导出 CSV ---
def h_records_export(handler, m, body, q):
    """导出任务/生成记录为 CSV（桩数据与真实任务同构）。"""
    pid = q.get("project_id", [None])[0]
    state = get_state()
    tasks = state.store.all("tasks")
    shots = {s["id"]: s for s in state.store.all("shots")}
    outputs = {o["task_id"]: o for o in state.store.all("outputs")}
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["时间", "名称", "模式", "时长(s)", "分辨率", "状态", "输出"])
    for t in sorted(tasks, key=lambda x: x.get("created_at", ""), reverse=True):
        s = shots.get(t.get("shot_id"), {})
        o = outputs.get(t["id"], {})
        w.writerow([
            t.get("created_at", "")[:19].replace("T", " "),
            s.get("location") or f"镜头{s.get('index', '')}",
            t.get("mode", ""), s.get("duration", ""),
            o.get("resolution", ""), t.get("status", ""),
            o.get("url", ""),
        ])
    return {"csv": buf.getvalue(), "filename": "video-studio-records.csv"}


# --- 合成历史 ---
def h_compose_list(handler, m, body, q):
    pid = q.get("project_id", [None])[0]
    state = get_state()
    recs = state.store.find("compose_records", project_id=pid) if pid else state.store.all("compose_records")
    return {"records": sorted(recs, key=lambda r: r.get("created_at", ""), reverse=True)}


def h_compose_create(handler, m, body, q):
    """新增合成记录（纯记录，不执行 ffmpeg；成片文件不入库）。"""
    _require(body, "project_id", "name")
    state = get_state()
    rec = ComposeRecord(
        id=new_id("compose"), project_id=body["project_id"], name=body["name"],
        version=body.get("version", "v1"), episode_range=body.get("episode_range", ""),
        output_url=body.get("output_url", ""),
        duration=float(body.get("duration", 0) or 0), size_mb=float(body.get("size_mb", 0) or 0),
    )
    state.store.add("compose_records", rec)
    return {"record": rec.to_dict()}


def h_compose_delete(handler, m, body, q):
    cid = m.group(1)
    state = get_state()
    state.store.delete("compose_records", cid)
    return {"deleted": cid}


# --- 视频生成配置 ---
_DEFAULT_VIDEO_CONFIG: dict[str, Any] = {
    "ref2va_diffusion": "minimax_h3_ref2va_pinned_ih8_convconvert.safetensors",
    "ref2va_attention_accel": True,
    "cache_reuse_threshold": 0.2,
    "cache_result_ratio": 0.95,
    "seedvr2_main": "seedvr2_ema_3b_fp8_e4m3fn.safetensors",
    "superres_target": "1080",
    "superres_tile": 5,
    "ref2va_text_encoder": "qwen3vl_32b_minimax_h3_fp4_awq.safetensors",
    "video_easycache": False,
    "cache_start_ratio": 0.15,
    "use_seedvr2": True,
    "seedvr2_vae": "ema_vae_fp16.safetensors",
    "vram_limit": 1920,
    "cpu_offload_tiles": 16,
}


def h_video_config_get(handler, m, body, q):
    state = get_state()
    cfgs = state.store.all("video_configs")
    cfg = cfgs[0] if cfgs else None
    return {"config": cfg, "defaults": _DEFAULT_VIDEO_CONFIG,
            "note": "仅存配置参数，不打包模型权重/密钥；真实算力接入后置。"}


def h_video_config_save(handler, m, body, q):
    state = get_state()
    params = dict(_DEFAULT_VIDEO_CONFIG)
    params.update(body.get("params", {}))
    cfgs = state.store.all("video_configs")
    if cfgs:
        state.store.update("video_configs", cfgs[0]["id"], params=params, updated_at=utcnow())
        cfg = state.store.get("video_configs", cfgs[0]["id"])
    else:
        cfg_obj = VideoConfig(id=new_id("vcfg"), name=body.get("name", "默认配置"), params=params)
        state.store.add("video_configs", cfg_obj)
        cfg = cfg_obj.to_dict()
    return {"config": cfg}


# --- 编辑专家库 ---
_BUILTIN_EXPERTS: list[dict[str, Any]] = [
    {"id": "exp_script_gen", "name": "剧本生成", "category": "剧本",
     "description": "整集唯一 / 单一编辑", "system_prompt": "# 角色\n你是资深短剧编剧。基于故事梗概生成结构化单集剧本，含场景、动作、对白、情绪。\n\n# 约束\n- 原创内容，禁止复刻受版权保护的角色与情节。\n- 输出严格 JSON：{scene, action, dialogue, emotion}。",
     "builtin": True},
    {"id": "exp_script_continue", "name": "剧本续写", "category": "剧本",
     "description": "承接上集 / 多集递进", "system_prompt": "# 角色\n你负责承接已有剧本续写。读取前情摘要，保持人物、世界观一致，推进主线并留集尾钩子。\n\n# 约束\n- 承接前文设定，不自行推翻既定事实。\n- 输出续写正文 + 前情摘要更新。",
     "builtin": True},
    {"id": "exp_script_split", "name": "剧本拆解", "category": "剧本",
     "description": "分镜化 / 对白逐句拆", "system_prompt": "# 角色\n把剧本文本拆解为分镜：按情节节拍、对白变化与动作连续性分段，不预设固定段数。\n\n# 输出\n每段含 location / action / dialogue / emotion / duration。",
     "builtin": True},
    {"id": "exp_rewrite", "name": "分集剧本改写", "category": "剧本",
     "description": "据文本改写 / 质量审阅", "system_prompt": "# 角色\n改写分集剧本并做质量审阅：给出需求定位、目标受众、核心驱动力、故事类型、核心架构、主题、走向与大纲。\n\n# 约束\n- 不擅自改动既定正文，改写建议与正文分离展示。",
     "builtin": True},
    {"id": "exp_prompt_expand", "name": "提示词扩写", "category": "提示词",
     "description": "剧本 → H3 三段式", "system_prompt": "# 角色\n把镜头扩写为权威 H3 三段式提示词：环境/光线/动作/情绪/运镜/声音。\n\n# 硬性\n- 通过版权与内容安全守卫；禁止复刻特定画风、具名 IP、真人换脸。\n- 含对白且模式支持音频时绑定 (S1)/(S2) ↔ voice_id。",
     "builtin": True},
    {"id": "exp_prompt_polish", "name": "提示词润色", "category": "提示词",
     "description": "合理性 / 一致性检查", "system_prompt": "# 角色\n润色提示词，检查角色一致性与逻辑合理性，不改变核心语义。\n\n# 输出\n润色后提示词 + 变更说明。",
     "builtin": True},
    {"id": "exp_asset_plan", "name": "资产规划", "category": "资产",
     "description": "角色 / 场景 / 道具", "system_prompt": "# 角色\n从剧本抽取需要固化的资产：角色、场景、道具，给出锚点图提示词与来源声明建议。\n\n# 约束\n- 每个资产必须声明合法来源；不建议使用受版权保护的形象。",
     "builtin": True},
    {"id": "exp_knowledge", "name": "Knowledge", "category": "系统",
     "description": "知识库 / 风格 bible", "system_prompt": "# 角色\n维护项目知识库：人物设定、世界观、风格 bible，供其他专家引用。\n\n# 约束\n- 仅沉淀原创或已授权的设定。",
     "builtin": True},
]


def h_experts_list(handler, m, body, q):
    state = get_state()
    custom = state.store.all("experts")
    builtin_ids = {e["id"] for e in _BUILTIN_EXPERTS}
    custom = [c for c in custom if c.get("id") not in builtin_ids]
    return {"experts": _BUILTIN_EXPERTS + custom, "total": len(_BUILTIN_EXPERTS) + len(custom)}


def h_experts_save(handler, m, body, q):
    """编辑/新增自定义专家（内置专家不可覆盖，保存为自定义副本）。"""
    _require(body, "name")
    state = get_state()
    eid = body.get("id") or new_id("exp")
    data = {
        "id": eid, "name": body["name"], "category": body.get("category", ""),
        "description": body.get("description", ""),
        "system_prompt": body.get("system_prompt", ""),
        "enabled": bool(body.get("enabled", True)), "builtin": False,
    }
    from video_studio.models import Expert as _Expert
    if state.store._exists("experts", eid):
        state.store.update("experts", eid, **data)
    else:
        state.store.add("experts", _Expert(**data))
    return {"expert": state.store.get("experts", eid)}


# --- 资产库分类统计 ---
def h_asset_stats(handler, m, body, q):
    state = get_state()
    assets = state.store.all("assets")
    stats = {"all": len(assets), "role": 0, "scene": 0, "prop": 0, "image": 0}
    for a in assets:
        t = a.get("type", "")
        if t in stats:
            stats[t] += 1
        elif t == "storyboard":
            stats["image"] += 1
    return {"stats": stats, "assets": assets}


def h_demo_init(handler, m, body, q):
    """一键初始化原创示例项目：项目/剧集/剧本/拆镜/资产/提示词/任务/成片。

    全部内容为原创，零版权风险；任务与算力均为桩，不产生真实费用。
    """
    state = get_state()
    if state.store.find("projects"):
        proj = state.store.all("projects")[0]
        return {"initialized": False, "reason": "已有数据", "project": proj,
                "counts": _demo_counts(state, proj["id"])}

    proj = Project(id=new_id("proj"), name="示例短剧·江边", aspect_ratio="16:9",
                   budget_total=50.0, budget_daily=20.0, style_preset="realistic_3d")
    state.store.add("projects", proj)

    # 多类型资产库（全部原创，来源声明齐全）
    prov = AssetProvenance(asset_id="", source="self_created").to_dict()
    assets = [
        Asset(id=new_id("asset"), project_id=proj.id, type="role", name="林远",
              voice_id="voice_林远", approved=True, image_url="", version=1,
              appearance_json={"gender": "男", "age": "30", "note": "旧风衣，短发"},
              provenance=dict(prov)),
        Asset(id=new_id("asset"), project_id=proj.id, type="role", name="守渡人",
              voice_id="voice_守渡人", approved=True, image_url="", version=1,
              appearance_json={"gender": "男", "age": "60", "note": "灰布衣，花白"},
              provenance=dict(prov)),
        Asset(id=new_id("asset"), project_id=proj.id, type="scene", name="江边旧码头",
              approved=True, image_url="", version=1, provenance=dict(prov)),
        Asset(id=new_id("asset"), project_id=proj.id, type="prop", name="旧皮箱",
              approved=True, image_url="", version=1, provenance=dict(prov)),
    ]
    for a in assets:
        state.store.add("assets", a)
    role = assets[0]

    script_path = ROOT / "examples" / "sample_script.txt"
    text = script_path.read_text(encoding="utf-8") if script_path.exists() else ""
    ep_titles = ["第一集·归来", "第二集·夜渡", "第三集·未竟"]
    first_shots: list[str] = []
    for idx in range(1, 4):
        ep = Episode(id=new_id("ep"), project_id=proj.id, index=idx,
                     title=ep_titles[idx - 1], duration_target=45,
                     summary="前情：林远十年后回到江边旧码头。" if idx > 1 else "")
        state.store.add("episodes", ep)
        if not text:
            continue
        beats = import_script(text, "txt")
        shots = beats_to_shots(ep, beats)
        # 注入情绪字段（演示用，原创）
        emotions = ["凝重", "克制", "怅然", "平静", "释然", "悬念"]
        for si, s in enumerate(shots):
            s.duration = 8 + si
            # 第一集前两个镜头绑定参考图（角色+场景），演示连线检查的完整态
            if idx == 1 and si < 2:
                s.asset_refs = [assets[0].id, assets[2].id] if si == 0 else [assets[1].id]
            state.store.add("shots", s)
            if idx == 1:
                first_shots.append(s.id)

    # 提示词扩写 + 批准态 + 审计（合规通过）
    from video_studio.models import Prompt, Shot
    for s in state.store.find("shots"):
        shot = Shot.from_dict(s)
        expanded = expand_prompt(shot, [role], mode="r2v")
        report = check_prompt(shot.action or shot.dialogue or shot.prompt_text, subject=shot.id)
        _audit(state, "prompt_check", shot.id, report, {"mode": "r2v", "demo": True})
        state.store.add("prompts", Prompt(
            id=new_id("prompt"), shot_id=shot.id, version=1,
            content=expanded.to_submission(), approved=True, score=4.5,
        ))

    # 桩任务 + 采用版输出：让「提交生成 / 记录导出」页有真实可看的数据（不调用真实算力）
    from video_studio.models import Output, Task
    demo_shots = state.store.find("shots", episode_id=state.store.all("episodes")[0]["id"])[:2]
    for s in demo_shots:
        t = Task(id=new_id("task"), shot_id=s["id"], kind="r2v", mode="r2v",
                 status="succeeded", cost_estimate_cents=50, cost_actual_cents=48)
        state.store.add("tasks", t)
        out = Output(id=new_id("out"), task_id=t.id, url=f"outputs/{t.id}.mp4",
                     duration=float(s.get("duration", 8)), resolution="768P", version=1)
        state.store.add("outputs", out)
        state.store.adopt_output(out.id)

    # 合成历史（原创演示记录）
    state.store.add("compose_records", ComposeRecord(
        id=new_id("compose"), project_id=proj.id, name="示例短剧·江边 全集合集",
        version="v1", episode_range="第1-3集", output_url="outputs/demo_full.mp4",
        duration=135, size_mb=43.7,
    ))

    return {"initialized": True, "project": proj.to_dict(),
            "counts": _demo_counts(state, proj.id)}


def _demo_counts(state: AppState, project_id: str) -> dict:
    return {
        "shots": len(_shots_of_project(state, project_id)),
        "assets": len(state.store.find("assets", project_id=project_id)),
        "prompts": len(state.store.all("prompts")),
        "tasks": len(state.store.all("tasks")),
    }


# ---------------------------------------------------------------------------
# 八步制作流程：工作流定义 + 每步功能点的真实后端能力
# ---------------------------------------------------------------------------

def h_workflow_steps(handler, m, body, q):
    """工作台八步定义（方案定稿），每步功能点都映射到真实接口。"""
    return {"steps": steps_as_dicts(), "count": len(WORKBENCH_STEPS)}


# --- 第 1 步：项目制作设置（集数 / 单集时长 / 预算 → 生成剧集骨架） ---
def h_project_settings(handler, m, body, q):
    """设置项目制作参数并按集数调整剧集骨架（补齐缺少的、移除超出的）。"""
    pid = m.group(1)
    state = get_state()
    proj = state.store.get("projects", pid)
    changes = {}
    for key in ("aspect_ratio", "style_preset", "name"):
        if key in body:
            changes[key] = body[key]
    for key in ("budget_total", "budget_daily"):
        if key in body:
            changes[key] = float(body[key] or 0)
    if changes:
        state.store.update("projects", pid, **changes)

    ep_count = int(body.get("episode_count", 0) or 0)
    duration = float(body.get("duration_target", 60) or 60)
    created = 0
    removed = 0
    if ep_count > 0:
        existing = state.store.find("episodes", project_id=pid)
        have = {int(e.get("index", 0)) for e in existing}
        for idx in range(1, ep_count + 1):
            if idx in have:
                continue
            state.store.add("episodes", Episode(
                id=new_id("ep"), project_id=pid, index=idx,
                title=body.get("title_prefix", f"第{idx}集").strip() or f"第{idx}集",
                duration_target=duration,
            ))
            created += 1
        # 集数调小：移除超出范围的末尾剧集（含其下游数据），使集数真正可修改
        for e in existing:
            if int(e.get("index", 0)) > ep_count:
                _cascade_delete_episode(state, e["id"])
                state.store.delete("episodes", e["id"])
                removed += 1
    return {"project": state.store.get("projects", pid),
            "episodes": state.store.find("episodes", project_id=pid),
            "created_episodes": created,
            "removed_episodes": removed,
            "note": "集数已按设定更新；已有剧集保持不变，超出集数的末尾剧集已移除。"}


# --- 第 2 步：剧本（梗概生成 / 整本导入 / AI 改写 / 版本链） ---
def h_script_generate_outline(handler, m, body, q):
    """梗概 → LLM 桩生成多集大纲与正文，写入剧集并保存版本链（原创、合规前置）。"""
    _require(body, "project_id", "synopsis")
    state = get_state()
    pid = body["project_id"]
    state.store.get("projects", pid)  # 存在性校验
    n = max(1, int(body.get("episodes", 1) or 1))
    duration = float(body.get("duration_target", 60) or 60)
    provider = get_provider()
    outlines = provider.generate_outline(body["synopsis"], n)
    existing = state.store.find("episodes", project_id=pid)
    have = {int(e.get("index", 0)) for e in existing}
    made = []
    for i, outline in enumerate(outlines, start=1):
        idx = i
        if idx in have:
            eid = next(e["id"] for e in existing if int(e.get("index", 0)) == idx)
        else:
            ep = Episode(id=new_id("ep"), project_id=pid, index=idx,
                         title=f"第{idx}集", duration_target=duration,
                         script_text=outline)
            state.store.add("episodes", ep)
            eid = ep.id
        # 合规检查（原创梗概桩文本，正常放行）并写审计
        report = check_prompt(outline, subject=eid)
        _audit(state, "script_generate", eid, report, {"source": "synopsis_stub"})
        versions = state.store.find("script_versions", episode_id=eid)
        state.store.add("script_versions", ScriptVersion(
            id=new_id("sv"), episode_id=eid, version=len(versions) + 1,
            text=outline, source="stub", confirmed=False,
        ))
        made.append({"episode_id": eid, "index": idx, "title": f"第{idx}集",
                     "text": outline, "compliance": report.passed})
    return {"episodes": made,
            "note": "LLM 为本地桩：按梗概生成原创大纲，不调用外部算力。"}


_EP_TITLE_RE = re.compile(r"^第\s*([0-9零一二三四五六七八九十百千]+)\s*[集章回]")
# Markdown 标题形式的集标题，如「### 第 01 集 ｜ 第一幕 · 错：一场认错人的婚礼」
# 这是规范创作案里真正的分集边界，优先于正文里出现的「第X集」引用。
_MD_EP_RE = re.compile(r"^\s*#{1,6}\s+第\s*([0-9零一二三四五六七八九十百千]+)\s*集\b(.*)$")

# 副标题里这些开头词说明是正文叙述被误当标题，不是真正的集名
_TITLE_NARRATION_STARTS = (
    "就", "才", "便", "已经", "终于", "原来", "这时", "此时", "突然", "忽然",
    "然而", "但是", "于是", "接着", "随后", "只见", "却", "可", "连", "把", "被",
)
_SCENE_LINE_RE = re.compile(r"^\s*(场景|地点|地点：|场景：|内景|外景|INT\.|EXT\.)")


def _cn_to_int(s: str) -> int | None:
    """把「12」或「十二」「第一百二十」等中文数字转成 int；失败返回 None。"""
    if s.isdigit():
        return int(s)
    digit = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9}
    if not s or any(c not in digit and c not in "十百千" for c in s):
        return None
    total, section, number = 0, 0, 0
    for c in s:
        if c in digit:
            number = digit[c]
        elif c == "十":
            section += (number or 1) * 10
            number = 0
        elif c == "百":
            section += (number or 1) * 100
            number = 0
        elif c == "千":
            section += (number or 1) * 1000
            number = 0
    return section + number


def _clean_subtitle(rest: str, body_lines: list[str], num: int) -> str:
    """从标题行剩余文字里提炼一个干净的短副标题。

    常见的"乱标题"来源：把整句正文当集名（"第1集 就全部知道真相了原来…"），
    或是"第一集就全部知道真相"这种叙述开头。这里做过滤与兜底：
    - 太长、含对白/场景标记、叙述词开头的，一律丢弃；
    - 丢弃后从正文首行的「场景：」里提取地点作为副标题；
    - 都没有就留空（标题只显示「第N集」）。
    """
    rest = (rest or "").strip(" :：·-—·、,，。.!！?？")
    usable = ""
    # 真正的集名是短词（如「闪婚三年」「总裁的白月光」），不含句读标点；
    # 带逗号/句号/冒号的基本都是正文叙述，一律不进标题。
    _punct = "，。！？,、;；：:"
    if rest and len(rest) <= 12 and not rest.startswith(_TITLE_NARRATION_STARTS) \
            and not any(c in rest for c in _punct):
        usable = rest
    if not usable:
        for ln in body_lines:
            s = ln.strip()
            if not s:
                continue
            m = _SCENE_LINE_RE.match(s)
            if m:
                loc = re.sub(r"^\s*(场景|地点)\s*[:：]\s*", "", s).strip(" 。.")
                if loc and len(loc) <= 16:
                    usable = loc
            break  # 只看第一处非空行，通常是场景行
    return usable[:16]


def _md_episode_subtitle(tail: str) -> str:
    """从 Markdown 集标题的剩余文字里提取副标题。

    形如「｜ 第一幕 · 错：一场认错人的婚礼」「：她的右手在抖」。
    优先取全角/半角竖线或冒号后的最后一段（真正的集名），去掉幕标记前缀。
    """
    s = (tail or "").strip()
    # 用竖线或冒号切，取最后一段（最贴近集名）
    for sep in ("｜", "|", "：", ":"):
        if sep in s:
            s = s.split(sep)[-1].strip()
    # 去掉「第一幕 · 错」这类幕/阶段前缀残留
    s = re.sub(r"^.*?[·、]\s*", "", s).strip()
    s = s.strip(" *—-")
    return s[:20]


def _split_episodes(text: str) -> list[dict]:
    """把整部剧本切分成集，每集 {index, title, text}。

    两级策略：
    1. 若文本含 ≥2 个 Markdown 集标题（如「### 第 01 集 ｜ …」），判定为规范
       创作案，只按这些标题分集。策划案/大纲/金句库里出现的「第一集…」引用
       不在标题行上，不会被误切成集——这正是修复"90集乱码标题"的关键。
    2. 否则回退到纯文本策略：只把行首独立的「第X集/章/回」当标题。

    两种策略都强制标题集号按出现顺序连续编号（1..n），且不把大段正文塞进标题。
    """
    lines = text.splitlines()

    # ---- 策略 1：Markdown 标题优先 ----
    md_cuts: list[tuple[int, int, str]] = []  # (行号, 集号, 副标题)
    for i, line in enumerate(lines):
        m = _MD_EP_RE.match(line)
        if not m:
            continue
        num = _cn_to_int(m.group(1))
        subtitle = _md_episode_subtitle(m.group(2) or "")
        md_cuts.append((i, num or (len(md_cuts) + 1), subtitle))
    if len(md_cuts) >= 2:
        # 同一部创作案常同时含「分集大纲」与「逐集完整剧本」，同一个集号会出现
        # 两次。按集号分组，保留正文最完整的那个版本（完整剧本远长于大纲）。
        best: dict[int, dict] = {}
        for k, (line_no, num, subtitle) in enumerate(md_cuts):
            end = md_cuts[k + 1][0] if k + 1 < len(md_cuts) else len(lines)
            body = "\n".join(lines[line_no + 1:end]).strip()
            key = num or (k + 1)
            prev = best.get(key)
            if prev is None or len(body) > len(prev["text"]):
                best[key] = {"num": key, "title": subtitle, "text": body}
        ordered = sorted(best.values(), key=lambda d: d["num"])
        result: list[dict] = []
        for k, item in enumerate(ordered):
            subtitle = item["title"]
            title = f"第{k + 1}集" + (f" · {subtitle}" if subtitle else "")
            result.append({"index": k + 1, "title": title, "text": item["text"]})
        return result

    # ---- 策略 2：纯文本行首标题（回退） ----
    cuts: list[tuple[int, int]] = []  # (行号, 集号)
    rest_by_line: dict[int, str] = {}
    for i, line in enumerate(lines):
        stripped = line.strip()
        mm = _EP_TITLE_RE.match(stripped)
        if not mm:
            continue
        num = _cn_to_int(mm.group(1))
        cuts.append((i, num or (len(cuts) + 1)))
        rest_by_line[i] = stripped[mm.end():].strip(" :：·-—")
    if not cuts:
        return []

    result = []
    for k, (line_no, num) in enumerate(cuts):
        end = cuts[k + 1][0] if k + 1 < len(cuts) else len(lines)
        body_lines = lines[line_no + 1:end]
        if k == 0 and line_no > 0:
            body_lines = lines[0:line_no] + body_lines
        body = "\n".join(body_lines).strip()
        subtitle = _clean_subtitle(rest_by_line.get(line_no, ""), body_lines, num)
        title = f"第{k + 1}集" + (f" · {subtitle}" if subtitle else "")
        result.append({"index": k + 1, "title": title, "text": body})
    return result


def h_script_import_book(handler, m, body, q):
    """导入整部原稿，按「第X集」标题自动分集，每集正文落库并保存版本。

    默认 mode='replace'：先清空该项目已有剧集（级联清理下游数据、审计保留），
    再从第 1 集重新编号导入，避免重复导入导致集数累加。
    mode='append'：保留旧集，从末尾续接（显式追加场景）。
    """
    _require(body, "project_id", "text")
    state = get_state()
    pid = body["project_id"]
    state.store.get("projects", pid)
    text = body["text"]
    duration = float(body.get("duration_target", 60) or 60)
    mode = body.get("mode", "replace")
    # 严格分集：只把「行首独立成行的 第X集」当作集标题，正文里出现的
    # "第一集就全部知道真相…"这类句子不会被误切成新集，避免集序号与标题错乱。
    chapters = _split_episodes(text)
    if not chapters:
        chapters = [{"index": 1, "title": "第1集", "text": text.strip()}]

    replaced = 0
    if mode == "replace":
        # 整部重新导入 = 覆盖：清空旧集（级联清理镜头/提示词/任务/输出/版本）
        for old in state.store.find("episodes", project_id=pid):
            _cascade_delete_episode(state, old["id"])
            state.store.delete("episodes", old["id"])
            replaced += 1
        base = 0
    else:  # append：从末尾续接
        base = len(state.store.find("episodes", project_id=pid))

    made = []
    for j, ch in enumerate(chapters, start=1):
        idx = base + j
        ep = Episode(id=new_id("ep"), project_id=pid, index=idx,
                     title=ch["title"] or f"第{idx}集", duration_target=duration,
                     script_text=ch["text"])
        state.store.add("episodes", ep)
        report = check_prompt(ch["text"][:500], subject=ep.id)
        _audit(state, "script_import", ep.id, report, {"source": "book_import"})
        state.store.add("script_versions", ScriptVersion(
            id=new_id("sv"), episode_id=ep.id, version=1, text=ch["text"],
            source="import", confirmed=False,
        ))
        made.append({"episode_id": ep.id, "index": ep.index, "title": ep.title})
    note = ("已覆盖导入：按「第X集」标题行顺序分集，序号连续。" if mode == "replace"
            else "已追加导入：在原剧集末尾续接。")
    return {"episodes": made, "chapters": len(made), "replaced": replaced,
            "mode": mode, "note": note}


def h_script_rewrite(handler, m, body, q):
    """AI 改写当前集正文：合规前置，改写后存版本链，保留前后对照。"""
    _require(body, "episode_id", "instruction")
    state = get_state()
    eid = body["episode_id"]
    ep_data = state.store.get("episodes", eid)
    before = ep_data.get("script_text", "")
    report = check_prompt(before or body["instruction"], subject=eid)
    _audit(state, "script_rewrite_check", eid, report, {"instruction": body["instruction"]})
    if not report.passed:
        raise ApiError(422, "改写指令命中版权/内容安全红线，已拦截")
    result = get_provider().rewrite(before, body["instruction"])
    versions = state.store.find("script_versions", episode_id=eid)
    sv = ScriptVersion(
        id=new_id("sv"), episode_id=eid, version=len(versions) + 1,
        text=result.text, before=before, instruction=body["instruction"],
        source=result.model, confirmed=False,
    )
    state.store.add("script_versions", sv)
    state.store.update("episodes", eid, script_text=result.text)
    return {"version": sv.to_dict(), "text": result.text,
            "note": "LLM 为本地桩；改写保留前后对照，可在版本链查看。"}


def h_script_versions(handler, m, body, q):
    eid = q.get("episode_id", [None])[0]
    state = get_state()
    vers = state.store.find("script_versions", episode_id=eid) if eid else state.store.all("script_versions")
    vers = sorted(vers, key=lambda v: (v.get("episode_id", ""), v.get("version", 0)))
    return {"versions": vers, "total": len(vers)}


def h_script_confirm(handler, m, body, q):
    """人工确认某集剧本版本：同集其他版本取消 confirmed。"""
    vid = m.group(1)
    state = get_state()
    target = state.store.get("script_versions", vid)
    for v in state.store.find("script_versions", episode_id=target["episode_id"]):
        if v.get("confirmed"):
            state.store.update("script_versions", v["id"], confirmed=False)
    state.store.update("script_versions", vid, confirmed=True)
    return {"version": state.store.get("script_versions", vid)}


# --- 第 3 步：拆镜（自动拆镜 / 缺字段高亮 / 镜头编辑） ---
def h_shots_split(handler, m, body, q):
    """把当前集正文重新拆为镜头表（覆盖式重跑，保留可追溯提示）。"""
    _require(body, "episode_id")
    state = get_state()
    eid = body["episode_id"]
    ep = Episode.from_dict(state.store.get("episodes", eid))
    text = body.get("text") or ep.script_text
    if not text:
        raise ApiError(400, "该集暂无剧本文本，请先生成或导入")
    fmt = body.get("format", "txt")
    try:
        beats = import_script(text, fmt)
    except ValueError as e:
        raise ApiError(400, str(e))
    # 覆盖式重跑：删除该集旧镜头
    for s in state.store.find("shots", episode_id=eid):
        state.store.delete("shots", s["id"])
    duration = float(body.get("duration", 5) or 5)
    shots = beats_to_shots(ep, beats, duration=duration)
    for s in shots:
        state.store.add("shots", s)
    return {"shots": [s.to_dict() for s in shots],
            "missing_fields": missing_fields_report(shots),
            "total": len(shots),
            "note": "拆镜为可重跑操作：旧镜头已替换，引用关系请到参考资产步骤重绑。"}


def h_shots_missing(handler, m, body, q):
    eid = q.get("episode_id", [None])[0]
    state = get_state()
    shots = [Shot.from_dict(s) for s in (
        state.store.find("shots", episode_id=eid) if eid else state.store.all("shots"))]
    return {"missing_fields": missing_fields_report(shots),
            "total_shots": len(shots)}


def h_shot_update(handler, m, body, q):
    sid = m.group(1)
    state = get_state()
    state.store.get("shots", sid)
    allowed = ("location", "action", "dialogue", "duration", "prompt_text")
    changes = {k: body[k] for k in allowed if k in body}
    if "duration" in changes:
        changes["duration"] = float(changes["duration"] or 0)
    if changes:
        state.store.update("shots", sid, **changes)
    shot = Shot.from_dict(state.store.get("shots", sid))
    return {"shot": shot.to_dict(),
            "missing": [f for f, ok in (("location", shot.location), ("action", shot.action),
                                        ("dialogue", shot.dialogue)) if not ok]}


# --- 第 4 步：参考资产（设置参考图 + 锁定资产版本） ---
def h_shot_set_refs(handler, m, body, q):
    """为镜头绑定参考资产：只允许已批准资产，锁定 asset_version。"""
    _require(body, "asset_ids")
    sid = m.group(1)
    state = get_state()
    shot_data = state.store.get("shots", sid)
    asset_ids = list(body["asset_ids"])
    approved_ids = {a["id"] for a in state.store.all("assets") if a.get("approved")}
    illegal = [a for a in asset_ids if a not in approved_ids]
    if illegal:
        raise ApiError(403, "包含未批准资产，禁止引用：" + ", ".join(illegal))
    version = int(body.get("asset_version", 0) or 0)
    changes = {"asset_refs": asset_ids}
    if version > 0:
        changes["asset_version"] = version
    state.store.update("shots", sid, **changes)
    # 审计引用动作
    from video_studio.compliance import AssetProvenance, check_asset_provenance
    for aid in asset_ids:
        ad = state.store.get("assets", aid)
        prov = AssetProvenance.from_dict(ad["provenance"]) if ad.get("provenance") else None
        rep = check_asset_provenance(prov)
        _audit(state, "asset_reference", aid, rep, {"shot_id": sid, "version": version or None})
    return {"shot": state.store.get("shots", sid), "bound": len(asset_ids)}


# --- 第 5 步：提示词（清空未批准 = 真实操作） ---
def h_prompts_clear_unapproved(handler, m, body, q):
    eid = q.get("episode_id", [None])[0]
    state = get_state()
    shot_ids = {s["id"] for s in (state.store.find("shots", episode_id=eid) if eid else state.store.all("shots"))}
    removed = 0
    for p in state.store.all("prompts"):
        if not p.get("approved") and p.get("shot_id") in shot_ids:
            state.store.delete("prompts", p["id"])
            removed += 1
    return {"removed": removed, "note": "仅清理未批准版本；已批准版本保留以维持版本链可追溯。"}


# --- 第 6 步：提交生成（一键停止 = 真实取消未完成的任务） ---
def h_tasks_cancel(handler, m, body, q):
    eid = body.get("episode_id")
    pid = q.get("project_id", [None])[0]
    state = get_state()
    if eid:
        shot_ids = {s["id"] for s in state.store.find("shots", episode_id=eid)}
        tasks = [t for t in state.store.all("tasks") if t.get("shot_id") in shot_ids]
    elif pid:
        shot_ids = {s["id"] for s in _shots_of_project(state, pid)}
        tasks = [t for t in state.store.all("tasks") if t.get("shot_id") in shot_ids]
    else:
        tasks = state.store.all("tasks")
    cancellable = {"created", "queued", "leased", "running", "uploaded"}
    cancelled = 0
    for t in tasks:
        if t.get("status") in cancellable:
            state.store.update("tasks", t["id"], status="cancelled")
            cancelled += 1
    return {"cancelled": cancelled, "total": len(tasks),
            "note": "仅取消未完成任务；succeeded / dead_letter 保持不变。"}


# --- 第 7 步：监控挑片（候选 / 采用 / 失败诊断 / 重试） ---
def h_candidates_generate(handler, m, body, q):
    """为镜头生成 2–4 个候选输出（桩），供挑片对比。"""
    _require(body, "shot_id")
    state = get_state()
    sid = body["shot_id"]
    shot = state.store.get("shots", sid)
    n = max(2, min(4, int(body.get("count", 3) or 3)))
    # 找一个 succeeded 任务挂载候选；没有则建一个桩任务
    tasks = state.store.find("tasks", shot_id=sid)
    task = next((t for t in tasks if t.get("status") == "succeeded"), None)
    if task is None:
        t = Task(id=new_id("task"), shot_id=sid, kind="r2v", mode="r2v",
                 status="succeeded", cost_estimate_cents=0, cost_actual_cents=0)
        state.store.add("tasks", t)
        task = state.store.get("tasks", t.id)
    existing = state.store.find("outputs", task_id=task["id"])
    for c in existing:
        state.store.delete("outputs", c["id"])
    made = []
    for i in range(1, n + 1):
        out = Output(id=new_id("out"), task_id=task["id"],
                     url=f"outputs/{task['id']}/candidate_{i}.mp4",
                     duration=float(shot.get("duration", 5) or 5),
                     resolution="768P", version=i)
        state.store.add("outputs", out)
        made.append(out.to_dict())
    return {"candidates": made, "task_id": task["id"], "total": len(made),
            "note": "候选为本地桩输出，不调用真实算力、无真实文件。"}


def h_candidates_list(handler, m, body, q):
    eid = q.get("episode_id", [None])[0]
    state = get_state()
    shot_ids = {s["id"] for s in (state.store.find("shots", episode_id=eid) if eid else state.store.all("shots"))}
    tasks = [t for t in state.store.all("tasks") if t.get("shot_id") in shot_ids]
    task_ids = {t["id"]: t for t in tasks}
    groups = []
    for t in tasks:
        outs = [dict(o) for o in state.store.find("outputs", task_id=t["id"])]
        if not outs:
            continue
        groups.append({
            "shot_id": t["shot_id"],
            "task_id": t["id"],
            "task_status": t.get("status", ""),
            "candidates": sorted(outs, key=lambda o: o.get("version", 0)),
            "adopted": next((o["id"] for o in outs if o.get("is_adopted")), ""),
        })
    return {"groups": groups, "total_tasks": len(task_ids)}


def h_output_adopt(handler, m, body, q):
    """挑片采用：is_adopted 唯一真源，同步 shot.adopted_task_id。"""
    oid = m.group(1)
    state = get_state()
    state.store.adopt_output(oid)  # 不存在会抛 KeyError → 404
    out = state.store.get("outputs", oid)
    return {"output": out, "adopted": True,
            "task": state.store.get("tasks", out["task_id"])}


def h_tasks_failures(handler, m, body, q):
    eid = q.get("episode_id", [None])[0]
    state = get_state()
    shot_ids = {s["id"] for s in (state.store.find("shots", episode_id=eid) if eid else state.store.all("shots"))}
    failing = [t for t in state.store.all("tasks")
               if t.get("shot_id") in shot_ids
               and (t.get("status") in {"failed", "dead_letter"} or t.get("failure_code"))]
    rows = []
    for t in failing:
        code = t.get("failure_code") or "UNKNOWN"
        rows.append({"task": t, "code": code,
                     "diagnosis": diagnose(code),
                     "spec": asdict(failure_spec(code))})
    return {"failures": rows, "total": len(rows)}


def h_task_retry(handler, m, body, q):
    """一键重试：可重试失败码重新派单（桩内直接成功），不可重试给出理由。"""
    from video_studio.control.failure import is_retryable
    tid = m.group(1)
    state = get_state()
    t = state.store.get("tasks", tid)
    code = t.get("failure_code") or "PROVIDER_TRANSIENT"
    if not is_retryable(code):
        raise ApiError(400, "该失败码不可自动重试：" + diagnose(code))
    new_state = state.scheduler.on_failure(tid, code)
    # 桩：重试后立即成功，产出采用版
    state.store.update("tasks", tid, status="succeeded")
    out = Output(id=new_id("out"), task_id=tid,
                 url=f"outputs/{tid}/retry.mp4",
                 resolution="768P", version=1)
    state.store.add("outputs", out)
    state.store.adopt_output(out.id)
    return {"task": state.store.get("tasks", tid),
            "previous_state": new_state,
            "diagnosis": diagnose(code),
            "note": "已按可重试失败码自动重派并完成（桩）。"}


# --- 第 8 步：精修成片（装配计划 / 字幕 / 升清策略 / 导出记录） ---
def h_finalize_plan(handler, m, body, q):
    """按项目已采用镜头生成装配计划：裁头 + 拼接 + SRT 字幕 + CTA + 升清策略。"""
    pid = q.get("project_id", [None])[0]
    eid = q.get("episode_id", [None])[0]
    backend_kind = q.get("backend", ["api"])[0]
    cta = q.get("cta", ["关注我们 · 下集见"])[0]
    state = get_state()
    if not pid:
        projects = state.store.all("projects")
        pid = projects[0]["id"] if projects else ""
    shots_meta = {s["id"]: s for s in (state.store.find("shots", episode_id=eid) if eid
                                        else _shots_of_project(state, pid))}
    # 取采用版输出：按 shot 的 adopted_task_id
    adopted = []
    for sid, s in shots_meta.items():
        tid = s.get("adopted_task_id")
        if not tid:
            continue
        outs = state.store.find("outputs", task_id=tid, is_adopted=True)
        if outs:
            adopted.append({**s, "output_url": outs[0]["url"]})
    cues = cues_from_shots(adopted)
    srt = render_srt(cues)
    clips = [a["output_url"] for a in adopted]
    try:
        up = upscale_strategy(backend_kind)
        up_val = up.value
    except ValueError:
        up_val = None
    plan = build_compose_plan(clips, srt_path="subtitles.srt", cta_text=cta,
                              output="final.mp4")
    pd = asdict(plan)
    pd["upscale"] = up_val
    return {
        "cues": [asdict(c) for c in cues],
        "srt": srt,
        "plan": pd,
        "ffmpeg_script": plan.to_ffmpeg_script(),
        "adopted_clips": len(clips),
        "note": "真实转码（ffmpeg / 升清）依赖外部算力，已后置；此处生成可审查的装配计划与字幕。",
    }


def h_finalize_export(handler, m, body, q):
    """导出成片：按参数生成一条成片记录（成片文件不入库，只存引用）。"""
    _require(body, "project_id")
    state = get_state()
    pid = body["project_id"]
    state.store.get("projects", pid)
    eid = body.get("episode_id", "")
    shots_meta = {s["id"]: s for s in (state.store.find("shots", episode_id=eid) if eid
                                        else _shots_of_project(state, pid))}
    total_duration = round(sum(float(s.get("duration", 5) or 5) for s in shots_meta.values()), 1)
    rec = ComposeRecord(
        id=new_id("compose"), project_id=pid,
        name=body.get("name", "精修成片"),
        version=body.get("version", "v1"),
        episode_range=body.get("episode_range", "全部"),
        output_url=f"outputs/final/{new_id('final')}.mp4",
        duration=float(body.get("duration", total_duration) or total_duration),
        size_mb=float(body.get("size_mb", 0) or 0),
    )
    state.store.add("compose_records", rec)
    return {"record": rec.to_dict(),
            "note": "已生成成片记录；真实 MP4 转码依赖 ffmpeg，已后置。"}


# ---------------------------------------------------------------------------
# 路由表
# ---------------------------------------------------------------------------

ROUTES: list[tuple[str, re.Pattern, Callable]] = [
    ("GET", r"^/api/health$", h_health),
    ("GET", r"^/api/pipeline$", h_pipeline),
    ("GET", r"^/api/backends$", h_backends),
    ("GET", r"^/api/projects$", h_projects_list),
    ("POST", r"^/api/projects$", h_projects_create),
    ("GET", r"^/api/projects/([^/]+)$", h_projects_get),
    ("GET", r"^/api/episodes$", h_episodes_list),
    ("POST", r"^/api/episodes$", h_episodes_create),
    ("POST", r"^/api/episodes/([^/]+)/update$", h_episode_update),
    ("DELETE", r"^/api/episodes/([^/]+)$", h_episode_delete),
    ("POST", r"^/api/script/import$", h_script_import),
    ("GET", r"^/api/shots$", h_shots_list),
    ("GET", r"^/api/assets$", h_assets_list),
    ("POST", r"^/api/assets$", h_assets_create),
    ("POST", r"^/api/assets/([^/]+)/approve$", h_assets_approve),
    ("POST", r"^/api/prompts/expand$", h_prompts_expand),
    ("GET", r"^/api/prompts$", h_prompts_list),
    ("POST", r"^/api/compliance/check$", h_compliance_check),
    ("GET", r"^/api/audit$", h_audit_list),
    ("GET", r"^/api/audit/verify$", h_audit_verify),
    ("GET", r"^/api/cost/overview$", h_cost_overview),
    ("POST", r"^/api/cost/check$", h_cost_check),
    ("GET", r"^/api/tasks$", h_tasks_list),
    ("POST", r"^/api/tasks$", h_tasks_submit),
    ("POST", r"^/api/tasks/([^/]+)/dispatch$", h_tasks_dispatch),
    ("POST", r"^/api/tasks/([^/]+)/fail$", h_tasks_fail),
    ("GET", r"^/api/failure-codes$", h_failure_codes),
    ("POST", r"^/api/compose/plan$", h_compose_plan),
    # --- 新工作台：剧本/分集/提示词/预览/提交/记录/合成/配置/专家/资产库 ---
    ("GET", r"^/api/episodes/tree$", h_episodes_tree),
    ("GET", r"^/api/script/outline$", h_script_outline),
    ("POST", r"^/api/prompts/approve$", h_prompts_approve),
    ("POST", r"^/api/prompts/batch$", h_prompts_batch),
    ("GET", r"^/api/preview/check$", h_preview_check),
    ("POST", r"^/api/submit/batch$", h_submit_batch),
    ("GET", r"^/api/records/export$", h_records_export),
    ("GET", r"^/api/compose/records$", h_compose_list),
    ("POST", r"^/api/compose/records$", h_compose_create),
    ("DELETE", r"^/api/compose/records/([^/]+)$", h_compose_delete),
    ("GET", r"^/api/video-config$", h_video_config_get),
    ("POST", r"^/api/video-config$", h_video_config_save),
    ("GET", r"^/api/experts$", h_experts_list),
    ("POST", r"^/api/experts$", h_experts_save),
    ("GET", r"^/api/asset-stats$", h_asset_stats),
    ("POST", r"^/api/demo/init$", h_demo_init),
    # --- 八步制作流程：工作流定义 + 每步功能点 ---
    ("GET", r"^/api/workflow/steps$", h_workflow_steps),
    ("POST", r"^/api/projects/([^/]+)/settings$", h_project_settings),
    ("POST", r"^/api/script/generate-outline$", h_script_generate_outline),
    ("POST", r"^/api/script/import-book$", h_script_import_book),
    ("POST", r"^/api/script/rewrite$", h_script_rewrite),
    ("GET", r"^/api/script/versions$", h_script_versions),
    ("POST", r"^/api/script/versions/([^/]+)/confirm$", h_script_confirm),
    ("POST", r"^/api/shots/split$", h_shots_split),
    ("GET", r"^/api/shots/missing$", h_shots_missing),
    ("POST", r"^/api/shots/([^/]+)/update$", h_shot_update),
    ("POST", r"^/api/shots/([^/]+)/refs$", h_shot_set_refs),
    ("POST", r"^/api/prompts/clear-unapproved$", h_prompts_clear_unapproved),
    ("POST", r"^/api/tasks/cancel$", h_tasks_cancel),
    ("POST", r"^/api/candidates/generate$", h_candidates_generate),
    ("GET", r"^/api/candidates$", h_candidates_list),
    ("POST", r"^/api/outputs/([^/]+)/adopt$", h_output_adopt),
    ("GET", r"^/api/tasks/failures$", h_tasks_failures),
    ("POST", r"^/api/tasks/([^/]+)/retry$", h_task_retry),
    ("GET", r"^/api/finalize/plan$", h_finalize_plan),
    ("POST", r"^/api/finalize/export$", h_finalize_export),
    # --- 算力管理：三种后端接口全景 / 配置 / 切换 / 健康 / ComfyUI / 桩自检 ---
    ("GET", r"^/api/compute/configs$", h_compute_configs),
    ("POST", r"^/api/compute/switch$", h_compute_switch),
    ("GET", r"^/api/compute/health$", h_compute_health),
    ("POST", r"^/api/compute/simulate$", h_compute_simulate),
    ("POST", r"^/api/comfyui/workflow$", h_comfyui_workflow),
]


class Handler(BaseHTTPRequestHandler):
    server_version = "VideoStudioWorkbench/1.0"

    def log_message(self, fmt: str, *args) -> None:
        # 精简日志，避免刷屏；生产可写入文件
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _send(self, status: int, payload: Any, content_type: str = "application/json; charset=utf-8") -> None:
        if isinstance(payload, (dict, list)):
            data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        elif isinstance(payload, bytes):
            data = payload
        else:
            data = str(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        # 工作台前端频繁迭代，禁用缓存，避免用户看到旧界面（改动不生效）
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        # 用完即断：避免 keep-alive 连接跨测试类遗留，访问到被重置的全局 STATE
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, path: str) -> None:
        rel = path.lstrip("/") or "index.html"
        target = (STATIC_DIR / rel).resolve()
        # 目录穿越防护
        if not str(target).startswith(str(STATIC_DIR.resolve())):
            self._send(403, {"error": "禁止访问"})
            return
        if not target.is_file():
            self._send(404, {"error": "文件不存在"})
            return
        suffix = target.suffix.lower()
        ctype = {".html": "text/html; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8"}.get(suffix, "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if method == "GET" and not path.startswith("/api/"):
            self._serve_static(path)
            return
        for mth, pattern, handler in ROUTES:
            if mth != method:
                continue
            m = re.match(pattern, path)
            if not m:
                continue
            try:
                body = _body_bytes(self) if method == "POST" else {}
                result = handler(self, m, body, query)
                self._send(200, result)
            except ApiError as e:
                self._send(e.status, {"error": e.message})
            except ComplianceBlocked as e:
                self._send(422, {"error": f"合规守卫拦截：{e}"})
            except KeyError as e:
                self._send(404, {"error": f"记录不存在：{e}"})
            except Exception as e:  # noqa: BLE001
                self._send(500, {"error": f"服务内部错误：{type(e).__name__}: {e}"})
            return
        self._send(404, {"error": f"未知接口：{method} {path}"})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_OPTIONS(self):
        self._send(204, b"")


def run(host: str = "127.0.0.1", port: int = 8765) -> None:
    get_state()
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"video-studio 工作台已启动： http://{host}:{port}")
    print(f"数据目录： {DATA_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="video-studio 工作台 API 服务")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    run(args.host, args.port)
