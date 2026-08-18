# Claude Windows x64 离线素材包

这是第三方整理的离线安装素材包，不是 Anthropic 官方发行物。官方 Claude 客户端和其他二进制保留原始签名，安装脚本会在执行前校验。

在 64 位 Windows 上解压全部文件，双击 `START-WINDOWS.cmd`。安装包不在安装过程中从境外站点下载组件。

内含：

- Anthropic 签名的 Claude Desktop MSIX。
- 与当前客户端匹配的 Claude Code 2.1.219 Windows x64 引擎。
- Git for Windows 离线安装器。
- 微软官方 WebView2 x64 离线安装器。
- CC Switch 3.19.1 Windows Portable。

引擎会同时预置到 Claude 的普通线路和第三方 Provider（`Claude-3p`）目录。如果安装由提权的 AI 助手发起，脚本会优先识别真正登录的桌面用户。

中国网络首次验收请先使用 Claude 的 Code 功能，不要进入 Cowork。Cowork 需要另外下载大型 VM 组件，不属于这个干净基础包的验收范围。安装器不会修改未公开的 Cowork 开关。

本包不含 API Key、账号密码、Provider 账户数据、获取 Key 的文档或给 AI Agent 的提示词。
