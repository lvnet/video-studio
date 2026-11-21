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
    beats_to_shots,
    expand_prompt,
    import_script,
    missing_fields_report,
)
from video_studio.models import (  # noqa: E402
    Asset,
    Episode,
    LocalStore,
    PriceRule,
    Project,
    new_id,
    utcnow,
)
from video_studio.post import (  # noqa: E402
    build_compose_plan,
    cues_from_shots,
    render_srt,
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
    return {
        "backends": [k.value for k in BackendKind],
        "registered": list(default_registry.names()) if hasattr(default_registry, "names") else
                      [k.value for k in BackendKind],
        "note": "三种后端均为桩实现，真实算力接入后置；切换不改上层业务。",
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


def h_demo_init(handler, m, body, q):
    """一键初始化原创示例项目：项目/剧集/剧本/拆镜/资产/提示词/任务/成片。

    全部内容为原创，零版权风险；任务与算力均为桩，不产生真实费用。
    """
    state = get_state()
    if state.store.find("projects"):
        proj = state.store.all("projects")[0]
        return {"initialized": False, "reason": "已有数据", "project": proj,
                "counts": _demo_counts(state, proj["id"])}

    proj = Project(id=new_id("proj"), name="示例短剧·江边", aspect_ratio="9:16",
                   budget_total=50.0, budget_daily=20.0)
    state.store.add("projects", proj)
    ep = Episode(id=new_id("ep"), project_id=proj.id, index=1, title="第一集")
    state.store.add("episodes", ep)

    script_path = ROOT / "examples" / "sample_script.txt"
    text = script_path.read_text(encoding="utf-8") if script_path.exists() else ""
    if text:
        beats = import_script(text, "txt")
        shots = beats_to_shots(ep, beats)
        for s in shots:
            state.store.add("shots", s)

    role = Asset(
        id=new_id("asset"), project_id=proj.id, type="role", name="林远",
        voice_id="voice_linyuan", approved=True, image_url="assets/linyuan.png", version=1,
        provenance=AssetProvenance(asset_id="", source="self_created").to_dict(),
    )
    state.store.add("assets", role)

    # 提示词扩写 + 审计（合规通过）
    for s in state.store.find("shots", episode_id=ep.id):
        from video_studio.models import Shot
        shot = Shot.from_dict(s)
        expanded = expand_prompt(shot, [role], mode="r2v")
        report = check_prompt(shot.action or shot.dialogue or shot.prompt_text, subject=shot.id)
        _audit(state, "prompt_check", shot.id, report, {"mode": "r2v", "demo": True})
        from video_studio.models import Prompt
        state.store.add("prompts", Prompt(
            id=new_id("prompt"), shot_id=shot.id, version=1,
            content=expanded.to_submission(),
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
    ("POST", r"^/api/demo/init$", h_demo_init),
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
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
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
