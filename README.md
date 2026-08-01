# Friend Agent Launcher

这是一个非官方 companion/launcher 的源码基础，用来验证“保留官方桌面 App，只写入受信 Friend 配置”的本地流程。它不包含、修改、注入、重签或重新分发 Claude Desktop、ChatGPT 或 Codex 官方 App，也不等于可以发给朋友的安装包。

作者：ruodou233、shing19。

## 当前状态

- **Claude macOS**：`candidate`，只允许在 macOS 上生成本地 CI/现场验证包；使用前必须已经安装官方 Claude Desktop。当前不宣称“下载即安装”“一键可用”或朋友可直接使用。
- **Claude Windows**：`blocked`。
- **Codex macOS / Windows**：`blocked`，仅保留脱敏 research；不构建、不打包、不发布 Codex artifact。
- **Friend gateway**：仓内已有可运行的 dependency-free `reference/mock`，覆盖合同中的四条 Friend 路径；本地测试不需要真实凭据。`proxy` 只表示显式 adapter 边界，不代表真实上游已接通。
- **真实外部链路**：VPS、公网 HTTPS/TLS、真实 New API catalog/balance adapter、朋友设备可达性、P0 和签名分发均未验证；`new-api-deployment/` 是未部署模板。
- `contracts/friend-api.openapi.json` 和 `release-support.json` 是无密钥机器契约/门禁数据，不代表服务已经上线。

公开仓库不包含 Friend Key、上游 Key、账号密码、真实域名或官方 App 二进制。没有真实固定网关时，客户端运行时失败关闭；候选包也不构成可用发行版。

## 当前客户端边界

当前 Friend Claude 流程只接受一次性 Friend Key，从 Rust 后端获取固定目录并选择 `canonical_id`；前端不提供 Endpoint、Provider 或裸模型输入，也不接受客户端覆盖账户归属。目录信任只基于固定 HTTPS origin、严格 wire schema、`tls-fixed-gateway`、版本和过期时间，不是公钥签名实现。

配置后的请求仍由官方 Claude Desktop 发出；本工具只负责本地候选流程、固定网关配置、恢复入口和状态展示。余额是 New API 的整数最小货币单位读数，不在控制面建立第二本账。

配置成功后，Key 按 Claude 官方 3P 静态配置写入当前 Friend profile，本机账户可读取；对本工具的持久化写入而言，Key 只存在这个最终 Friend profile。敏感 profile 使用最终 generation path 的 `create_new`、Unix `0600`、flush 和 sync，不生成本工具的 `.friend-agent.tmp` / `.friend-agent.bak` 明文副本；输入框提交后清空，本工具设置、日志和恢复 manifest 不另存或回显。

Claude 的 configure/current-key/restore 文件事务同时持有进程 Mutex 和专用非敏感 `.friend-agent.lock`；后者只协调同样配合该锁的进程，不是不可伪造的强 CAS，也不能消灭不使用该锁的外部写入竞态。后端在首次快照前确认官方 App 已停止，并在写入/删除最终动作前再次校验；发现异常就 fail-closed 进入 `RECOVERY_REQUIRED`。恢复 journal 会在 library 与 `friend-generations` 两处分别写入、读回验证并 sync，只有两处都持久化才算成功，任一处失败都不静默回退。

首页保留三类入口：

1. **我方 Key**：V1A 唯一可直接配置和查余额的入口，按账户、产品和安装绑定控制额度。
2. **邀请**：只打开人工登记入口，不承诺自动到账或自动结算。
3. **免费第三方**：只打开外链，不接受第三方 Key、Endpoint 或 Provider。

“打开官方获取页”只是外链入口，不是安装器；官方 App 需要用户自行安装并在启动器中复检。

## 构建、测试与门禁

Node.js 22.x 是门禁基线；依赖版本由 `package-lock.json` 锁定。以下命令是当前真实的静态构建、测试和门禁入口：

```bash
python friend-gateway/tests/test_gateway.py
python contracts/validate_contract.py
bash payment-control-plane/verify.sh
python tests/test_deployment_static.py
python scripts/verify-release-support.py --action check
python tests/test_release_gate.py
cargo fmt --manifest-path src-tauri/Cargo.toml --check
cargo test --manifest-path src-tauri/Cargo.toml
cargo test --release --manifest-path src-tauri/Cargo.toml
npm run build:claude
npm run build:codex
git diff --check
bash scripts/scan-secrets.sh
```

`npm run desktop:build:claude` 还要通过 `release-support.json` 的 Claude macOS `candidate` 门禁，只能生成本地 candidate；`npm run desktop:build:codex` 和 `npm run desktop:build:windows` 应保持阻断。手工执行 Codex Tauri 构建同样会先经过门禁，且 Codex 配置的 `bundle.active` 为 `false`。

构建流程由 `scripts/build-macos.sh` 统一编排；其中 wrapper 环境变量检查只用于阻止误用，不是安全边界，也不能证明构建不可由其他方式触发。真正的发布依据是 CI、支持矩阵、最终产物 allowlist、secret scan 和 P0 evidence。这不产生朋友可直接安装的发行包。

如果本机不是 Node.js 22.x，仍可运行契约校验、静态前端构建、Rust 格式检查和测试，但结果必须记录 Node 版本偏差；不因版本偏差宣称候选包可发布，也不安装系统软件来规避门禁。

## 不修改官方 App

Friend 的边界是辅助配置和启动，不复制官方 UI，不接管官方更新，不把官方 App 放进本仓库。当前公开状态仅支持已安装官方 App 后的 Claude macOS 本地候选验证；Windows、Codex、朋友分发、VPS 上线和公开 Release 都等待各自门禁与真实 P0 证据。

项目采用 MIT 许可证；新增依赖和未来发布产物仍需单独做许可证与凭据扫描。
