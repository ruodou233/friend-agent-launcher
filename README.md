# Friend Agent Launcher

给不熟悉 Provider、配置文件和 CC Switch 的朋友使用的两个轻量桌面启动器：

- **Friend Claude**：配置并打开原版 Claude Desktop。
- **Friend Codex**：配置并打开原版 ChatGPT / Codex 桌面端。

用户只需要安装、粘贴 Key、点击“开始使用”。Base URL、协议和模型默认由发行包预设，
需要时才在“高级设置”中出现。

## 为什么不修改官方 App

Claude Desktop 已提供官方第三方推理模式，保留 Chat、Cowork 和 Code 原版界面；
Codex 桌面端也支持用户级自定义 Responses Provider。因此本项目不复制界面、不修改、
注入、重签或重新分发官方二进制。官方 App 继续走自己的自动更新。

参考：

- [Claude Desktop on 3P](https://claude.com/docs/third-party/claude-desktop/overview)
- [Claude Desktop Gateway](https://claude.com/docs/third-party/claude-desktop/gateway)
- [Codex custom model providers](https://developers.openai.com/codex/config-advanced/#custom-model-providers)
- [CC Switch](https://github.com/farion1231/cc-switch)
- [relay-ai](https://github.com/jacob-bd/relay-ai)
- [codex-relay](https://github.com/MetaFARS/codex-relay)

## 最终使用流程

1. 用户下载 Friend Claude 或 Friend Codex。
2. 启动器检测官方 App；未安装时只显示一个“下载官方 App”按钮。
3. 首次使用只粘贴 Key；发行包预设 HTTPS 网关和默认模型。
4. 启动器完成一次对应协议的低成本模型调用；高级设置中可获取去重后的模型列表。
5. 启动器备份现有配置、写入配置、必要时正常重启并打开原版 App。
6. 以后用户点击 Friend Claude / Friend Codex 图标即可直接进入原版 Agent。

“没有 Key？”只提供三个可选入口：免费 Token、邀请朋友奖励、我们的中转站。它不阻塞
已有 Key 的用户。

## 获取 Key

- **免费 Token**：打开 [free-token-eggs](https://github.com/ruodou233/free-token-eggs)，查看当前仍值得注册的平台。
- **邀请朋友**：转发本项目的 [Releases](https://github.com/ruodou233/friend-agent-launcher/releases) 下载页；奖励登记暂由发行者人工处理。
- **我们的中转站**：朋友内测阶段由发行者逐人创建、限额和发放 Key；公网网关上线前不开放自助购买。

## 实现范围

两个产品共用一个 Tauri 2 核心，只保留以下能力：

- 官方 App 检测、下载入口和启动；
- Key、网关和模型的首次配置；
- Claude Messages / Codex Responses 最小调用与可选模型发现；
- 原配置备份、一键恢复官方模式；
- 错误 Key、协议不兼容和网关不可达的中文提示；
- Windows x64、macOS Apple Silicon 构建；
- 源码与产物的凭据扫描；

首版不做支付、复杂多 Provider 面板、动态路由、独立聊天历史或自制 Agent 界面。
同名模型的多上游迁移由服务端完成，客户端不让用户理解线路概念。

## Claude 与 Codex 的差异

### Friend Claude

写入 Claude Desktop 官方 3P Gateway 配置：

- 网关必须支持 Anthropic Messages `POST /v1/messages`；
- 可通过 `GET /v1/models` 自动发现 Claude 模型，也可写显式模型列表；
- 会话保存在用户本机；
- 完整退出并重启 Claude 后配置生效。

Claude 的单机静态 Gateway Key 会进入 Claude 的本地 3P 配置。朋友内测版因此只使用每人、
每个 Agent 独立的低额度下游 Key，并确保可定位、限额和单独吊销。正式扩大分发时再升级为
OIDC 或受管 Credential Helper。

### Friend Codex

保留式修改 `~/.codex/config.toml`：

- 网关必须支持 OpenAI Responses API；
- Key 不写进 TOML；通过安装在稳定 App Data 路径的凭据 Helper 从系统凭据库按需读取；
- macOS 使用 Keychain，Windows 使用 Credential Manager；
- 原有项目、对话、MCP 和其他配置不删除。

如果某个上游只有 Chat Completions，才另行评估协议转换；我们的 New API 已提供
Responses，首版不附带 relay。

## 构建

源码构建不含任何 Key。发行者通过非敏感环境变量预设网关与默认模型：

```bash
export FRIEND_GATEWAY_URL=https://gateway.example.com
export FRIEND_CLAUDE_MODEL=your-claude-logical-model
export FRIEND_CODEX_MODEL=your-codex-logical-model
npm ci
npm test
npm run desktop:build:claude
npm run desktop:build:codex
```

没有预设时仍可在“高级设置”手动填写，适合开发调试；发给小白的发行包必须预设公网
HTTPS 网关和默认逻辑模型。

## 发布阶段

### 朋友内测版

- 未签名或临时签名安装包；
- 每人独立、低额度、可吊销 Key；
- 在真实 Windows 和 macOS 上完成安装、首次对话、重启、换 Key、恢复配置和卸载测试；
- 网关不可用或协议不匹配时失败关闭，不写坏原配置。

### 公开正式版

- Apple Developer ID 签名与公证；
- Windows 代码签名；
- 国内可访问的公网 HTTPS 网关；
- 发布前扫描源码、Git 历史和全部安装产物，确认没有 API Key、密码或个人测试数据。

## 当前阻断项

Mac mini 上的 New API 目前只监听回环地址，尚无朋友设备可长期访问的公网 HTTPS 域名。
因此当前可以开源源码、生成无密钥 Beta 安装器并做本机兼容测试；但“安装后只粘贴 Key
就能从朋友家使用”的正式验收，必须等公网 HTTPS 入口完成并注入发行构建。Codex 桌面端
在完全没有 OpenAI 登录状态时是否接受第三方 Provider，也仍需在干净 Windows/macOS
账户中做端到端实测。

## 开源边界

- 仓库和 Release 不包含上游 Key、下游 Key、账号密码或官方 App 二进制。
- 项目不使用 CC Switch 名称，不冒充 Claude、Anthropic、Codex 或 OpenAI 官方产品。
- Claude 单机配置目前通过其本地 `configLibrary` 兼容实现写入；官方推荐路径仍是 App
  内“Apply locally”，因此每次 Claude 大版本更新都要回归测试并保留一键恢复。
- 借鉴 MIT 项目的配置与启动思路；若直接复制代码，会保留相应许可证和版权声明。
- 项目采用 MIT 许可证；发布前仍需对新增依赖执行第三方许可证核对。
