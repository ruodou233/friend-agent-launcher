# Friend gateway reference/mock adapter

状态：**可本地运行的 reference/mock 实现，未完成真实 VPS→New API 适配证据，不宣称生产可用**。

服务只实现合同中的四个公网路径：

- `GET /v1/friend/preflight`
- `POST /v1/friend/catalog`
- `GET /v1/friend/balance`
- `POST /v1/messages`

`/healthz` 只供容器自身健康检查，Caddy 不公开它。管理、模型、Responses 和未知路径没有 handler。

## 边界

- Bearer Friend Key 只按服务端保存的 SHA-256 绑定解析出 `account_id`/`install_id`；明文 key 不写入绑定文件、日志或响应。请求体、查询参数和 `X-Account-Id`/`X-Install-Id` 等调用方 header 均不能覆盖绑定。
- catalog 只从服务端 `CatalogAdapter` 读取，不接受客户端目录。重复 `canonical_id` 或 `model_ref` 时，完全相同的条目只保留首次出现的一条；字段冲突直接失败关闭，不静默择一。
- balance 通过独立的 `BalanceAdapter` 读取，wire 固定为合同要求的整数 minor unit，`source` 固定为 `new-api`。
- `mock` 模式只使用仓内无秘密 fixture，并返回固定的 mock Messages 响应。
- `proxy` 模式的 `NewApiMessagesProxy` 把已验证的原始 JSON body 向配置的内网 URL 发出一次 `POST /v1/messages`，不把外部 Friend Key 转发给 New API，也不自动重试/切路由；Messages 与 Balance 上游响应的所有 header 都会先验证类型、token 名称、控制字符和大小写归一后的唯一性，任一异常都 502 fail-closed；Messages 只转发明确审核过的 `Content-Type`，未知但合法的 header 丢弃。余额 adapter 要求配置的 reviewed adapter URL 返回 canonical `{"product":"claude","balance":...}` wire；这些厂商映射和真实链路仍待 P0 证据。
- 日志只写固定 allowlist 中的精确 path、method、status；未知路径记为固定占位符，不写 Authorization、key、body、查询字符串或控制字符。

## 本地 mock

不需要第三方 Python 依赖或真实凭据：

```bash
FRIEND_GATEWAY_MODE=mock \
FRIEND_GATEWAY_PORT=3000 \
python3 friend-gateway/friend_gateway.py
```

仓内 mock fixture 的测试 key 是 `local-mock-friend-key`；它只是本地测试值，对应文件中保存的只有 SHA-256。调用时仍必须发送合同要求的 `X-Request-Id` 和 Bearer header。

## 生产 P0 边界

需要真实环境单独证明：New API 的具体 Messages/余额接口与认证映射、`friend-model:` 到上游模型的固定映射、流式和 tool use、上游错误/超时语义、VPS→New API DNS/TLS/超时，以及密钥撤销传播。没有这些证据时只能标记 `reference/mock`，不能把 `proxy` 模式写成已验证生产 adapter。
