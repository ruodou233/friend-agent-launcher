# Friend Agent 唯一实施方案

> 截止：2026-08-01。本文是本项目唯一的实施边界、服务契约、文件计划与发布验收依据；它记录当前已落地基线、未部署模板和后续验收条件，不把未验证能力写成支持。

## 0. 主审裁决后的范围

- **V1A 目标（未部署）**：Claude macOS + 小型公网 VPS + LowcostAI 单主线路 + 手工发 Key + New API 只读余额 + 人工微信幂等加额 + 可恢复配置 + 一位朋友现场 E2E。
- **V1B**：只有 V1A 的 Claude macOS 现场 E2E 完成后，才做 Claude Windows；不先做通用四平台安装器。
- **Codex**：独立 P0 研究，可与 V1A 并行，但不阻塞 V1A、不构建发行包、不上传、不宣称支持；任意 custom provider、免 OpenAI 登录、历史可见性未完成真机验证前均是 no-go。
- **后续**：OpenAI Next 备线路、第三方直连/可信 Provider 预设、公开签名与更新链路均不进入 V1A，按发布矩阵后置。
- VPS、余额查询和三类入口是用户明确需求，不能因收敛范围删除；自动支付、自动邀请结算、第二套余额账本、动态路由控制台和长期双节点隧道不做。

当前阶段只是**现场陪同安装内测**。只有在某个产品×系统的**对应安装适配器通过发布矩阵**且真实 P0 都完成后，才可说“下载→官方安装→粘贴 Key”；此前只能说“已装官方 App 后粘贴 Key”。

### 0.1 当前交付基线与未验证边界

- 仓内已有可运行的 dependency-free `friend-gateway` `reference/mock`：它实现合同中的四条 Friend 路径，`/healthz` 仅供容器健康检查；无凭据测试覆盖严格 wire、Bearer Key 服务端绑定、catalog/balance 和单次 Messages 转发。
- `new-api-deployment/` 已把 Caddy 的四条允许路径接到 `friend-gateway`，并保留 mock fixture；它仍是未部署模板，不是公网服务或安装包。
- `proxy` 模式只提供显式的 New API Messages/余额 adapter 边界；真实 New API catalog、真实 balance adapter、VPS→New API 映射、TLS、公网可达、流式/tool P0 和朋友设备现场 E2E 均未验证。当前没有真实固定网关证据。
- Claude macOS 仍只有本地 `candidate`；Claude Windows 与 Codex macOS/Windows 继续 `blocked`。签名分发、公证/代码签名、公开 Release 和自动更新链路也未验证。

## 1. 现状、原因与目标行为

### 1.1 修改原因

0.1.0 把网关可达、模型可用、配置写入、官方 App 安装和官方桌面验收混成一次“开始使用”，导致网页打开被当成安装、HTTP 成功被当成桌面可用、Claude Key 出现多个长期副本、恢复可能覆盖用户配置。两名独立内审已把范围收敛到可恢复的 Claude macOS 首发，并要求把服务、Key、目录、幂等和发布门禁写成可执行合同。

已落地基线移除公网 server session 状态：Rust 只在进程内生成不可预测的 `local_flow_id`，TTL 不超过 10 分钟，绝不序列化、联网、落盘或写入日志；目录与余额由 Rust 持 Bearer Key 直接调用，服务端只核验 token 摘要/元数据；账户与安装归属从 Key 元数据推导，发行门禁则按 `status` 与 P0 digest 分流。当前目录信任边界只能叫 `tls-fixed-gateway`：固定 HTTPS origin、`deny_unknown_fields`、V1A 版本前缀和 expiry；公钥签名是未来增强，当前不实现、不验证，也不以签名名义包装现有信任。

### 1.2 旧行为与已落地基线

- 旧 0.1.0 的兼容代码曾允许 Endpoint、模型和 Key 输入，并尝试探测 `/v1/models` 后直接调用；这些旧路径不构成当前 Friend Claude 的公开产品边界。
- 当前已落地流程只允许 Claude macOS candidate：前端提交一次性 Key，Rust 取得固定目录，前端只提交 `canonical_id` 与 `catalog_version`，再由 Rust 写入官方 Claude 3P 配置并清理进程内 Key；仓内 gateway 目前仅提供 reference/mock 和未验证的显式 proxy 边界。
- 当前 Rust wire schema 是固定的：`CatalogRequest` 只有 `product`、`protocol`；`CatalogResponse` 只有 `product`、`protocol`、`catalog_version`、`expires_at`、`integrity`、`catalog`、`balance`；目录项和余额字段由 `gateway.rs` 的严格反序列化定义。公有请求不接受 `account_id` 或 `install_id`，二者由 Friend Key 元数据推导。
- 当前余额使用 `amount_minor` 整数最小货币单位，并带 `currency`、`as_of`、`source=new-api`；控制面不创建第二本余额账。目录信任是 `tls-fixed-gateway`，不是签名实现。
- “打开官方获取页”仍只是外链，不是安装适配器或安装包；真实桌面流式、工具、重启、历史、MCP、项目、权限和朋友现场 E2E 尚未形成完整 P0 证据。
- VPS、公网 HTTPS/TLS、真实 New API catalog/balance adapter、人工微信充值控制面和长期服务均未部署；本地或回环 New API 只能作为开发/验证前置，不能替代朋友设备可达证据。
- CI 与 release gate 以 Node.js 22 为基线；当前只允许 Claude macOS local candidate，Codex/Windows blocked。CI 已运行 gateway 无凭据测试、合同、payment、deployment static、Rust fmt/release tests、release gate 和 source/history/artifact secret scan；这些门禁通过不等于外部 P0 通过。

