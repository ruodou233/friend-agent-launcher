# 私有 Claude 候选包构建器

`scripts/build-private-claude-kit.py` 只构建私有的 Claude Desktop fresh-install 候选包，支持 macOS 和 Windows。它接收用户在仓库外准备好的官方安装器、SHA-256、独立 Key 和候选配置，输出仓库外 ZIP；构建器不下载或上传这些输入。

## 边界

- `--installer`、`--key-file` 和 `--output-dir` 必须在仓库外，仓库内路径会被拒绝。
- 安装器 URL 必须通过官方 HTTPS URL 规则，安装器体积小于 1 MiB 只有测试时可配合 `--allow-small-test-installer` 使用。
- ZIP 是私有候选包，不是公开发行物；Key 不得提交或上传公开仓。ZIP 成员不保存 Key 明文，但安装脚本内的 Base64 profile 可由收到包的本机用户恢复，因此包本身仍需按敏感材料处理。
- 这是 fresh-install-only 流程：发现既有 Claude 配置、策略、符号链接或 Claude 正在运行时会拒绝安装，不合并或备份现有配置。Restore 只移除本包配置；只在三个目标文件与本包预期哈希均匹配时删除，若任一文件被修改则拒绝；Restore 不卸载 Claude.app/AppX，也不删除测试会话。
- macOS 根目录包含 `Install.command` / `Restore.command`；Windows 根目录包含 `Install.cmd` / `Restore.cmd`，PowerShell 实现位于 `support/`。
- Windows 静态测试只能检查包结构和脚本入口；Windows 真机仍需验收官方安装器、权限、Claude 启动与 Restore 流程。

命令参数以 `python3 scripts/build-private-claude-kit.py --help` 为准。以下仅示意参数形状，不可直接运行，所有敏感输入均为占位符：

```text
python3 scripts/build-private-claude-kit.py \
  --platform <macos-or-windows> \
  --installer <official-installer-outside-repo> \
  --installer-url <official-https-url-placeholder> \
  --installer-sha256 <64-char-sha256-placeholder> \
  --key-file <key-file-outside-repo> \
  --gateway-url https://example.invalid \
  --output-dir <output-dir-outside-repo> \
  --models claude-fable-5 \
  --quota-label <test-label> \
  --expires-at <future-utc-timestamp> \
  --deployment-uuid <uuid-placeholder> \
  --validation-status <status-placeholder> \
  --version <version-placeholder>
```
