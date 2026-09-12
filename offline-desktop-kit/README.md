# 官方桌面 Agent 离线安装素材包

把官方 Claude / Codex 桌面客户端、CC Switch 和配套运行组件组装成离线 ZIP，再交给朋友电脑上的 WorkBuddy 等 Agent 完成安装和配置。用户继续使用官方桌面界面；CC Switch 管理服务地址、Key 和模型。

本目录发布源码与构建模板，不包含第三方安装器、API Key 或预构建 ZIP。客户端保留原始签名。与仓库里的旧 Tauri 启动器、固定网关和计费实验相互独立。

## 给使用者

先安装能操作本机的 WorkBuddy 或其他 Agent，收齐对应系统的素材包，再把 [安装配置提示词](WORKBUDDY-PROMPT.md) 发给它。服务地址与 Key 由使用者自行准备，在本地配置界面填写。

| 使用场景 | 构建单元 |
|---|---|
| Claude Windows x64 | `claude-windows` |
| Claude Mac，Apple / Intel 芯片 | `claude-macos` |
| Codex Mac，Apple 芯片 | `codex-macos` |
| Codex Windows x64 | `codex-windows` 与 `codex-windows-runtime`，两份一起使用 |

Windows Codex 两包解压成并列文件夹，从主包的 `START-WINDOWS.cmd` 启动。其余主包运行 `START-WINDOWS.cmd` 或 `START-macOS.command`。组件包不另设安装入口。

Claude Windows 首装或升级需要当前桌面账户确认管理员权限；只提升官方 MSIX 安装子进程，后续配置保持普通用户身份。Claude 包包含 Code 引擎，不包含 Cowork 虚拟机。

## 构建

需要 Python 3.10+。Mac 包必须在 macOS 上构建，以保留应用符号链接和执行权限。载荷目录与输出目录都放在源码仓库外。

| 构建单元 | 外部载荷目录中的文件 |
|---|---|
| `claude-macos` | `official-client.dmg`、`CC-Switch.dmg`、`CC-Switch-LICENSE`、`claude-code-engine-arm64.app/`、`claude-code-engine-x64.app/` |
| `claude-windows` | `official-client.msix`、`CC-Switch.zip`、`CC-Switch-LICENSE`、`Git-for-Windows.exe`、`Git-for-Windows-LICENSE.txt`、`MicrosoftEdgeWebView2RuntimeInstallerX64.exe`、`claude-code-engine.exe` |
| `codex-macos` | `official-client.dmg`、`CC-Switch.dmg`、`CC-Switch-LICENSE`、`codex-primary-runtime.tar.xz` |
| `codex-windows` | `official-client.msix`、`CC-Switch.zip`、`CC-Switch-LICENSE`；另提供配套的 `codex-primary-runtime.tar.gz` 和 `MicrosoftEdgeWebView2RuntimeInstallerX64.exe` 用于匹配校验，这两项不写入主包 |
| `codex-windows-runtime` | `codex-primary-runtime.tar.gz`、`MicrosoftEdgeWebView2RuntimeInstallerX64.exe` |

```bash
python3 offline-desktop-kit/build_offline_desktop_kits.py \
  --kit claude-macos \
  --assets-dir /path/to/external-assets/claude-macos \
  --output-dir /path/to/output
```

对需要的平台分别运行。构建器核对固定 manifest 的载荷哈希，拒绝混入不同版本或覆盖现有 ZIP。ZIP 内保留必要脚本、manifest、载荷、授权说明和简短使用说明，不另附重复校验清单。输出先放支持硬链接的本地磁盘，再复制到微信、U 盘或共享目录。

## 版本与验收

这是一组 **2026-09-10 核验的配套版本**，不代表永久最新版：

| 组件 | 版本 |
|---|---|
| Claude Desktop | Mac `1.49585.0`；Windows `1.49585.0.0` |
| 桌面绑定的 Claude Code | `2.1.260` |
| Codex Desktop | Mac `26.903.71938`；Windows MSIX `26.903.8094.0` |
| Codex Primary Runtime | `26.905.11957` |
| CC Switch | `3.20.2` |
| Git for Windows | `2.55.0.5` |

维护者于 **2026-09-13 确认已完成实际验收测试**；未单独记录各系统设备与账号的明细，不把这次确认扩展为所有机器和服务线路的兼容承诺。