### 1.3 目标用户流程与数据告知

固定流程：

`检测官方 App → 获取或选择官方包 → 校验身份 → 安装后复检 → 网关预检 → 输入 Key → 鉴权并取得目录 → 选择目录项 → 字段级备份 → 分阶段写入 → 读回校验 → 提交非敏感状态 → 打开原版 App → 真机验收`

- Friend 不允许发行版 Endpoint、任意 Provider、裸模型名或任意 Key 输入；只能使用固化 HTTPS 网关和服务端目录。进程存在、路径存在、配置写入、HTTP 预检都不是官方 App 安装或桌面可用证据。
- 首次配置前必须展示：**“我方默认不记录请求/响应正文和 Key；请求会经过我方网关和上游，上游的数据策略另行适用。额度与扣费按我方账户和 New API 记录，Key 可被撤销。请勿输入不应发送到该服务的数据。”** 用户确认后才可输入 Key。
- 配置成功后只向前端返回 catalog、balance 和状态；前端在提交后立即清空 Key 输入值（成功、失败、取消均如此），不在前端、本工具设置、日志或恢复 manifest 中另存或回显 Key、`local_flow_id` 或 Endpoint，也不能直接调用公网接口。配置成功后，Rust 按 Claude 官方 3P 静态配置把 Key 写入当前 Friend profile，本机账户可读取；除该 profile 外不创建额外 Key 副本。

## 2. 最小服务端契约与职责

### 2.1 职责边界

| 角色 | 可做什么 | 不可做什么 |
|---|---|---|
| Friend 前端 | 展示阶段、目录、状态、余额和三类入口；提交 Key 后立即清空输入值；配置命令只提交 `canonical_id` 与 `catalog_version` | 持久化、再次回显或记录 Key/`local_flow_id`，提交 Endpoint、model_ref、协议或裸模型名，直接调用公网接口 |
| Rust 后端 | 接收一次性用户 Key；用不可预测随机值生成仅进程内的 `local_flow_id`，维护不超过 10 分钟的 Key 映射；用受信目录解析配置并完成鉴权、安装、写入、读回和清理；配置成功后按 Claude 官方 3P 静态配置写入当前 Friend profile | 将 Key 或 `local_flow_id` 写入网络、Friend 设置、日志、备份、错误、命令行、发行产物或当前 Friend profile 之外的磁盘副本；接受客户端 Endpoint、模型或协议 |
| Friend 网关/控制面 | 验证 Bearer Key 的 token 摘要/元数据，返回带固定信任标记、版本和过期时间的 catalog，返回 catalog、balance、`catalog_version`、`expires_at`，转发已允许的协议，记录脱敏元数据审计 | 持久化明文 Key，接受客户端声明的 `account_id`、`install_id`、Endpoint 或模型，代替 New API 建余额真相，自动重试生成 POST |
| New API | 提供余额/用量读取和人工加额目标；提供按 Key 生命周期所需的状态查询 | 由 Friend 控制面复制一套余额 |
| 管理端 | 私网/回环访问手工发 Key、撤销和微信加额接口 | 对公网暴露管理端或数据库 |
| 官方 App | 保持原版 UI、历史、项目、MCP 和权限行为 | 被修改、注入、重签、冒充或随 Friend 一起再分发 |

### 2.2 只公开这些路径

公网 HTTPS 边缘只允许以下路径；管理接口和数据库走回环或私网，不放在 Caddy 公网路由中。

| 方法与路径 | 鉴权 | 语义 | V1 状态 |
|---|---|---|---|
| `GET /v1/friend/preflight` | 无 Key | 返回客户端版本、产品/协议是否可用；不生成、不检查余额 | reference/mock；真实公网未验证 |
| `POST /v1/friend/catalog` | Rust 使用 `Authorization: Bearer <Friend Key>` 调用 | 服务端只验证 token 摘要/元数据，返回 `catalog`、`balance`、`catalog_version`、`expires_at`；不持久化明文 Key | reference/mock；真实 catalog adapter 未验证 |
| `GET /v1/friend/balance` | Rust 使用同一 `Authorization: Bearer <Friend Key>` 调用 | 刷新并返回 New API 当前余额；前端通过 Rust 间接调用 | reference/mock；真实 balance adapter 未验证 |
| `POST /v1/messages` | Claude 3P 静态 Key | Anthropic Messages 流式/工具请求 | reference/mock；真实流式/tool P0 未验证 |
| `GET /v1/models` | — | 不属于当前公开契约；Friend 使用固定目录，不开放任意模型发现 | 关闭，待未来单独验收 |
| `POST /v1/responses` | Codex P0 后另行启用 | OpenAI Responses；必须先完成 Codex P0 | V1 关闭 |

