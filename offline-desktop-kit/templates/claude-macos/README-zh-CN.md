# Claude macOS 离线素材包

这是第三方整理的离线安装素材包，不是 Anthropic 官方发行物。官方 Claude 客户端和其他二进制保留原始签名，安装脚本会在执行前校验。

解压全部文件，双击 `START-macOS.command`。如被 macOS 拦截，请右键该文件后选择“打开”。安装包不在安装过程中从境外站点下载组件。

内含：

- Anthropic 签名并经 Apple 公证的 Claude Desktop 1.24012.11 DMG。
- Claude Code 2.1.219 arm64 和 x86_64 执行引擎，安装时自动选择本机架构。
- CC Switch 3.19.1 macOS DMG。

引擎会同时预置到 Claude 的普通线路和第三方 Provider（`Claude-3p`）目录。即使安装由提权的 AI 助手发起，也会自动安装给当前登录的桌面用户。

中国网络首次验收请先使用 Claude 的 Code 功能，不要进入 Cowork。Cowork 需要另外下载大型 VM 组件，不属于这个干净基础包的验收范围。安装器不会修改未公开的 Cowork 开关。

本包不含 API Key、账号密码、Provider 账户数据、获取 Key 的文档或给 AI Agent 的提示词。