9 月 10 日构建的五个 ZIP 均小于 1,000,000,000 字节。已检查载荷哈希、ZIP 完整性、macOS 签名、脚本语法、Windows 提权命令路径转义和版本比较。CI 在 macOS / Windows 上运行构建测试与脚本解析，不会真实安装客户端。

更新时同步调整桌面客户端、绑定引擎、运行组件和 manifest；不能只换安装器或放宽哈希。官方滚动下载链接可能已更新，若与清单不符，应重新核验整套版本。

官方来源：[Claude 发布源](https://downloads.claude.ai/releases/darwin/universal/RELEASES.json)、[Claude 桌面文档](https://code.claude.com/docs/en/desktop)、[Codex Mac 更新源](https://persistent.oaistatic.com/codex-app-prod/appcast.xml)、[Codex Windows 部署](https://learn.chatgpt.com/docs/enterprise/windows-deployment)、[CC Switch](https://github.com/farion1231/cc-switch/releases/tag/v3.20.2)。Code 引擎以桌面包内 manifest 为准；Codex 运行组件使用官方 `codex-primary-runtime/latest/<平台>/LATEST.json`。

## 准备官方载荷

Claude 这组固定桌面载荷：[Mac DMG](https://downloads.claude.ai/releases/darwin/universal/1.49585.0/Claude-41ad1dff5275eedc8af25989f59f33c5efe14063.dmg)、[Windows MSIX](https://downloads.claude.ai/releases/win32/x64/1.49585.0/Claude-41ad1dff5275eedc8af25989f59f33c5efe14063.msix)。下载后分别命名为 `official-client.dmg` / `official-client.msix`。

桌面包内 `app.asar` 的 Code manifest 固定引擎版本、平台校验值及下载基址。这组引擎来自：

- [Mac arm64 bundle](https://downloads.claude.ai/claude-code-releases/2.1.260/darwin-arm64/claude.app.tar.zst)
- [Mac x64 bundle](https://downloads.claude.ai/claude-code-releases/2.1.260/darwin-x64/claude.app.tar.zst)
- [Windows x64 binary](https://downloads.claude.ai/claude-code-releases/2.1.260/win32-x64/claude.exe.zst)

用支持 zstd 的工具解压。Mac 保留完整 `claude.app` 及权限，按架构改目录名为载荷表里的两个 `.app`；Windows 解压为 `claude-code-engine.exe`。安装器的 `.verified` 标记对应压缩包校验值，而 manifest 内引擎哈希对应解压后的可执行文件，两者不要混用。

Codex 运行组件固定载荷：[Mac arm64](https://persistent.oaistatic.com/codex-primary-runtime/26.905.11957/codex-primary-runtime-darwin-arm64-26.905.11957.tar.xz)、[Windows x64](https://persistent.oaistatic.com/codex-primary-runtime/26.905.11957/codex-primary-runtime-win32-x64-26.905.11957.tar.gz)。重命名为载荷表中的 `codex-primary-runtime.tar.xz` / `.tar.gz`，不提前解压。

查询新运行组件使用 [Mac manifest](https://persistent.oaistatic.com/codex-primary-runtime/latest/darwin-arm64/LATEST.json) 或 [Windows manifest](https://persistent.oaistatic.com/codex-primary-runtime/latest/win32-x64/LATEST.json)，核对其中的下载地址、大小、哈希与版本后成套更新。

Codex 桌面滚动下载：[Mac DMG](https://persistent.oaistatic.com/codex-app-prod/ChatGPT.dmg)、[Windows MSIX](https://persistent.oaistatic.com/codex-app-prod/ChatGPT-x64.msix)。滚动链接不保证仍能取得 9 月 10 日的字节；与固定清单不符时重新验证新版本，不能跳过校验。

CC Switch 使用 3.20.2 release 的 macOS DMG / Windows Portable ZIP，并附对应 LICENSE。Git 使用 [2.55.0.5 x64](https://github.com/git-for-windows/git/releases/tag/v2.55.0.windows.5) 及授权文件；WebView2 使用[微软 x64 离线安装器](https://go.microsoft.com/fwlink/?linkid=2124701)，同样按 manifest 核对版本载荷。

## 源码与第三方载荷

作者：ruodou233、shing19。项目自有源码遵循仓库 MIT 许可证；第三方客户端、运行组件和商标不由该许可证授权。取得与分发载荷时遵守各自许可，授权说明见 [THIRD_PARTY-NOTICES.md](THIRD_PARTY-NOTICES.md)。公开 Git 仓库与 Release 不上传第三方二进制、个人服务配置或 Key。