内部接口只保留最小集合：`POST /internal/keys`、`POST /internal/keys/{token_id}/revoke`、`POST /internal/manual-recharges`、`GET /internal/manual-recharges/{business_ref}`。它们只给管理端使用，不能从公网入口访问。

### 2.3 鉴权、错误和幂等

- 所有请求使用 HTTPS；每个请求带唯一 `request_id`（HTTP 层同时映射为 `X-Request-Id`），仅用于普通追踪。鉴权只按 `token_id`、状态、产品、`install_id`、过期时间和服务端保存的摘要/哈希判断；Friend 服务不落盘、不缓存、不打印明文 Key。
- 对所有需要鉴权的 Friend 服务请求，`account_id` 永远由 Bearer Friend Key 推导；不接受客户端在请求体、Header 或路径中声明、覆盖或切换 `account_id`。
- 非 2xx 统一返回 `{ request_id, code, message, retryable }`；`message`、日志、指标和追踪不得含 Key、`local_flow_id`、完整请求体或上游敏感错误。至少固定 `AUTH_REQUIRED`、`KEY_EXPIRED`、`KEY_REVOKED`、`PRODUCT_PROTOCOL_MISMATCH`、`CATALOG_EXPIRED`、`CATALOG_UNTRUSTED`、`UPSTREAM_UNAVAILABLE`、`RECOVERY_REQUIRED`。
- V1 网关对生成 `POST` 不自动重试；客户端是否再次发起由官方 App 自己决定，网关不透明重放。收到流式字节、上游 response ID、工具状态或计费接受信号后绝不切换线路。

后续若启用备线路，必须先定义可重试错误白名单、至多一次切备且只允许发生在上游接受请求之前，并用故障注入验证 `request_id`、计费不重复、工具调用不重复、`response_id` 语义不漂移；在这些证据齐全前，OpenAI Next 不得出现在目录或生产路由。

## 3. 目录、Key 生命周期与本地流转

### 3.1 最小目录 schema

服务端目录的每个条目至少有：

```json
{
  "product": "claude",
  "protocol": "anthropic-messages",
  "catalog_version": "<version>",
  "expires_at": "<rfc3339>",
  "integrity": "tls-fixed-gateway",
  "catalog": [
    {
      "product": "claude",
      "protocol": "anthropic-messages",
      "canonical_id": "<opaque-canonical-id>",
      "model_ref": "<server-issued-model-ref>",
      "gateway_ref": "friend-fixed-gateway",
      "display_name": "<user-facing-name>",
      "capabilities": ["streaming", "tool_use"],
      "default": true,
      "catalog_version": "<version>",
      "expires_at": "<rfc3339>",
      "billing_label": "<billing-label>"
    }
  ],
  "balance": {
    "amount_minor": 0,
    "currency": "CNY",
    "as_of": "<rfc3339>",
    "source": "new-api"
  }
}
```

`CatalogRequest` 的实际 JSON 只有 `product` 和 `protocol`；公有调用不携带 `account_id` 或 `install_id`，服务端从 Bearer Friend Key 元数据推导。`CatalogResponse`、`CatalogEntry`、`BalanceResponse` 和 `BalanceSnapshot` 必须保持与 `gateway.rs` 的序列化/反序列化字段一一对应。`model_ref`、`protocol`、`gateway_ref`、`catalog_version`、`expires_at` 和 `integrity` 由固定网关返回并由 Rust 校验；`model_ref` 是内部模型引用，不是用户可填写的裸模型名，`gateway_ref` 只能解析为固化的 Friend 网关。`product`、`protocol`、能力和计费语义必须一致才可去重；仅展示名相同不能去重。目录对前端隐藏物理上游、倍率、主备和迁移策略；Friend UI 只展示一次 canonical 项。Rust 拒绝信任标记不匹配、版本不受信、已过期或与产品/协议不匹配的目录，不接受客户端提供的 Endpoint、model_ref、协议或模型。公钥签名留作未来增强，不在当前客户端伪造或验证。所有 P0 证据保存脱敏摘要及 digest，不保存 Key 或完整请求。

### 3.2 Key 生命周期合同

每条 Friend 管理记录至少包含：`account_id`、`product`、`install_id`、`token_id`、`status`、`expiry`、`revoked_at`。其中 `account_id` 由对应 Friend Key 推导，客户端不得声明或覆盖；`status` 至少为 `active/expired/revoked`；`revoked_at` 只有撤销后写入。

