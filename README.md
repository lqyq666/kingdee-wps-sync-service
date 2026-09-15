# 金蝶 ERP → WPS 365 多维表格单向同步服务

**Implemented:** production sync framework, mock integration, retry/checkpoint/DLQ/metrics.

**Not yet verified:** real Kingdee API, real WPS API.

**Blocked by:** Kingdee credentials and metadata; WPS credentials and file/sheet IDs.

这是一个可直接演示和复现的单向同步框架：金蝶 OpenAPI → Python 同步服务 → PostgreSQL → WPS 365 多维表格 → 销售数据详情表 → 高层、区域经理、客户经理驾驶舱。仓库目前仅以确定性的 mock 金蝶与 mock WPS 适配器完成端到端验证；不会伪造真实 API 调用结果。

## 快速演示（clean checkout）

Docker Desktop 启动后，在仓库根目录运行（Compose 中 PostgreSQL 的 `trust` 认证仅供本机 mock 演示；生产环境必须使用独立的受管数据库凭据）：

```powershell
Copy-Item .env.example .env
docker compose up -d --build
curl.exe -i http://localhost:28080/health/live
curl.exe -i http://localhost:28080/health/ready
docker compose exec sync-service python -m app.sync --dry-run
docker compose exec sync-service python -m app.sync
docker compose exec sync-service python -m app.sync
```

Linux/macOS 可把第一行替换为 `cp .env.example .env`。宿主机端口可在本地 `.env` 用 `HOST_PORT` 调整（容器内始终为 `8080`）。预期：健康检查均为 `200`；第一次正式 mock sync 返回 `status: SUCCESS`、`fetched > 0`、`created > 0`；第二次返回 `status: SUCCESS`、`created: 0`、`updated: 0`、`skipped > 0`。最后两次输出证明 PostgreSQL 中的稳定 source key 和 content hash 实现了幂等同步。

停止演示环境而保留本地数据：

```powershell
docker compose down
```

## 服务接口与运行方式

- `GET /health/live`：进程存活检查。
- `GET /health/ready`：PostgreSQL 连通性检查。
- `GET /metrics`：Prometheus 指标。
- `POST /sync?dry_run=true`：不写远端或同步状态的预演。
- `POST /sync`：执行一次同步。
- `POST /dlq/replay`：重放尚未成功的死信记录；不会推进 source checkpoint。

容器命令 `python -m app.sync` 适合 cron、Kubernetes Job 或调度器。任务锁避免同一销售明细流并发执行。

## 已实现的同步保障

- 增量 checkpoint 与 15 分钟可配置 overlap window。
- `source_document_id:source_line_id` 组成稳定 `_sync_key`。
- 规范 JSON content hash，重复数据跳过、变更数据批量 update、新数据批量 create。
- PostgreSQL 持久化同步记录、任务锁、运行审计、checkpoint 与 DLQ。
- 指数退避 + full jitter 重试，失败写入 DLQ，可显式 replay。
- 跨系统对账：正式写入前按隐藏字段 `_sync_key` 回查 WPS（官方 `records/list_by_page` 接口），远端已存在但本地未提交的行会被收编为 update/skip 而不是重复 create；远端同 key 多行、无 id 等冲突进入 DLQ 等待人工处置。
- JSON 结构化日志、Prometheus 指标、liveness/readiness 与可选 webhook 失败告警。
- WPS 365 自建应用 token 内存缓存与可选 KSO-1 签名，按 WPS 官方 `client_credentials`、`X-Kso-Date`、`X-Kso-Authorization` 合同实现；真实租户仍未发起调用。

## 配置与真实联调边界

默认 `.env.example` 是 mock 模式。只有当 `KINGDEE_MODE=real` 或 `WPS_MODE=real` 时，服务才会要求相应全部配置；缺失时在启动或同步时清晰返回 `BLOCKED / REQUIRES REAL CREDENTIALS and metadata`，不会发起不完整的真实请求。

不要提交 `.env`、数据库文件、token、私钥或客户导出的元数据。生产环境的 PostgreSQL 密码、webhook URL 和所有 API 凭据必须在部署平台的 Secret Manager 中注入。

真实联调所需项、字段映射交接和 WPS 隐藏恢复字段见：

- [集成状态](docs/INTEGRATION_STATUS.md)
- [真实联调准备清单](docs/REAL_INTEGRATION_REQUIREMENTS.md)

## 本地质量检查

```powershell
python -m pip install -r requirements-dev.lock
python -m pip install --no-deps -e .
python -m compileall app
pytest
docker compose config
docker build .
```

CI 使用 Python 3.12 和 mock mode，不读取真实外部 Secret，也不调用 Kingdee/WPS 生产端点。
