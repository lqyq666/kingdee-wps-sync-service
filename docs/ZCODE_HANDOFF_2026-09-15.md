# Zcode 继续开发交接：金蝶 ERP → WPS 365 同步服务

更新时间：2026-09-15

当前分支：`main`

服务实现基线：`ecc94aff84e20ea9427b8b3979c6acba60012347`（`feat: align WPS client with self-built app API`）

请从当前 `origin/main` 检出接手；该分支包含本交接文档及其后续格式修正。
公开仓库：<https://github.com/lqyq666/kingdee-wps-sync-service>

## 交接结论

这是一个**已在 mock 环境验证的单向同步框架**，不是已完成真实客户联调的项目。

- 已实现：同步编排、增量 checkpoint、overlap window、稳定 source key、content hash、PostgreSQL 状态、批量 create/update、指数退避 + jitter、DLQ/replay、任务锁、dry-run、JSON 日志、Prometheus、健康检查、webhook、Docker/Compose、mock Kingdee/mock WPS、WPS 自建应用协议客户端与可选 KSO-1。
- 已验证：Python 编译、17 个 pytest、Docker build；GitHub Actions CI 通过，且 CI 只使用 mock mode。
- 未验证：真实 Kingdee API、真实 WPS API、真实字段映射、真实权限/签名/限流/分页/错误码。
- 当前真实联调状态：**BLOCKED / REQUIRES REAL CREDENTIALS AND METADATA**。

不得把 mock 验收、单测或 HTTP 200 表述为“真实 API 已联调通过”。

## 安全红线

1. 不读取用户剪贴板，不要求用户在聊天中粘贴 AppSecret、token、私钥、客户数据或抓包内容。
2. `.env`、数据库文件、缓存、日志、证书文件均已忽略；保留 `.env.example` 作为模板。不得提交任何真实凭据。
3. 生产凭据只由用户在部署环境的 Secret Manager 或本机未跟踪的 `.env` 注入。Docker Compose 的 `.env` 是本地便利配置，不是加密 Secret Vault。
4. 已在聊天中暴露且具备权限的凭据应由其持有人在 WPS/金蝶后台轮换；不要把它们写回仓库。
5. 不要对真实 WPS 表直接做首次试写。必须先使用管理员授权的沙箱/隔离测试表和最小脱敏样本。

## 当前架构与代码地图

```text
Kingdee OpenAPI
    -> app.integrations.RealKingdeeClient / MockKingdeeClient
    -> app.service.SyncEngine
    -> PostgreSQL (sync_records, sync_checkpoints, sync_runs, dead_letters, task_locks)
    -> app.integrations.WpsOpenApiClient / MockWpsClient
    -> WPS 365 DBSheet: 销售数据详情表
    -> 高层 / 区域经理 / 客户经理驾驶舱
```

| 责任 | 文件 | 说明 |
| --- | --- | --- |
| 环境变量与 fail-fast 校验 | `app/config.py` | `KINGDEE_MODE` / `WPS_MODE` 不是 `mock` 时缺项会报 `BLOCKED`。 |
| 适配器 | `app/integrations.py` | Mock、通用待确认的 Kingdee adapter、WPS token/create/update/KSO-1。 |
| 核心同步状态机 | `app/service.py` | checkpoint、锁、hash 分类、批处理、重试、DLQ、审计。 |
| 17 字段映射 | `app/mapping.py` | WPS 字段固定；真实 source 字段刻意全部 `None`。 |
| 数据模型 | `app/models.py` | SQLAlchemy 表定义；当前以 `Base.metadata.create_all()` 初始化。 |
| HTTP 接口 | `app/main.py` | `/health/live`、`/health/ready`、`/metrics`、`POST /sync`、`POST /dlq/replay`。 |
| CLI | `app/sync.py` | `python -m app.sync [--dry-run]`，可被 cron/Kubernetes Job 调用。 |
| 重试、hash、日志、指标 | `app/retry.py`、`app/hashing.py`、`app/logging.py`、`app/metrics.py` | 均为无云厂商依赖实现。 |
| Compose/CI | `docker-compose.yml`、`.github/workflows/ci.yml` | Compose 仅作本机 mock 演示；CI 为 Python 3.12 + mock。 |

## 数据契约（不可猜测）

### 固定的 17 个 WPS 业务字段