- `install_id` 是本地随机 UUID 或用户标签，不取硬件指纹；每个账户的产品/安装拥有独立低额度 Key，可单独撤销。
- 换机或重装必须先撤销旧 Key，确认旧状态已不可用，再发新 Key；不以覆盖旧记录代替撤销。
- “唯一副本”只表示 Friend 管理范围内、一次提交完成（`committed`）后的持久化副本，不声称操作系统、官方 App、用户备份或未知 profile 的全球唯一性。

### 3.3 Key 流转

1. 用户在 Friend 前端输入 Friend Key，提交给 Rust 后立即清空输入值；Rust 用不可预测随机值生成 `local_flow_id`，只在进程内保存 `local_flow_id → Friend Key` 映射，TTL 不超过 10 分钟。该 ID 不返回前端、不作为 IPC/HTTP 字段、不进入任何网络请求、磁盘、设置、日志、指标、错误、命令行或发行产物；HTTP `request_id` 仍是独立的普通追踪字段。
2. Rust 以 `Authorization: Bearer <Friend Key>` 调用 `POST /v1/friend/catalog`，验证固定 HTTPS origin、严格 wire schema、`tls-fixed-gateway`、`catalog_version` 和 `expires_at`，取得受信目录、余额和状态；`local_flow_id` 不随请求发送，目录快照也不接受客户端改写。
3. 配置命令的输入 payload 严格只接受 `canonical_id` 与 `catalog_version`。Rust 从当前进程内 flow context 取得 Key，在未过期且受信的目录中解析实际 `model_ref`、协议和固定网关；缺失/过期/不受信目录、未知 canonical ID，以及任何额外 Endpoint、model_ref、协议、Provider 或裸模型字段一律拒绝。
4. 取消、崩溃、超时、鉴权失败、读回失败和启动失败都清空 Rust 的进程内 Key 映射；提交成功后也清空。配置完成后再次打开 Friend，Rust 只可从 Friend 自有当前 Claude profile 读取 Key 到内存，使用同一 Bearer Key 调用 `GET /v1/friend/balance` 刷新余额后立即清空；不创建新的额外副本，读不到就提示重新输入。敏感 profile 的写入只在最终 generation path 上 `create_new`，Unix 权限为 `0600`，并 flush/sync；不使用会产生 `.friend-agent.tmp` / `.friend-agent.bak` 明文副本的通用原子写入器。

| 产品 | Friend 管理的提交后合法副本 | 禁止副本 | 恢复语义 |
|---|---|---|---|
| Claude | 官方 Claude 3P 本地 profile 的当前 Friend 代静态 gateway Key；启动刷新余额时仅读入 Rust 内存，不创建新副本；明确告知本机可读风险 | 新增/长期 Keychain、Friend 设置、恢复/备份、目录、日志、环境变量、源码、产物；旧 Keychain 仅作迁移输入并在 `COMMIT` 前保留 | 只处理 Friend 自有代；未知或用户 profile 不覆盖 |
| Codex（后续） | macOS Keychain 或 Windows Credential Manager | `config.toml`、helper 文件、Friend 设置、参数、日志和备份 | 只撤销 Friend 自有凭据/Provider/helper；P0 未过不实现发行 |

### 3.4 Claude 代际 profile 与恢复

每次 Claude 配置都以一个只含非敏感元数据的 `generation manifest` 为控制记录，至少记录 `generation_id`、父代 ID、profile 路径、Friend 所有权标记及证明摘要、产品/安装绑定、字段集合、读写前后哈希、预期版本、当前阶段、`commit_state` 和 `delete_state`；manifest 不复制 Key 或完整 profile。只有 Friend 自有且所有权可验证的 generation/profile 才能被删除或恢复，未知对象和用户 profile 一律不碰。

本次源码修复的真实边界如下：原先的 check-then-write/delete 不能称为强 CAS；现在每个 Claude 文件事务在进程 Mutex 外再持有 library 下专用非敏感 `.friend-agent.lock` 到事务结束，写入和删除的最终动作前都再次校验。该 OS 锁只约束同样打开并使用它的配合进程，不能阻止不配合的外部进程，因此仍不宣称消灭所有外部竞态；官方 App 由后端 gate 在首次快照前确认已停止，未能确认或发现异常则 fail-closed。

`RECOVERY_REQUIRED.json` 必须同时落在 library 与 `friend-generations`（manifest_dir）两处；每一处都要写入、读回比对并 sync，只有主、副两处都持久化才算成功。任一处失败都保留 `RECOVERY_REQUIRED` 终态，不能只把副路径当成静默 fallback；`ensure_no_recovery_journal` 仍检查两处，cleanup 状态 manifest 写入失败也不能被忽略，错误路径仍会尝试双 journal。journal 只包含状态、generation、阶段、哈希和原因，不写 Key。

阶段与补偿固定如下；跨文件写入不宣称为单一原子事务，也不把协作锁包装成不可伪造的外部 CAS：

