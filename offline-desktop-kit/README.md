# Offline Desktop Kit

这套源码用于把用户自行取得的官方桌面安装器、离线 Runtime 和 CC Switch 发行包，组装成适合朋友现场安装的离线素材包。它保留真正的 Claude/Codex 桌面界面；CC Switch 只承担 Provider/Token 配置，不是聊天客户端。

作者：ruodou233、shing19。两位作者按本仓根目录 MIT 许可证发布本项目自有源码与模板；MIT 不覆盖第三方载荷、商标或其他项目的权利。

## 开源边界

仓库只包含安装脚本、manifest 模板和构建器，不包含：

- Claude Desktop、ChatGPT/Codex、Git for Windows、WebView2 等官方二进制；
- CC Switch 二进制；
- Claude Code/Codex Runtime；
- API Key、账号密码、微信信息或私有 Provider 配置；
- 已打包的朋友分发 ZIP。

请从各项目官方渠道合法取得载荷，并遵守对应许可和分发条款。源码发布不代表 Anthropic、OpenAI、Microsoft 或 CC Switch 项目对本工具背书。

## 支持的构建单元

| `--kit` | 外部素材目录必须包含 |
|---|---|
| `claude-macos` | `official-client.dmg`、`CC-Switch.dmg`、`CC-Switch-LICENSE`、`claude-code-engine-arm64.app/`、`claude-code-engine-x64.app/` |
| `claude-windows` | `official-client.msix`、`CC-Switch.zip`、`CC-Switch-LICENSE`、`Git-for-Windows.exe`、`Git-for-Windows-LICENSE.txt`、`MicrosoftEdgeWebView2RuntimeInstallerX64.exe`、`claude-code-engine.exe` |
| `codex-macos` | `official-client.dmg`、`CC-Switch.dmg`、`CC-Switch-LICENSE`、`codex-primary-runtime.tar.xz` |
| `codex-windows` | `official-client.msix`、`CC-Switch.zip`、`CC-Switch-LICENSE`、`MicrosoftEdgeWebView2RuntimeInstallerX64.exe`、配套的 `codex-primary-runtime.tar.gz`（只校验，不写入 Base ZIP） |
| `codex-windows-runtime` | `codex-primary-runtime.tar.gz` |

Codex Windows 的 Base 和 Runtime 是两个配套 ZIP，朋友安装时需要放在同一父目录。

## 构建

Python 3.10 及以上。macOS kit 必须在 Mac 上构建，以保留 `.app` 的符号链接和 Unix 权限：

```bash
python3 offline-desktop-kit/build_offline_desktop_kits.py \
  --kit claude-macos \
  --assets-dir /absolute/path/to/private-assets/claude-macos \
  --output-dir /absolute/path/to/output
```

构建器会：

1. 从 `templates/<kit>/` 复制公开安装脚本；
2. 从仓库外素材目录复制载荷；
3. 根据 manifest 严格校验固定载荷的 SHA-256，不会用任意新文件覆盖已验证的声明；
4. 生成包内 `SHA256SUMS.txt`；
5. 保留符号链接与执行权限，生成不含 AppleDouble/`__MACOSX` 的 ZIP。

`--assets-dir` 和 `--output-dir` 必须都在源码仓库外。构建器先写临时文件，再以不覆盖的硬链接发布固定文件名；已有文件和符号链接都会被拒绝。因此请先输出到支持硬链接的本地磁盘（如 APFS、NTFS、ext4），再复制到 exFAT/FAT U 盘或不支持硬链接的 NAS/SMB 目录。

模板中的版本、Bundle ID、签名 Team ID、Runtime marker 和发布者字段对应已验证的 v2 载荷。升级官方客户端或 Runtime 时，维护者仍需同步更新这些身份与版本字段，并在目标系统真人验收；构建器不会猜测新版本兼容性。

## v2 行为

- Claude Code 引擎同时写入普通目录与第三方 Provider 使用的 `Claude-3p` 目录。
- macOS 提权运行时识别真实桌面用户，并避免把应用装进 `/var/root`。
- Windows 检测当前进程与桌面账户是否一致，避免把 AppX 和配置注册到 SYSTEM 或另一个管理员。
- 已安装同版或更高版官方客户端时跳过旧载荷，继续补齐 Runtime/引擎。
- Codex Runtime 先在新目录解压验证，再用可回滚的目录切换替换旧目录；已有同版/更新 Runtime 会保留，遇到更新客户端却没有明确更新的 Runtime 时停止安装。这不是抗掉电的事务式原子替换。
- CC Switch 原位更新程序文件，保留用户已有 Provider 数据。
- 安装包不包含 Key；Provider 配置由用户或其本地 Agent 完成。
- 中国网络首次验收 Claude 时建议先测试 Code，不进入需要额外 VM 下载的 Cowork。

## 安全与真实边界

安装脚本会在目标系统核验官方签名、发布者或 Team ID；构建时根据 manifest 核对列明的固定文件或主可执行文件哈希。`assets-dir` 必须是构建者控制的受信输入；构建器不会净化任意 `.app` 内容或许可文本。某个第三方 Endpoint/Key 能否工作仍取决于协议兼容性、模型权限、余额与网络可达性。官方客户端升级也可能要求新的离线 Runtime，旧模板不能永久替代兼容性测试。

公开仓库不发布预构建二进制。你可以在自己的受信环境构建私人测试包，但不要把含第三方二进制或个人配置的产物直接提交回源码仓。如果还要把生成的 ZIP 交给朋友，该行为也可能构成分发：须先逐项确认再分发权与对应义务，包括 Git for Windows 及其捆绑组件可能要求的源码或书面要约；只附一份 license/notice 不一定充分。

上游起点：[CC Switch](https://github.com/farion1231/cc-switch)、[Git for Windows](https://gitforwindows.org/)、[WebView2 离线部署](https://learn.microsoft.com/microsoft-edge/webview2/concepts/distribution)。Claude/Codex 客户端与 Runtime 的来源和权利以构建当日的官方渠道与条款为准。
