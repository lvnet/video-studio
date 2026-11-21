# 架构说明与边界

## 三层架构

```
创作层   剧本 → 拆镜 → 参考资产 → 提示词扩写 → 成片
控制面   任务状态机 / 调度 / 成本闸门 / 资产版本 / 合规审计
算力适配层  GenRequest → Backend（cloud_api / local_comfyui / cloud_gpu）
```

上层只依赖 Backend 抽象，切换后端不改剧本、提示词和项目数据。

## 关键类型区分（已修复的混淆）

| 类型 | 所在模块 | 职责 |
|---|---|---|
| `GenRequest` | `compute.base` | 发给算力后端的一次生成请求（task_id/step/payload） |
| `Task` | `models.entities` | 控制面持久化的任务记录（id/shot_id/kind/status/成本三段） |

历史上算力层的请求对象也曾叫 `Task`，与持久化实体同名易误用。现已更名为
`GenRequest`；`compute.Task` 保留为向后兼容别名（指向 `GenRequest`）。新代码请用
`GenRequest`。

## 本地持久化边界（重要）

当前 `LocalStore` 用本地 JSON 文件做事实源，每个实体类型一个文件，写入采用
「临时文件 + 原子替换」。这适合**单用户、单机、自用**场景，但有明确边界：

- **无事务**：一次 `add/update` 只保证单文件原子，跨文件的一致性由业务方法
  （如 `adopt_output`）按序执行保证，不是数据库级事务。
- **无并发锁**：同一进程内多写者可能相互覆盖；多进程同时写同一目录会损坏数据。
- **无索引/查询优化**：`find` 是全量扫描，数据量大时性能下降。
- **无备份/恢复机制**：需使用者自行备份数据目录。

因此：

- 自用、单机、单进程：直接使用 `LocalStore`，零依赖，clone 即跑。
- 多用户 / 多机 / 需要并发与事务：必须按方案换成 PostgreSQL + Redis。

### 迁移到 PostgreSQL 的路径

`LocalStore` 的方法（`add/get/update/delete/all/find` + 业务方法）就是 Repository
接口。实现同名接口的 `PostgresStore` 即可替换，上层业务无需改动：

1. 用 SQLAlchemy 把 `models.entities` 的 dataclass 映射为表（方案 05 已给出表结构）。
2. `add/get/update/delete/find` 改为 SQL 操作，事务用数据库事务。
3. `adopt_output` / `lock_asset_version` / 成本账本改为事务内操作，保证原子性。
4. 租约从内存字典改为 Redis sorted set + Lua（方案 09）。
5. 生成物存对象存储（MinIO / 本地目录），DB 只存 `storage_uri` + `sha256`。

## 合规边界

- 版权守卫、内容安全过滤是**本地最小规则 + 归一化抗绕过**，提高门槛但非绝对保证。
- 审计日志是 append-only + 哈希链，能检测篡改但不阻止有文件系统权限者修改。
- 真正的零风险需要：供应商内容安全 API、生成结果留痕审计、人工复核、用户协议免责。
- 详见 COMPLIANCE.md。

## 算力适配层（接口已做实，默认桩）

三种后端接口全部做实，但**默认走内存桩**：零外连、零成本、零版权风险。
要接真实算力须显式关闭 `dry_run` 并注入 `HttpClient`；上层业务无感。

- `Backend` 统一契约：`capabilities / health / estimate / submit / poll / fetch / cancel / normalize_error`。
- 传输层 `HttpClient` 与协议逻辑解耦：默认 `StubHttpClient`（内存模拟），
  真实模式用 `UrllibHttpClient`（标准库，无第三方依赖），测试用 `FakeHttpClient` 断言协议。
- **ComfyUI 全套 HTTP API**：`GET /system_stats`（健康）、`GET /object_info`（能力发现）、
  `POST /prompt`（提交 workflow graph）、`GET /queue`、`GET /history/{id}`（轮询）、
  `GET /view`（产物字节）、`POST /upload/image`（multipart 参考图）、`POST /interrupt`（取消）。
  workflow 由 `build_workflow()` 生成，节点类型仅为默认模板，可用 `payload['workflow']` 覆盖
  （不硬编码模型权重，不下载任何权重）。产物落本地目录并计算 sha256，返回规范化 `storage_uri`。
- **Cloud API**：鉴权只从 `credentials_ref` 指向的环境变量读取（密钥不明文入库/下发），
  请求/响应字段映射、云端任务状态归一、产物 `storage_uri + sha256`、可配置计价（按秒/分辨率）。
- **Cloud GPU**：Agent 注册（能力标签 `mode/vram/upscale`）、心跳、按 mode 能力匹配派单
  （r2v 只派给声明 r2v 的节点，避免回退 FL2VA 丢身份锁定）、租约、产物由 Agent 推对象存储。
- `fetch()` 桩模式推进任务到完成并返回带 64 位 sha256 的 `Artifact`；任务不存在或云端产物
  无 `storage_uri` 时明确报错，不静默返回假产物。
- 失败经 `normalize_error()` 归一到统一 `FailureCode`（transient / invalid_params /
  out_of_budget / node_dead / unknown），供状态机决策重试/死信。
- 注册表 `BackendRegistry` 支持按 kind 注册/切换/缓存、从 `BackendConfig` 配置驱动构造、
  记录激活后端，`status()` 输出可下发的全景视图（不含密钥明文）。

**安全边界**：密钥只存环境变量引用名，`ComputeConfig.to_public_dict()` 与
`/api/compute/configs` 绝不下发密钥明文；本平台不下载/打包任何模型权重，
所有示例提示词原创，零版权素材复制。
