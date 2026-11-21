# Changelog

本项目所有重要变更记录于此。格式参考 Keep a Changelog。

## [Unreleased]

### Changed
- **合规补强**：版权守卫与内容安全过滤改用归一化匹配（全角/空格/繁简/形近字），提高黑名单绕过成本；新增内容安全分级过滤器（BLOCK/REVIEW）；新增只追加审计日志（哈希链防篡改、可查询）。仍诚实标注为最小守卫，非绝对保证。
- **架构修复**：算力层请求对象 `Task` 更名为 `GenRequest`，与控制面持久化 `Task` 区分；`compute.Task` 保留为向后兼容别名。
- **文档**：新增 ARCHITECTURE.md，明确 LocalStore 单机边界（无事务/无并发锁）与迁移 PostgreSQL 路径、合规与算力边界。

### Added
- **合规底座（红线）**：LICENSE 明确仅覆盖本项目代码；COMPLIANCE.md 写清第三方模型/素材许可边界、禁止用途、备案边界；提示词版权守卫（拒绝复刻特定画风/具名IP/真人换脸/违法用途）；素材来源声明与检查；资产入用双门槛（人工批准 + 来源合规）。
- **核心数据模型与本地持久化**：Project/Episode/Shot/Asset/TaskAsset/Prompt/RuleSet/BackendConfig/PriceRule/TaskEvent/Task/Output/CostLedger；本地 JSON 存储（原子写）；采用版唯一真源、资产版本锁定做成代码约束。
- **创作层**：txt/JSON/Markdown 剧本导入与自动拆镜、缺字段报告；权威 H3 三段式 + R2V 六段式提示词 Schema 引擎、`(S1)/(S2)↔voice_id` 绑定；规则集版本化与热加载；LLM 可插拔桩（外部算力后置）。
- **控制面**：任务状态机、本地调度器（幂等/租约/失败重派/死信）；累计成本闸门（预估+已花+在途≤上限，冻结/结算/释放，可配置单价表）；完整命名空间失败码表 + 中文诊断 + 错误归一化。
- **后期成片**：SRT 字幕生成、裁头杂音参数、拼接清单、片尾 CTA、后端无关升清抽象（ffmpeg 可选）。
- **算力适配层**：统一 Backend 接口、后端注册表与切换、cloud_api/local_comfyui/cloud_gpu 三种桩（真实调用后置）。
- **工程能力**：零依赖测试运行器（81 用例）、一键环境自检 doctor.py、原创示例剧本、端到端演示。

### Notes
- 外部算力（H3/LLM API、ComfyUI、云GPU）的真实调用全部后置；当前为接口一致的可插拔桩。
- 仓库不含任何模型权重、素材、字体、音乐或受版权保护文本。
