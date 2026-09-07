# 私有 Claude 候选包构建器

`scripts/build-private-claude-kit.py` 只构建 Claude Desktop fresh-install 候选包，支持 macOS 和 Windows。它接收用户在仓库外准备好的官方安装器、SHA-256 和候选配置，输出仓库外 ZIP；构建器不下载或上传这些输入。凭据既可在私下测试包中内置，也可完全不进包、等安装时由使用者输入。

## 边界

- `--installer`、`--output-dir`，以及使用内置 Key 模式时的 `--key-file` 必须在仓库外，仓库内路径会被拒绝。
- 凭据模式必须二选一：`--key-file` 生成内置 Key 的私下测试包；`--prompt-for-key` 生成不含 Key、安装时由用户本机无回显输入 Key 的可分享包。两者不能同时使用。
- 安装器 URL 必须通过官方 HTTPS URL 规则，安装器体积小于 1 MiB 只有测试时可配合 `--allow-small-test-installer` 使用。
- 内置 Key 的 ZIP 是私有候选包，不得提交或上传公开仓。ZIP 成员不保存 Key 明文，但安装脚本内的 Base64 profile 可由收到包的本机用户恢复，因此包本身仍需按敏感材料处理。`--prompt-for-key` 产物不含 Key，可以分享，但仍不应把官方安装器二进制提交到源码仓。
- 这是 fresh-install-only 流程：发现既有 Claude 配置、策略、符号链接或 Claude 正在运行时会拒绝安装，不合并或备份现有配置。Restore 删除前会读取并核对 manifest、profile/meta 身份字段和三个目标文件哈希；任一项被修改或身份不匹配就拒绝。prompt 模式安装时生成的动态 manifest 是防误删边界，不抵抗同一 OS 用户主动同时篡改 profile/manifest。
- 安装失败只回滚配置文件；官方 Claude.app/AppX 安装可能已经完成并仍保留。Restore 不卸载 Claude.app/AppX，也不删除测试会话。
- macOS 在打开最终 Claude.app 前执行 `codesign --verify --deep --strict`、`spctl --assess --type execute`，并要求 `TeamIdentifier=Q6L2SF6YDW`；Windows 在 `Add-AppxPackage` 前要求 installer SHA-256 匹配、Authenticode `Status=Valid`，且签名主题包含 `Anthropic, PBC`。
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

构建无 Key 包时，把示例里的 `--key-file ...` 替换为：

```text
  --prompt-for-key
```

无 Key 包只保存网关、模型和部署信息，不保存实际 Key。用户双击安装入口后输入的 Key 会写入当前用户的 Claude-3p profile；输入过程不回显，但同一系统用户仍可读取该配置，因此不能把它当作硬件保险库。无论 prompt 还是 embedded，manifest 都绑定 `generation_id` 与 `deployment_uuid`；Restore 会读取 profile/meta JSON，在 owner、product、generation、deployment、metadata entry 身份和三个文件哈希全部匹配后才移除本包创建的三个固定配置文件。
