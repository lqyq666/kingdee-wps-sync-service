# 真实 Kingdee / WPS 联调准备清单

## 当前状态：BLOCKED（WPS 侧为版本墙，非凭据问题）

2026-09-15 真实联调结论：open.wps.cn 自建应用凭据有效、scope 配置成功、应用与目标文件同属「青云协序」企业；但该企业为体验版且未认证，企业文档类接口全部被 `interface_company_doc` 拒绝。社区证据指向需付费企业高级版。可行的开发路径是切换到金山文档开放平台（developer.kdocs.cn，`/api/v1/openapi/dbt/*` 轻维表/多维表格 API，AK/SK 签名鉴权），两个平台应用不通用；生产环境若采购 WPS 365 企业高级版，可切回 `WPS_PROVIDER=wps365`。

当前仓库 `.env`/`.env.example` 已预留 `WPS_PROVIDER`（`wps365`|`kdocs`）与 `KDOCS_APP_ID`/`KDOCS_AK`/`KDOCS_SK` 占位。

不要把 `.env`、token、私钥、抓包内容或客户数据提交到 Git，也不要在聊天中粘贴 Secret。

## Kingdee 需由客户/实施方提供

1. 沙箱或只读生产 API 的 base URL、授权方式、App ID/Secret 与访问 IP 白名单要求。
2. 销售出库/销售明细的准确 form id、增量查询 endpoint、请求示例和脱敏响应示例。
3. 17 个业务字段的真实 source field 名称、类型、枚举、空值与单位规则。
4. 可靠的业务主键/行标识与最后修改时间字段，确认时区、精度和删除/冲销语义。
5. KSO-1（若必需）的官方 canonicalization、签名字符串、header 名称和验签样例。

服务会在 `KINGDEE_MODE != mock` 时检查 `KINGDEE_BASE_URL`、`KINGDEE_APP_ID`、`KINGDEE_APP_SECRET`、`KINGDEE_FORM_ID`、`KINGDEE_SYNC_URL`、`KINGDEE_SOURCE_ID_FIELD` 与 `KINGDEE_SOURCE_MODIFIED_AT_FIELD`。即使这些变量齐全，字段映射仍需以客户元数据更新，未完成前真实同步保持阻塞。

## WPS 需由客户管理员提供

1. WPS 365 企业自建应用的 AppID/AppSecret、`kso.dbsheet.readwrite` scope（回查用到的 `records/list_by_page` 要求 `kso.dbsheet.read` 或 `readwrite`，以及目标文件的读权限）、管理员授权状态，以及是否开启“接口签名”。
2. 销售数据详情表的 `file_id`、`sheet_id`，以及创建/更新记录的正式文档和脱敏成功响应。当前官方自建应用 token 使用 `POST https://openapi.wps.cn/oauth2/token` + `client_credentials`；记录写入使用 WPS 365 OpenAPI 的 DBSheet create/update 路径。拿到 `file_id` 后，`python -m app.smoke sheets --file-id <file_id>` 会调用官方 [`GET /v7/coop/dbsheet/{file_id}/schema`](https://open.wps.cn/documents/app-integration-dev/wps365/server/dbsheet/get-schema)（只读，`kso.dbsheet.read`）列出全部数据表与字段，直接定位 `sheet_id`；`python -m app.smoke fields ...` 会核验 20 个目标字段是否齐全、技术字段是否为文本类型。
3. API 的批量限制、限流策略、错误码、请求幂等策略。按字段查询记录已按官方 [`records/list_by_page`](https://open.wps.cn/documents/app-integration-dev/wps365/server/dbsheet/records/list-record-by-page) 实现（`filter.criteria` + `Equals` 文本匹配 + `page_num`/`page_size` 分页）；仍需管理员确认单次请求 `criteria` 数量上限（代码默认每组 50 个），必要时下调。
4. 三个驾驶舱仅消费业务字段的确认；隐藏技术字段不得在驾驶舱展示或聚合。
5. KSO-1（如要求）的官方签名规范和测试租户。

服务会在 `WPS_MODE != mock` 时检查 `WPS_APP_ID`、`WPS_APP_SECRET`、`WPS_FILE_ID` 与 `WPS_SHEET_ID`。缺一个即明确失败，不会尝试真实请求。WPS 官方基础地址与 token 地址已有安全默认值；`WPS_KSO_SIGNING_ENABLED=true` 时，服务以 AppSecret 生成官方 KSO-1 签名。

## WPS 销售主表必须新增的隐藏文本字段

在“销售数据详情表”增加以下字段，并对高层、区域经理、客户经理三个驾驶舱隐藏：

| 字段 | 类型 | 用途 |
| --- | --- | --- |
| `_sync_key` | 文本 | 跨系统稳定唯一键；建议值为 `source_document_id:source_line_id`。 |
| `_source_modified_at` | 文本 | 保存源系统最后修改时间及其时区，用于 checkpoint、overlap 与冲突排查。 |
| `_sync_hash` | 文本 | 业务字段 canonical hash，用于判断重复投递与内容变更。 |

这三个字段用于 crash recovery 和跨系统幂等恢复，不能作为驾驶舱指标。它们还能让运维在远端已写入、服务在本地状态提交前中断时，按 `_sync_key` 回查并安全恢复。

## 真实联调验收顺序

1. 使用客户提供的沙箱和最小只读样本，完成 17 个字段逐项 mapping review。
2. 验证 token、签名、分页、增量过滤、时区和错误码，不把 API 响应或 Secret 写进仓库。
3. 对一批新数据验证 create；重复投递验证无重复；变更一条数据验证 update。
4. 人为中断本地状态提交，按 WPS 隐藏字段进行恢复演练；测试 DLQ replay。
5. 通过限流、网络失败、权限失败、断点恢复和监控告警验收后，才可声明真实 API 联调通过。