`日期`、`单据编号`、`客户`、`单据状态`、`物料名称`、`实发数量`、`仓库`、`产品类别`、`销售品类`、`品牌`、`县城`、`大区经理`、`客户经理`、`大区`、`出厂价`、`含税单价`、`销售单位`。

Mock 只使用 `demo_source_field`。真实金蝶字段不能猜：收到正式字段字典、类型、单位、枚举和空值规则后，才填写 `FIELD_MAPPINGS[*].real_source_field`，并新增每个字段的映射测试。

### WPS 表的技术字段

在“销售数据详情表”增加并对三个驾驶舱隐藏以下**文本字段**：

| 字段 | 作用 |
| --- | --- |
| `_sync_key` | 稳定跨系统唯一键，当前格式 `source_document_id:source_line_id`。 |
| `_source_modified_at` | 金蝶源记录最后修改时间，含时区。 |
| `_sync_hash` | 17 个业务字段 canonical JSON 的 SHA-256。 |

它们不属于驾驶舱指标，不能在驾驶舱显示或聚合。

### 源记录最低要求

同步引擎要求每行都有 `source_document_id`、`source_line_id`、`modified_at`。实施方还必须定义删除、红冲、作废、时区、精度、重复行和分页语义；当前代码不会自行推测这些业务规则。

## 已实现的 WPS 协议边界

`WpsOpenApiClient` 已按公开 WPS 365 自建应用文档编码下列请求，但尚未发出真实请求：

1. `POST https://openapi.wps.cn/oauth2/token`，表单参数 `grant_type=client_credentials`、`client_id`、`client_secret`。
2. `POST /v7/coop/dbsheet/{file_id}/sheets/{sheet_id}/records/create`。
3. `POST /v7/coop/dbsheet/{file_id}/sheets/{sheet_id}/records/update`。
4. 每条记录的 `fields_value` 是 JSON 字符串；远端返回的 record id 写入本地 `SyncRecord.remote_record_id`。
5. `WPS_KSO_SIGNING_ENABLED=true` 时附加 `X-Kso-Date` 与 `X-Kso-Authorization: KSO-1 ...`；默认关闭，只有 WPS 管理员确认需要“接口签名”时才开启。

`WPS_BASE_URL` 与 `WPS_TOKEN_URL` 有官方默认值；真实 WPS 模式必填的是 `WPS_APP_ID`、`WPS_APP_SECRET`、`WPS_FILE_ID`、`WPS_SHEET_ID`。

## 已知生产缺口：下一位开发者优先处理

### P0：真实联调前的授权与数据契约

1. 获取金蝶沙箱或只读生产 API 的授权方式、base URL、form id、增量接口、分页方式、请求样例和脱敏响应。
2. 获取金蝶 17 个字段的正式映射、稳定行主键、修改时间、删除/红冲语义。
3. 获取 WPS 365 自建应用管理员授权、`kso.dbsheet.readwrite` scope、目标隔离测试表的 `file_id`/`sheet_id`、签名开关、正式 API 错误码和配额说明。
4. 在不写入 Git 的本地配置填入凭据。缺失任何必填项时，应用必须保持 `BLOCKED`，而不是构造请求。

### P0：补齐跨系统 crash recovery

当前本地幂等性是可靠的：已写入 `sync_records` 的相同 `_sync_key` + `_sync_hash` 会被 skip，变更会 update。

但存在一个真实生产边界：若 WPS create 成功后、`SyncRecord` 本地事务提交前进程崩溃，下一次执行没有 WPS 远端回查能力，可能再次 create。同样，WPS 表中的三个技术字段目前只会写入，`WpsOpenApiClient` 尚未实现按 `_sync_key` 查询和 reconciliation。

在声称“跨系统 crash recovery”前，必须：

1. 根据客户已确认的 WPS 查询/过滤 API，实现 `find_records_by_sync_keys()`；不要猜 endpoint 或筛选语法。
2. 在 create 前、以及重试/恢复路径中用 `_sync_key` 查询远端，发现已存在记录时回填/修复本地 `SyncRecord` 后走 update 或 skip。
3. 增加故障注入测试：模拟“远端返回成功、数据库提交前崩溃”，随后运行 reconciliation，断言远端不产生重复记录。
4. 为 remote id 缺失、远端多条同 key、查询失败、部分批次成功等情形规定 DLQ 和人工处置策略。

### P1：将真实 Kingdee adapter 替换为客户合同实现

