# 集成状态

更新时间：2026-09-14

## 可验证完成：Mock 环境

| 能力 | 状态 | 证据边界 |
| --- | --- | --- |
| 同步编排、增量 checkpoint、overlap window | Implemented | 单元测试与 Docker mock 演示 |
| 稳定 source key、content hash、幂等跳过 | Implemented | PostgreSQL `sync_records` 与重复 mock 运行 |
| 批量 create/update、重试与 jitter | Implemented | 代码与单元测试 |
| DLQ / replay、任务锁、dry-run | Implemented | 单元测试与 HTTP/CLI 接口 |
| JSON 日志、Prometheus、health/readiness、webhook | Implemented | 服务实现；webhook 仅在配置后调用 |
| Docker / Docker Compose / GitHub Actions | Implemented | 本仓库配置 |

## 未验证：真实 API

| 集成 | 状态 | 原因 |
| --- | --- | --- |
| Kingdee OpenAPI | **BLOCKED / REQUIRES REAL CREDENTIALS** | 没有客户 API 授权、端点合同、业务单据/字段元数据 |
| WPS 365 OpenAPI | **BLOCKED / REQUIRES REAL CREDENTIALS** | 没有 AppID、AppSecret、file_id、sheet_id、端点合同 |
| KSO-1 | **BLOCKED / REQUIRES REAL CREDENTIALS** | 已保留可选签名钩子；真实 canonicalization 尚未拿到官方租户文档验证 |
| 真实字段映射 | **BLOCKED / REQUIRES REAL METADATA** | 17 个 WPS 目标字段固定，所有真实 source 字段刻意留空，禁止猜测 |

因此，本仓库可以被表述为“mock 已验证的生产级同步框架”；禁止把 mock 验收描述成真实 API 联调通过。

## 17 个业务字段

`日期`、`单据编号`、`客户`、`单据状态`、`物料名称`、`实发数量`、`仓库`、`产品类别`、`销售品类`、`品牌`、`县城`、`大区经理`、`客户经理`、`大区`、`出厂价`、`含税单价`、`销售单位`。

其目标定义在 `app/mapping.py`。仅 `demo_source` 用于 mock；`real_source_field` 均为 `None`，收到客户字段字典后需经过业务确认与测试再填写。