| 阶段 | 进入条件与动作 | 失败补偿 / 终态 |
|---|---|---|
| `PREFLIGHT` | 校验官方 App 版本、目标路径、配置边界和当前修改状态；不写入 | 失败即停止，不删除、不覆盖 |
| `OWNERSHIP_CAPTURE` | 证明现有代的 Friend 标记、路径、产品/安装绑定和字段所有权，写入 manifest 与字段哈希 | 所有权未知或属于用户时不碰目标，停止并保留原状；需要人工处置时为 `RECOVERY_REQUIRED` |
| `WRITE_NEW_GENERATION` | 只在最终 generation path 创建新的 Friend 代，旧代和旧 Keychain 保留；profile 写入不产生工具额外临时/备份明文副本 | 写入失败只删除已证明属于 Friend 的新代；删除对象不确定则 `RECOVERY_REQUIRED` |
| `READBACK_VERIFY` | 读回并校验字段、哈希、格式、版本和 Friend 所有权 | 校验失败时按上一行清理新代；旧代仍保留，不触碰未知/用户对象 |
| `METADATA_SWITCH` | 仅把官方 metadata 切到已验证的新代 | 仅在新旧代所有权均可验证且无用户新修改时切回旧代；否则 `RECOVERY_REQUIRED` |
| `OFFICIAL_APP_VERIFY` | 启动原版 Claude，验证配置、历史、MCP、项目和权限仍在 | 同条件下补偿切回旧代；条件不满足则停止并 `RECOVERY_REQUIRED` |
| `COMMIT` | 记录新代已读回、已切换且官方 App 验收通过 | **在此阶段之前不得删除旧 Keychain 或旧 Friend 代；**提交记录完成后才允许进入删除阶段 |
| `DELETE_OLD_FRIEND_GENERATION` | 重新验证旧 generation/profile 的 Friend 所有权后，删除旧 Friend 代及其旧 Keychain 迁移记录 | 删除失败或结果不确定立即 `RECOVERY_REQUIRED`，不盲目重试；未知或用户 profile 不删除 |

旧 0.1.0 Keychain 记录只能作为迁移输入，且不得在 `COMMIT` 前删除；只有 manifest 能证明它属于 Friend 自有旧代时才可在提交后删除。任何删除结果不确定、所有权证明缺失或检测到用户新修改，都保留原对象、停止后续删除并进入 `RECOVERY_REQUIRED`；恢复只合并仍属于 Friend 的字段。

## 4. Claude 官方配置、安装适配器与路由

### 4.1 官方 3P 配置依据

保留官方本地 3P 配置表述，不把 `configLibrary` 当作未经验证的私有路径；适配器必须绑定已验 Claude 版本，并以官方文档为准：

