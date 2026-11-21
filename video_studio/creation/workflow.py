"""工作台八步制作流程（方案定稿）。

注意与 pipeline.py 的区分：
- pipeline.py 是「能力链」抽象（创意/剧本/角色/分镜/关键帧/动画/配音/合成），
  供任务与算力适配层内部使用，测试对其 Step 枚举有依赖，保持不变。
- 本模块是用户在工作台看到的「八步制作流程」，顺序按方案 v2 定稿：
  项目 → 剧本 → 拆镜 → 参考资产 → 提示词 → 提交 → 挑片 → 精修成片。

每一步列出「功能点」，每个功能点都对应一个真实可调用、可追溯的后端能力，
而不是展示用的占位。前端通过 /api/workflow/steps 拉取本定义渲染 stepper。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class FunctionPoint:
    key: str
    label: str
    desc: str
    endpoint: str


@dataclass(frozen=True)
class WorkbenchStep:
    key: str
    name: str
    short: str
    desc: str
    points: list[FunctionPoint] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# 方案定稿八步，每步功能点都落到真实后端接口
WORKBENCH_STEPS: list[WorkbenchStep] = [
    WorkbenchStep(
        key="project", name="创建项目", short="项目",
        desc="片名 / 画幅 / 风格 / 集数 / 单集时长 → 生成项目骨架",
        points=[
            FunctionPoint("project_create", "新建项目", "创建项目并写入画幅、风格、预算", "POST /api/projects"),
            FunctionPoint("project_settings", "制作设置", "设置集数、单集时长、预算并生成剧集骨架", "POST /api/projects/<id>/settings"),
        ],
    ),
    WorkbenchStep(
        key="script", name="剧本", short="剧本",
        desc="梗概 AI 生成分集大纲，或导入原稿；改写后人工确认，保存版本",
        points=[
            FunctionPoint("script_generate", "梗概生成", "用 LLM 桩按梗概生成多集大纲与正文", "POST /api/script/generate-outline"),
            FunctionPoint("script_import", "导入原稿", "导入整部原稿，自动按集切分并落库", "POST /api/script/import-book"),
            FunctionPoint("script_rewrite", "AI 改写", "按指令改写当前集正文并保存版本链", "POST /api/script/rewrite"),
            FunctionPoint("script_versions", "版本历史", "查看每集剧本的改写版本与前后对照", "GET /api/script/versions"),
        ],
    ),
    WorkbenchStep(
        key="storyboard", name="拆镜", short="拆镜",
        desc="按场景 / 人物动作 / 对白 / 时长拆成镜头表，缺字段高亮",
        points=[
            FunctionPoint("shot_split", "自动拆镜", "按场景/动作/对白把剧本拆成结构化镜头", "POST /api/shots/split"),
            FunctionPoint("shot_missing", "缺字段高亮", "列出缺字段镜头，供人工补全", "GET /api/shots/missing"),
            FunctionPoint("shot_edit", "镜头编辑", "编辑镜头的场景/动作/对白/时长", "POST /api/shots/<id>/update"),
        ],
    ),
    WorkbenchStep(
        key="reference", name="参考资产", short="参考资产",
        desc="角色四视图、场景图与音色锚点；人工批准硬门槛，锁定 asset_version",
        points=[
            FunctionPoint("asset_create", "登记资产", "登记角色/场景/道具并记录来源声明", "POST /api/assets"),
            FunctionPoint("asset_approve", "人工批准", "来源合规检查通过后批准为可用资产", "POST /api/assets/<id>/approve"),
            FunctionPoint("shot_refs", "绑定参考图", "为镜头绑定已批准资产并锁定版本", "POST /api/shots/<id>/refs"),
        ],
    ),
    WorkbenchStep(
        key="prompt", name="提示词", short="提示词",
        desc="按权威 Schema 扩写 H3 三段式 / R2V 六段式，对白绑定 voice_id",
        points=[
            FunctionPoint("prompt_expand", "规范扩写", "版权守卫 + 内容安全前置后扩写为规范提示词", "POST /api/prompts/expand"),
            FunctionPoint("prompt_batch", "批量扩写", "一键扩写整集镜头", "POST /api/prompts/batch"),
            FunctionPoint("prompt_approve", "批准评分", "版本链只保留一个批准版并记录评分", "POST /api/prompts/approve"),
        ],
    ),
    WorkbenchStep(
        key="submit", name="提交生成", short="提交生成",
        desc="累计成本闸门（预估 + 已花 + 在途 ≤ 上限）校验后批量建任务",
        points=[
            FunctionPoint("cost_check", "成本闸门", "提交前原子校验累计成本是否超预算", "POST /api/cost/check"),
            FunctionPoint("submit_batch", "批量提交", "按 T2V/I2V/R2V 批量建任务，链式锁可选", "POST /api/submit/batch"),
            FunctionPoint("task_cancel", "停止任务", "取消本集未完成的任务", "POST /api/tasks/cancel"),
        ],
    ),
    WorkbenchStep(
        key="review", name="监控挑片", short="监控挑片",
        desc="每段生成 2–4 候选，人工选中采用版；失败按失败码诊断并一键重试",
        points=[
            FunctionPoint("candidate_gen", "生成候选", "为镜头生成 2–4 个候选输出（桩）", "POST /api/candidates/generate"),
            FunctionPoint("candidate_adopt", "挑片采用", "选中候选为采用版，唯一真源同步到镜头", "POST /api/outputs/<id>/adopt"),
            FunctionPoint("failure_diag", "失败诊断", "按最小失败码给出诊断与建议动作", "GET /api/tasks/failures"),
            FunctionPoint("task_retry", "一键重试", "可重试失败码自动重派，不可重试给出理由", "POST /api/tasks/<id>/retry"),
        ],
    ),
    WorkbenchStep(
        key="finalize", name="精修成片", short="精修成片",
        desc="升清、裁头杂音、拼字幕与片尾 CTA，导出 MP4（记录管理）",
        points=[
            FunctionPoint("finalize_plan", "装配计划", "裁头 + 拼接 + 字幕 + CTA + 升清策略", "GET /api/finalize/plan"),
            FunctionPoint("finalize_export", "导出成片", "按装配计划导出并记录成片版本", "POST /api/finalize/export"),
            FunctionPoint("srt", "字幕文件", "按镜头对白生成 SRT 字幕", "GET /api/finalize/plan"),
        ],
    ),
]


def steps_as_dicts() -> list[dict]:
    return [s.to_dict() for s in WORKBENCH_STEPS]


def step_keys() -> list[str]:
    return [s.key for s in WORKBENCH_STEPS]


__all__ = [
    "FunctionPoint", "WorkbenchStep", "WORKBENCH_STEPS",
    "steps_as_dicts", "step_keys",
]
