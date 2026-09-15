# 集成状态

更新时间：2026-09-15

## 可验证完成：Mock 环境

| 能力 | 状态 | 证据边界 |
| --- | --- | --- |
| 同步编排、增量 checkpoint、overlap window | Implemented | 单元测试与 Docker mock 演示 |
| 稳定 source key、content hash、幂等跳过 | Implemented | PostgreSQL `sync_records` 与重复 mock 运行 |
| 批量 create/update、重试与 jitter | Implemented | 代码与单元测试 |
| DLQ / replay、任务锁、dry-run | Implemented | 单元测试与 HTTP/CLI 接口 |
| 按 `_sync_key` 回查 WPS 的跨系统对账与 crash recovery | Implemented (mock) | 故障注入测试：远端写入成功、本地提交前崩溃，再次运行与 DLQ replay 均不产生重复行 |
| JSON 日志、Prometheus、health/readiness、webhook | Implemented | 服务实现；webhook 仅在配置后调用 |
| Docker / Docker Compose / GitHub Actions | Implemented | 本仓库配置 |

## 未验证：真实 API

| 集成 | 状态 | 原因 |
| --- | --- | --- |
| Kingdee OpenAPI | **BLOCKED / REQUIRES REAL CREDENTIALS** | 没有客户 API 授权、端点合同、业务单据/字段元数据 |
| WPS 365 OpenAPI | **BLOCKED / REQUIRES REAL CREDENTIALS** | 2026-09-15：自建应用凭据已由管理员在本机 `.env` 注入，`POST /oauth2/token`（`client_credentials`，未开启接口签名）已在真实租户成功返回 access_token；隔离测试表的 `file_id`、`sheet_id` 尚未提供，create/update/list_by_page 均未发起真实调用 |
| WPS `records/list_by_page` 回查 | **BLOCKED / REQUIRES REAL CREDENTIALS** | 已按 [官方文档](https://open.wps.cn/documents/app-integration-dev/wps365/server/dbsheet/records/list-record-by-page) 编码 `filter.criteria` + `Equals` + 页码分页；未在真实表上验证筛选语义与 criteria 数量上限 |
| KSO-1 | **BLOCKED / REQUIRES REAL CREDENTIALS** | 已按 WPS 官方签名说明实现；尚未持真实应用凭据发送请求验证 |
| 真实字段映射 | **BLOCKED / REQUIRES REAL METADATA** | 17 个 WPS 目标字段固定，所有真实 source 字段刻意留空，禁止猜测 |

因此，本仓库可以被表述为“mock 已验证的生产级同步框架”；禁止把 mock 验收描述成真实 API 联调通过。

## 跨系统对账规则

同步在正式写入前，对所有本地没有记录的行、以及本地有记录但缺少 `remote_record_id` 的行，按 `_sync_key` 批量回查 WPS（每组最多 50 个 `Equals` 条件，`OR` 组合，`page_size=1000` 翻页）。dry-run 不回查，只基于本地状态估算。

| 远端状态 | 处理 | 计数 |
| --- | --- | --- |
| 未找到 | 正常 create（缺 id 的本地行复用同一行写回新 id） | `created` |
| 找到 1 行且 `_sync_hash` 与当前一致 | 回填本地 `sync_records`，不写远端 | `skipped` |
| 找到 1 行但 `_sync_hash` 不一致 | 回填本地后按远端 id update | `updated` |
| 找到多行，或行缺少 `id` | 不写远端，写入 DLQ，`reason` 以 `remote_reconciliation_conflict` 开头 | 不计入，`kingdee_wps_sync_dlq_records_total` +1 |
| 回查请求失败 | 按 `SYNC_MAX_ATTEMPTS` 重试后整轮 FAILED，未完成的行进入 DLQ | `FAILED` |

回填本地记录时保存的是远端当前的 `_sync_hash`，而不是本次计算值：若回填后、update 前再次崩溃，下一轮仍会判定为变更并补发 update。DLQ replay 使用同一套回查规则；冲突条目保持 `PENDING` 并记录原因，需人工在 WPS 表中合并/删除重复行后再 replay。checkpoint 仍在本轮 SUCCESS 时推进，冲突行的完整载荷保留在 DLQ 中，不会因 checkpoint 推进而丢失。

## 17 个业务字段

`日期`、`单据编号`、`客户`、`单据状态`、`物料名称`、`实发数量`、`仓库`、`产品类别`、`销售品类`、`品牌`、`县城`、`大区经理`、`客户经理`、`大区`、`出厂价`、`含税单价`、`销售单位`。

其目标定义在 `app/mapping.py`。仅 `demo_source` 用于 mock；`real_source_field` 均为 `None`，收到客户字段字典后需经过业务确认与测试再填写。