- [Claude Desktop 3P Configuration reference](https://claude.com/docs/third-party/claude-desktop/configuration)：macOS 用户目录为 `~/Library/Application Support/Claude-3p/configLibrary/`，Windows 用户目录为 `%LOCALAPPDATA%\Claude-3p\configLibrary\`。
- [Claude Desktop 3P Gateway](https://claude.com/docs/third-party/claude-desktop/gateway)：V1 使用官方 `inferenceGatewayBaseUrl`、`inferenceGatewayApiKey` 等静态配置，实际请求走 Messages。
- 普通本地安装不能假设 `inferenceCredentialHelper` 可用；官方参考把 helper 标为 MDM-only。V1 仍用静态 Key，不把 helper 当作本地安装的隐含能力。

### 4.2 官方 App 身份适配器

每个通过 P0 的适配器都返回以下结构，不能假定只有一个 DMG、MSIX 或 EXE：

`{ path, publisher_or_team, signature, notarization_or_trust, version, package_type, update_method, support_status }`

适配器可从官方渠道或用户选择的本地官方包开始，校验真实包身份、签名/公证、版本和安装后复检；Friend 不打包官方二进制。macOS、Windows 的分发格式、安装路径和更新方式都由实测版本决定。

### 4.3 V1 路由

- V1 只有 LowcostAI 单主线路；Claude 使用 Anthropic Messages，目录和计费语义固定。网关不对生成 POST 自动重试，也不跨上游透明重放。
- OpenAI Next 从 V1 移出；在后续满足第 2.3 节的错误白名单、至多一次、上游接受前、请求 ID/计费/工具/response ID 故障注入之前，不注册、不展示、不运行。
- Dog、任意第三方裸 Key、未知模型、动态评分、常驻 relay、CC Switch 和多 Provider 面板不进入 V1。

## 5. 三类入口与人工微信幂等

Friend 首页必须保留三类入口，但能力边界固定：

1. **我方 Key**：V1 唯一可直接配置和查余额的入口；手工发 Key，按账户/产品/安装绑定和低额度控制。
2. **邀请**：只做人工登记与状态查询，未自动化前不承诺自动到账。
3. **免费第三方**：V1 只打开外链，不接受任意第三方 Key/Endpoint；未来只有受信 Provider 完成对应产品×系统 P0 后，才可做内置版本化预设并直接配置。

人工微信加额的唯一状态机为：`pending → crediting → credited | failed`。

- 数据库必须对 `request_id` 和 `business_ref` 分别设置 `NOT NULL UNIQUE` 约束（`business_ref = manual-wechat:<request_id>`），并以数据库事务保证金额、币种、账户绑定一旦进入 `pending` 不可变；同一请求或业务引用重放只能读回原记录。
- `pending → crediting` 必须是原子状态抢占：用条件更新/行锁让一个操作取得 claim 并记录 `operator_id`、claim 时间和审计信息；并发抢占失败者不得调用 New API。`credited` 是终态，不得重复执行。
- New API 超时、查询结果未知或本地与 New API 状态不一致时，记录证据后保持 `crediting`，只能人工按 `request_id` 与 `business_ref` 对账；未确认“未执行/未扣款”前不得重试。只有人工和 New API 证据共同确认原操作未执行时，才允许重新抢占一次。
- `failed` 只有在保存可核验的 New API 状态/审计证据、证明没有执行也没有扣款后才能落库；缺少该证据时保持 `crediting`，不能用失败超时猜测代替。余额只读 New API；控制面只保存申请/操作状态，不建第二本余额账。

## 6. 真机 fixture 与执行顺序

P0 必须使用干净账户或可审计的真实设备 fixture，至少保留：已有历史、MCP、项目目录、权限配置、未知 profile 和 Friend profile。验证后检查历史/项目/MCP/权限仍在，且恢复不覆盖用户新修改。

工具调用只使用固定的 `friend_p0_noop` fixture：固定输入、固定输出、无网络、无文件写入、无外部副作用；P0 不调用任意真实工具。

| 顺序 | 阶段 | 必须证据 | 依赖与结果 |
|---|---|---|---|
| 1 | P0-C-Mac | Claude macOS 官方版本/包身份；跳过 Anthropic 登录；静态 Key；Messages 流式；`friend_p0_noop` 工具；重启；历史、MCP、项目、权限、其他 profile 保留；恢复与切回官方模式 | 先由人工安装；通过后才能做 V1A 朋友现场 E2E |
| 2 | V1A 现场 E2E | 一位朋友设备、VPS、无代理可达路径、余额、手工 Key、配置/恢复、真实 UI 和失败记录 | 通过后才进入 Claude Windows |
| 3 | P0-C-Win | Windows 官方分发包身份、同等流式/工具/重启/恢复和 fixture | 仅在第 2 步完成后创建 Windows 适配器 |
| 并行 | P0-X | Codex custom provider、`auth.command`、有/无 OpenAI 登录、历史可见、Responses 流式/工具/重启/恢复 | 可并行研究，不阻塞 V1A，不构建或上传 Codex 包 |

## 7. VPS 最小运维与唯一发布矩阵

### 7.1 VPS 最小运维（目标形态，尚未部署）

- 未来生产才在小型公网 VPS 直接承载 Caddy + Friend gateway + New API + MySQL + 最小控制面；当前尚无已部署的朋友可达公网服务。仓内 Compose/Caddy 只是 reference/mock 模板。Mac mini 仅作测试、管理和加密备份。管理端、数据库和内部健康接口回环或私网，公网只公开第 2.2 节的明确路径。
- 必须测试 **VPS→上游** 的 DNS、TLS、超时、流式和工具链路，不以 Mac mini 可达替代；配置备份/恢复并演练可接受的 RPO ≤ 24 小时、RTO ≤ 4 小时。
- Named Tunnel/FRP 只可有人值守灰度，不作为长期生产双节点方案。

### 7.2 机器发布数据和 CI 规则

当前 `release-support.json` 是机器数据，不是第二份方案文档。每个产品×系统至少声明 `product`、`system`、`status`、`channel` 和 `p0_evidence_digest`；CI 可对全部源码做静态检查/单元测试，但只有 `status == "go"` 且存在 P0 digest 的组合才可生成并上传朋友发行包，`status == "candidate"` 只可生成本地现场测试包，不上传公开 artifact。

V1A 唯一允许生成的产品×系统组合是 **Claude macOS `candidate`**，且仅生成现场测试包；Codex 与 Windows 在 V1A 阶段均禁止生成、打包和上传任何候选/发行 artifact，只能保留脱敏研究记录。未来 `go` 门禁不改变这一当前边界；P0 evidence digest 对应脱敏证据摘要，不能把 Key、`local_flow_id`、完整日志或完整请求放入产物。

CI 沿用 Node.js 22；`.github/workflows/ci.yml` 已执行 gateway 无凭据测试、合同、payment、deployment static、Rust release tests、release gate 和 source/history/artifact secret scan。wrapper 环境变量检查仅是误用防护，不是不可伪造的安全边界；真正的发布依据是 CI、支持矩阵、最终产物 allowlist、secret scan 和 P0 evidence。完整 Git 历史、构建后解包产物和日志扫描是门禁证据；门禁通过不等于 VPS、公网、真实 adapter 或 P0 已验证，任何 Key 命中都阻止对应组合发布。

### 7.3 发布矩阵（阻断、Go/No-Go 和停止线合并于此表）

| 产品×系统/事项 | 当前允许 | Go 证据 | No-Go / 阻断动作 |
|---|---|---|---|
| Claude macOS | 当前为 `candidate`：仅现场陪同内测，可生成本地现场测试包；未有适配器前只能“已装官方 App 后粘贴 Key” | P0-C-Mac、V1A 一位朋友现场 E2E、VPS→上游测试、恢复/Key 扫描通过，并在 `release-support.json` 有 `status=go` 与 P0 digest 后，才可生成并上传朋友发行包 | `candidate` 不上传公开 artifact；未达到 `go` 前不说“下载即安装”；网页按钮只算入口，不算安装 |
| Claude Windows | V1A 阶段受阻，禁止生成、打包或上传任何 Windows artifact；只保留研究/手工安装 | Claude macOS 现场 E2E 后，Windows 自有包身份、版本、流式、工具、重启、fixture、恢复和扫描通过，另行解除阻断 | 不创建通用适配器或发行承诺；不得以 Windows 研究结果替代 V1A |
| Codex macOS/Windows | V1A 阶段受阻，禁止生成、打包或上传任何 Codex artifact；仅 P0-X 脱敏研究 | 未来各自 P0 全部通过，另行更新本矩阵 | 任何 custom provider、免登录、历史、Responses 或恢复核心项失败都停在研究，不写成支持 |
| V1 服务合同、Key、目录、恢复、人工微信 | 必须随 Claude macOS 一起验收 | 契约错误/幂等测试、代际 profile 读回、未知 profile 保护、Key 生命周期、余额/充值边界、并发/重放充值测试通过 | 合同缺字段、Key 被写入当前 Friend profile 之外的明文副本、状态冲突、恢复不确定或余额第二真相存在，组合 no-go |
| 真实朋友分发 | V1A 只有一位现场朋友 | 上游明确允许多人使用/转发，并完成适用合规确认；同时 V1A 网络/备份/撤销/充值证据通过 | 未取得确认时不得真实分发；不写长法律论述，用记录结果阻断 |
| 公开版 | V1 后续 | Apple Developer ID/公证、Windows 代码签名、签名更新、自动更新、回滚、扫描和更新密钥轮换通过 | 继续标“现场陪同内测”，不上传公开版 |

任何一行失败只阻断对应产品×系统或事项，不静默降级为另一条未验证路线；最终状态必须逐组合记录，而不是用“双产品目标”代替支持状态。

## 8. 后续文件与依赖范围

不读取、不写入密钥或真实 `.env`。下表区分现有/新增文件、前置条件、受阻范围和禁止产物；表中的新增项仍需满足前置条件后才能实施。

### 8.1 文件执行矩阵

| 文件/目录（状态） | 执行内容 | 前置 | 受阻 / 当前状态 | 禁止产物 |
|---|---|---|---|---|
| `index.html`（现有） | Friend 页面壳与数据告知入口 | 先确定目录、Key 清空和阶段状态合同 | 前端合同未落地前不进入 V1A 包装 | Key、`local_flow_id`、Endpoint、裸模型输入 |
| `src/main.js`、`src/styles.css`（现有） | 展示阶段/目录/余额/三类入口；提交后清空 Key；配置 payload 只含 `canonical_id`、`catalog_version` | 服务端合同与 Rust 命令契约 | 未通过字段拒绝和失败关闭测试前受阻 | Key、`local_flow_id`、任意 Endpoint/model_ref/Provider 的持久化或产物 |
| `vite.config.js`（现有） | Vite 7 构建与资源边界 | Node.js 22、现有 lockfile、前端字段合同 | 发现敏感值进入 bundle 或构建目标不是 Claude macOS candidate 时受阻 | Key、`local_flow_id`、任意模型/Endpoint、含敏感值的资源 |
| `package.json`、`package-lock.json`（现有） | 复用现有 Node 依赖和命令 | Node.js 22 | 依赖/命令未能只服务 V1A candidate 时受阻 | 官方二进制、CC Switch、常驻 relay、Key |
| `src-tauri/src/main.rs`、`src-tauri/src/lib.rs`（现有） | Tauri 入口、Rust 命令、进程内 flow context、受信目录解析与清理 | `contracts/friend-api.openapi.json`（新增）和本节 Rust 设计；Rust stable | `local_flow_id` 可被序列化/暴露，或命令接受额外 Endpoint/model_ref/协议时受阻 | Key/`local_flow_id` 进入网络、日志、错误、CLI、Friend 设置、包或当前 Friend profile 之外的磁盘副本 |
| `src-tauri/tauri.conf.json`、`tauri.claude.conf.json`、`tauri.codex.conf.json`（现有） | 构建目标与打包配置；V1A 只允许 Claude macOS candidate 配置参与候选包生成 | P0-C-Mac、`release-support.json`（新增）状态和安装适配器 | `tauri.codex.conf.json` 在 V1A 受阻；Windows/Codex 配置不得生成任何 artifact | Codex/Windows 候选或发行包、官方二进制、Key、`local_flow_id` |
| `src-tauri/Cargo.toml`、`src-tauri/Cargo.lock`、`src-tauri/build.rs`（现有） | 复用 Tauri 2、Rust stable、`reqwest` rustls、`serde`、`fs2`、`keyring`、`toml_edit` 基线 | Rust 命令与契约确定 | 不引入未批准 Provider/relay；Codex 实现仍受 V1A 阻断 | Key、官方二进制、任意 Provider/Endpoint |
| `src-tauri/src/gateway.rs`、`src-tauri/src/recovery.rs`、`src-tauri/src/secure_store.rs`、`src-tauri/src/claude.rs`、`src-tauri/src/codex.rs`（新增） | 网关调用、generation manifest/补偿、短 TTL Key、Claude 迁移；Codex 仅后续研究 | 服务合同、所有权/阶段测试；Claude 先过 P0-C-Mac | Codex 文件与任何 Codex 构建受 V1A 阻断；恢复所有权证据不足时不实现删除 | Key/完整 profile、`local_flow_id`、任意 Endpoint/model_ref、官方二进制 |
| `src-tauri/src/installer.rs`、`src-tauri/src/installer/macos_claude.rs`（新增） | 校验官方 Claude macOS 包身份、签名/公证、版本和安装后复检 | P0-C-Mac 与真实官方包身份证据 | P0 或安装后复检未通过前受阻；Windows 适配器不在 V1A 创建 | 重打包/修改/随 Friend 分发的官方二进制、Windows/Codex 包 |
| `scripts/build-macos.sh`、`scripts/build-windows.ps1`（现有）；`scripts/verify-release-*`（新增） | 只为允许组合构建/扫描 | Node.js 22、P0 digest、`release-support.json` | V1A 只可生成 Claude macOS candidate；Windows/Codex 构建产物路径必须阻断 | Windows/Codex artifact、公开 candidate、Key、`local_flow_id`、完整日志/请求 |
| `.github/workflows/ci.yml`（现有） | 执行 gateway 无凭据测试、合同、payment、deployment static、Rust fmt/release、release gate 和 secret scan | `release-support.json` 与扫描脚本 | CI 通过不构成真实 VPS、New API adapter、TLS、公网或 P0 证据；非 go 组合不得上传 | Codex/Windows artifact、无 P0 digest 的发行包、Key |
| `contracts/friend-api.openapi.json`（现有） | 将第 2 节路径、受信目录、错误和幂等机器化 | 本计划服务合同定稿 | 未覆盖目录过期、任意模型拒绝、充值幂等前受阻 | 第二份 prose 方案、Key、完整请求/响应 |
| `release-support.json`（现有） | 记录产品×系统、渠道、状态和脱敏 P0 digest | P0 证据和发布矩阵 | V1A 只登记 Claude macOS `candidate`；Codex/Windows 保持 blocked | Key、完整日志/请求、`go` 或其他可产包状态的未验组合 |
| `friend-gateway/`（新增） | dependency-free reference/mock gateway、严格 Friend wire、服务端 Key 绑定、显式 New API adapter 边界和无凭据测试 | 服务合同；真实 New API catalog/balance 映射、VPS/TLS/公网与 P0 证据 | mock 可本地运行；proxy 仍是未验证 reference，不进入生产 | 明文 Key、客户端 account/install 覆盖、自动重试、伪造签名信任 |
| `new-api-deployment/`（新增） | Caddy、Compose、备份/恢复、VPS→上游和健康脚本 | 服务合同、VPS 私网/回环边界、RPO/RTO 演练 | 上游流式/工具/恢复证据未齐前不进入生产；当前未部署 | 公网管理/数据库、第二本余额账、自动生成 POST 重试 |
| `payment-control-plane/`（新增） | 人工微信申请、审计、原子 claim、New API provisioner 和测试 | 数据库 `UNIQUE(request_id)`/`UNIQUE(business_ref)`、状态机和对账流程 | 超时/未知保持 `crediting`；未确认未执行不得重试；无未扣款证据不得 `failed` | 自动支付、盲重试、第二本余额账、Key |
| `README.md`（现有） | 持续同步真实安装承诺、local candidate 与 blocked 边界 | 对应产品×系统实际 Go 证据 | 当前只保留 Claude macOS local candidate；VPS、Codex、Windows 和朋友分发未完成 | 未验证的下载/安装/Windows/Codex 承诺 |

依赖复用现有 Tauri 2、Rust stable、Node.js 22 和上述锁定依赖；不引入 CC Switch、常驻 relay、自制聊天 UI、官方二进制、Dog 或自动支付。完成任何阶段后，必须将脱敏证据和 `release-support.json` 状态对齐，再进入下一组合。