`RealKingdeeClient` 目前只是一个显式标注“待客户确认”的占位 adapter，假设 JSON body 为 `form_id`/`modified_since`，并期待返回 `data` 或 `rows` 列表。不能直接用于客户租户。

收到合同后应实现且测试：认证/签名、分页、增量过滤、限流、超时、错误码、数据类型转换、删除/红冲与回补策略。只有 `KINGDEE_MODE=real` 且映射完成后才可做真实读。

### P1：生产化运维

- Compose 的 PostgreSQL `trust` 仅适用于本机 mock 演示；生产换成受管 PostgreSQL、独立最小权限账号、备份与恢复方案。
- 当前数据库使用 `Base.metadata.create_all()`；生产 schema 演进前引入受控 migration（例如 Alembic）及回滚策略。
- 确认 WPS 单批上限/每应用限流后，将 `SYNC_BATCH_SIZE` 限制在合同上限内，并按需要加入主动 rate limiter。
- 把结构化日志、Prometheus `/metrics`、webhook 告警接入现有监控；webhook URL 必须由 Secret Manager 注入。
- 明确调度频率、任务锁 TTL、告警升级、DLQ 留存期和 replay 审批流程。

## 可复现验证

### 无凭据 mock 验收

```powershell
Copy-Item .env.example .env
docker compose up -d --build
curl.exe -i http://localhost:28080/health/live
curl.exe -i http://localhost:28080/health/ready
docker compose exec sync-service python -m app.sync --dry-run
docker compose exec sync-service python -m app.sync
docker compose exec sync-service python -m app.sync
```

预期：两个 health 均为 200；首次正式运行 `fetched > 0`、`created > 0`、`status=SUCCESS`；重复运行 `created=0`、`updated=0`、`skipped>0`。

如需重新证明“干净数据库”的首次运行，只能在确认目标是本机 mock volume 后执行 `docker compose down -v`；绝不能对生产数据库或未知 volume 使用该命令。

### 本地与 CI 检查

```powershell
python -m compileall app
pytest
docker compose config
docker build .
```

最近一次本地证据：`compileall` 成功、`17 passed`、`docker build .` 成功；本机 mock 服务 `/health/live` 与 `/health/ready` 返回 200。最近一次 GitHub Actions CI 成功：<https://github.com/lqyq666/kingdee-wps-sync-service/actions/runs/34923002350>。

CI 文件为 `.github/workflows/ci.yml`，显式设置 `KINGDEE_MODE=mock` 和 `WPS_MODE=mock`，不得加入真实外部 Secret 或真实 API 调用。

## 建议的真实联调顺序

1. 先完成 WPS 目标测试表、17 个业务字段、3 个隐藏技术字段、自建应用 scope/管理员授权。
2. 将凭据由用户安全注入本机或测试部署环境，启动时验证配置；不在终端回显、不在日志记录。
3. 使用最小测试数据分别验证 WPS token、权限和单条 create；保留脱敏审计证据，不保存 token/客户数据。
4. 完成金蝶请求合同和逐字段 mapping review，先在 `KINGDEE_MODE=real` + `WPS_MODE=mock` 做只读抽取/映射验证。
5. 在隔离 WPS 表验证新建、重复投递、字段变更 update、分页、overlap、限流和断点恢复。
6. 实现并验收远端 `_sync_key` reconciliation 后，演练“远端成功、本地未提交”的 crash recovery。
7. 通过 DLQ replay、权限失败、网络失败、告警和回滚演练后，才可将真实联调状态改为 PASS。

## 给 Zcode 的第一条任务指令

```text
接手 D:\金蝶-wps同步，从当前 origin/main 检出，先阅读 docs/ZCODE_HANDOFF_2026-09-15.md、README.md、docs/INTEGRATION_STATUS.md 和 docs/REAL_INTEGRATION_REQUIREMENTS.md。服务实现基线为 ecc94aff84e20ea9427b8b3979c6acba60012347；先执行 git status、python -m compileall app、pytest、docker build .。严禁读取或提交 .env/Secret，严禁声称真实金蝶或 WPS 已通过。优先实现并测试 WPS 按 _sync_key 的远端 reconciliation；只有拿到客户确认的 WPS 查询合同和脱敏样本后才编码真实查询。随后根据客户金蝶合同替换 RealKingdeeClient、填写经过 review 的 17 字段映射，并完成隔离测试表的真实验收证据。
```
