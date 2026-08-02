#!/usr/bin/env bash
set -euo pipefail

export LC_ALL=C

root_dir=$(cd "$(dirname "$0")/.." && pwd -P)
dist_dir="$root_dir/dist"
lock_dir="$dist_dir/.friend-test-kit.lock"
build_script="$root_dir/scripts/build-macos.sh"
gateway_verify_script="$root_dir/scripts/verify-friend-gateway.py"
verify_script="$root_dir/scripts/verify-release-support.py"
scan_script="$root_dir/scripts/scan-secrets.sh"
atomic_publish_script="$root_dir/scripts/atomic-publish-macos.py"
kit_name="friend-test-kit"
zip_name="friend-test-kit-claude-macos-arm64-candidate.zip"
final_zip="$dist_dir/$zip_name"
candidate_dmg_name="Friend-Claude-0.1.0-macos-arm64-candidate.dmg"
checksum_name="SHA256SUMS-candidate.txt"

temp_root=""
temp_identity=""
release_dir=""
release_dir_identity=""
candidate_dmg_identity=""
checksum_identity=""
checksum_content_digest=""
staging_root=""
staging_identity=""
staging_created=0
staging_kit_root=""
staging_dir=""
staging_dmg_identity=""
staging_checksum_identity=""
unpacked_dir=""
post_unpacked_dir=""
zip_path=""
zip_identity=""
dist_created=0
dist_identity=""
lock_created=0
lock_identity=""

fail() {
  echo "朋友现场测试套件: BLOCKED: $1" >&2
  exit 1
}

directory_identity() {
  local path="$1"
  if [[ "$(uname -s)" == "Darwin" ]]; then
    /usr/bin/stat -f '%d:%i' "$path"
  else
    /usr/bin/stat -c '%d:%i' "$path"
  fi
}

same_directory_identity() {
  local path="$1"
  local expected="$2"
  local actual
  [[ -d "$path" && ! -L "$path" ]] || return 1
  actual=$(directory_identity "$path") || return 1
  [[ "$actual" == "$expected" ]]
}

record_directory_identity() {
  local path="$1"
  [[ -d "$path" && ! -L "$path" ]] || return 1
  directory_identity "$path"
}

file_identity() {
  local path="$1"
  [[ -f "$path" && ! -L "$path" ]] || return 1
  if [[ "$(uname -s)" == "Darwin" ]]; then
    /usr/bin/stat -f '%d:%i' "$path"
  else
    /usr/bin/stat -c '%d:%i' "$path"
  fi
}

same_file_identity() {
  local path="$1"
  local expected="$2"
  local actual
  actual=$(file_identity "$path") || return 1
  [[ "$actual" == "$expected" ]]
}

assert_built_release_identity() {
  local context="$1"
  same_directory_identity "$release_dir" "$release_dir_identity" || fail "$context 后 release_dir identity 不匹配"
  same_file_identity "$candidate_dmg" "$candidate_dmg_identity" || fail "$context 后 candidate DMG identity 不匹配"
  same_file_identity "$checksum_file" "$checksum_identity" || fail "$context 后 checksum identity 不匹配"
}

assert_built_checksum_content_stability() {
  local context="$1"
  local current_checksum_content_digest
  current_checksum_content_digest=$(shasum -a 256 "$checksum_file" | awk '{ print $1 }') || fail "$context 后无法计算 checksum 内容 digest"
  [[ "$current_checksum_content_digest" == "$checksum_content_digest" ]] || fail "$context 后 checksum 内容已变化"
}

assert_built_dmg_digest() {
  local context="$1"
  local current_dmg_digest
  current_dmg_digest=$(shasum -a 256 "$candidate_dmg" | awk '{ print $1 }') || fail "$context 后无法计算 candidate DMG digest"
  [[ "$current_dmg_digest" == "$source_digest" ]] || fail "$context 后 candidate DMG digest 已变化"
}

assert_payload_matches_build() {
  local kit_root="$1"
  local staged_dmg="$kit_root/claude-macos/$candidate_dmg_name"
  local staged_checksum="$kit_root/claude-macos/$checksum_name"
  local staged_dmg_digest
  local staged_checksum_content_digest

  staged_dmg_digest=$(shasum -a 256 "$staged_dmg" | awk '{ print $1 }') || fail "无法计算被打包 DMG digest"
  [[ "$staged_dmg_digest" == "$source_digest" ]] || fail "被打包 DMG digest 不匹配 build 期望 digest"
  staged_checksum_content_digest=$(shasum -a 256 "$staged_checksum" | awk '{ print $1 }') || fail "无法计算被打包 checksum 内容 digest"
  [[ "$staged_checksum_content_digest" == "$checksum_content_digest" ]] || fail "被打包 checksum 内容不匹配 build checksum"
}

assert_staged_payload_matches_build() {
  local kit_root="$1"
  local staged_dmg="$kit_root/claude-macos/$candidate_dmg_name"
  local staged_checksum="$kit_root/claude-macos/$checksum_name"

  same_file_identity "$staged_dmg" "$staging_dmg_identity" || fail "被打包的 DMG identity 不匹配 cp 后的 staging 文件"
  same_file_identity "$staged_checksum" "$staging_checksum_identity" || fail "被打包的 checksum identity 不匹配 cp 后的 staging 文件"
  assert_payload_matches_build "$kit_root"
}

cleanup() {
  exit_code=$?

  # All recursive cleanup is restricted to this run's mktemp directories.
  if [[ "$staging_created" -eq 1 && -n "$staging_root" ]]; then
    case "$staging_root" in
      "$dist_dir"/.friend-test-kit.*)
        if same_directory_identity "$staging_root" "$staging_identity"; then
          find "$staging_root" -depth -delete >/dev/null 2>&1 || true
        fi
        ;;
    esac
  fi
  if [[ -n "$temp_root" && -n "$temp_identity" ]]; then
    case "$temp_root" in
      "${TMPDIR:-/tmp}"/friend-test-kit.*)
        if same_directory_identity "$temp_root" "$temp_identity"; then
          find "$temp_root" -depth -delete >/dev/null 2>&1 || true
        fi
        ;;
    esac
  fi
  if [[ "$lock_created" -eq 1 && -n "$lock_identity" ]]; then
    if same_directory_identity "$lock_dir" "$lock_identity"; then
      rmdir "$lock_dir" >/dev/null 2>&1 || true
    fi
  fi
  if [[ "$dist_created" -eq 1 && -n "$dist_identity" ]]; then
    # rmdir removes dist only when it is still this invocation's empty directory.
    if same_directory_identity "$dist_dir" "$dist_identity"; then
      rmdir "$dist_dir" >/dev/null 2>&1 || true
    fi
  fi
  exit "$exit_code"
}
trap cleanup EXIT

require_darwin_arm64() {
  [[ "$(uname -s)" == "Darwin" ]] || fail "朋友现场测试套件要求 Darwin"
  [[ "$(uname -m)" == "arm64" ]] || fail "朋友现场测试套件要求 arm64"
}

assert_kit_layout() {
  local kit_root="$1"
  local relative
  local unexpected
  local file_count
  local directory_count

  [[ -d "$kit_root" && ! -L "$kit_root" ]] || fail "套件目录缺失或不安全"
  unexpected=$(find "$kit_root" \( -type l -o ! -type f ! -type d \) -print -quit)
  [[ -z "$unexpected" ]] || fail "套件包含 symlink 或不支持的文件类型"

  file_count=$(find "$kit_root" -type f | wc -l | tr -d '[:space:]')
  directory_count=$(find "$kit_root" -type d | wc -l | tr -d '[:space:]')
  [[ "$file_count" -eq 3 && "$directory_count" -eq 2 ]] || fail "套件布局超出允许的三个文件"

  while IFS= read -r relative; do
    if [[ "$relative" == "$kit_root" ]]; then
      relative=""
    else
      relative="${relative#$kit_root/}"
    fi
    case "$relative" in
      ""|claude-macos) ;;
      claude-macos/$candidate_dmg_name|claude-macos/$checksum_name|claude-macos/开始测试.md) ;;
      *) fail "套件包含未允许的路径" ;;
    esac
  done < <(find "$kit_root" -type d -print)

  while IFS= read -r relative; do
    relative="${relative#$kit_root/}"
    case "$relative" in
      claude-macos/$candidate_dmg_name|claude-macos/$checksum_name|claude-macos/开始测试.md) ;;
      *) fail "套件包含未允许的文件" ;;
    esac
  done < <(find "$kit_root" -type f -print)
}

assert_dmg_checksum() {
  local kit_root="$1"
  local dmg_path="$kit_root/claude-macos/$candidate_dmg_name"
  local checksum_path="$kit_root/claude-macos/$checksum_name"
  local line_count
  local digest
  local recorded_name
  local actual_digest

  [[ -f "$dmg_path" && ! -L "$dmg_path" ]] || fail "套件 DMG 缺失或不安全"
  [[ -f "$checksum_path" && ! -L "$checksum_path" ]] || fail "套件 checksum 缺失或不安全"
  line_count=$(wc -l <"$checksum_path" | tr -d '[:space:]')
  [[ "$line_count" -eq 1 ]] || fail "套件 checksum 必须只有一行"
  digest=$(awk 'NF { print $1; exit }' "$checksum_path")
  recorded_name=$(awk 'NF { print $NF; exit }' "$checksum_path")
  [[ "$digest" =~ ^[0-9A-Fa-f]{64}$ && "$recorded_name" == "$candidate_dmg_name" ]] || fail "套件 checksum 格式无效"
  actual_digest=$(shasum -a 256 "$dmg_path" | awk '{ print $1 }')
  [[ "$digest" == "$actual_digest" ]] || fail "套件 DMG 与 checksum 不匹配"
}

assert_zip_layout() {
  local archive_path="$1"
  if ! python3 - "$archive_path" "$kit_name" "$candidate_dmg_name" "$checksum_name" <<'PY'
from pathlib import Path
import stat
import sys
import zipfile

archive_path = Path(sys.argv[1])
kit_name, dmg_name, checksum_name = sys.argv[2:]
expected = {
    f"{kit_name}/claude-macos/{dmg_name}",
    f"{kit_name}/claude-macos/{checksum_name}",
    f"{kit_name}/claude-macos/开始测试.md",
}
with zipfile.ZipFile(archive_path) as archive:
    entries = archive.infolist()
    names = [entry.filename for entry in entries]
    modes = [(entry.external_attr >> 16) & 0o170000 for entry in entries]
    if len(names) != 3 or set(names) != expected or any(name.endswith("/") for name in names):
        raise SystemExit(1)
    if any(stat.S_ISLNK(mode) for mode in modes):
        raise SystemExit(1)
PY
  then
    fail "ZIP 布局不符合三个文件 allowlist"
  fi
}

require_darwin_arm64
command -v python3 >/dev/null 2>&1 || fail "缺少 python3"
command -v unzip >/dev/null 2>&1 || fail "缺少 unzip"
command -v shasum >/dev/null 2>&1 || fail "缺少 shasum"
[[ -x "$build_script" ]] || fail "现有 macOS 构建 wrapper 不可执行"
[[ -f "$gateway_verify_script" && -f "$verify_script" && -f "$scan_script" ]] || fail "release gate 脚本缺失"
[[ -f "$atomic_publish_script" ]] || fail "atomic publish helper 缺失"

if [[ -L "$dist_dir" || ( -e "$dist_dir" && ! -d "$dist_dir" ) ]]; then
  fail "dist 必须是实际目录"
fi
if [[ ! -e "$dist_dir" ]]; then
  mkdir "$dist_dir"
  dist_created=1
fi
[[ -d "$dist_dir" && ! -L "$dist_dir" ]] || fail "dist 必须是实际目录"
dist_identity=$(directory_identity "$dist_dir")
[[ -n "$dist_identity" ]] || fail "无法记录 dist 边界"

if [[ -L "$lock_dir" ]]; then
  fail "dist/.friend-test-kit.lock 不得是 symlink"
fi
if [[ -e "$lock_dir" ]]; then
  fail "dist/.friend-test-kit.lock 已存在，可能有另一个朋友测试套件准备任务正在运行"
fi
if ! mkdir "$lock_dir"; then
  fail "无法取得 dist/.friend-test-kit.lock"
fi
lock_created=1
lock_identity=$(directory_identity "$lock_dir")
[[ -n "$lock_identity" ]] || fail "无法记录 lock 边界"

if [[ -e "$final_zip" || -L "$final_zip" ]]; then
  fail "朋友测试 ZIP 目标已存在；拒绝覆盖旧包"
fi

# This preflight runs before every build and only gates the explicitly supplied
# endpoint; it does not assert a real upstream or key-bearing production path.
if ! python3 "$gateway_verify_script" --url "${FRIEND_GATEWAY_URL-}"; then
  fail "网关 preflight 未通过"
fi

temp_root=$(mktemp -d "${TMPDIR:-/tmp}/friend-test-kit.XXXXXX")
temp_identity=$(directory_identity "$temp_root")
[[ -n "$temp_identity" ]] || fail "无法记录构建临时目录边界"
release_dir="$temp_root/release/macos"
candidate_dmg="$release_dir/$candidate_dmg_name"
checksum_file="$release_dir/$checksum_name"

# The ZIP staging directory is hidden, unique, and in dist so its final file
# move cannot cross filesystems.
staging_root=$(mktemp -d "$dist_dir/.friend-test-kit.XXXXXX")
staging_created=1
staging_identity=$(directory_identity "$staging_root")
[[ -d "$staging_root" && ! -L "$staging_root" && -n "$staging_identity" ]] || fail "无法创建安全的 ZIP staging 目录"
staging_kit_root="$staging_root/$kit_name"
staging_dir="$staging_kit_root/claude-macos"
unpacked_dir="$temp_root/unpacked-before-publish"
post_unpacked_dir="$temp_root/unpacked-after-publish"
zip_path="$staging_root/$zip_name"
mkdir -p "$staging_dir" "$unpacked_dir"

# The build wrapper accepts this exact prepare-owned temp release boundary and
# never writes the repository's existing release/macos during this workflow.
export FRIEND_RELEASE_TEMP_ROOT="$temp_root"
export FRIEND_RELEASE_DIR="$release_dir"
bash "$build_script"

release_dir_identity=$(record_directory_identity "$release_dir") || fail "build 返回后 release_dir 缺失或不是普通目录"
candidate_dmg_identity=$(file_identity "$candidate_dmg") || fail "build 返回后 candidate DMG 缺失或不是普通文件"
checksum_identity=$(file_identity "$checksum_file") || fail "build 返回后 checksum 缺失或不是普通文件"
checksum_content_digest=$(shasum -a 256 "$checksum_file" | awk '{ print $1 }') || fail "无法记录 build checksum 内容 digest"
[[ "$checksum_content_digest" =~ ^[0-9A-Fa-f]{64}$ ]] || fail "build checksum 内容 digest 无效"

python3 "$verify_script" \
  --action artifact-check \
  --release-dir "$release_dir" \
  --require-release-files
assert_built_release_identity "release verifier"
assert_built_checksum_content_stability "release verifier"

source_digest=$(awk 'NF { print $1; exit }' "$checksum_file")
recorded_name=$(awk 'NF { print $NF; exit }' "$checksum_file")
[[ "$source_digest" =~ ^[0-9A-Fa-f]{64}$ ]] || fail "候选 checksum 格式无效"
[[ "$recorded_name" == "$candidate_dmg_name" ]] || fail "候选 checksum 文件名无效"
assert_built_release_identity "读取 checksum"
assert_built_checksum_content_stability "读取 checksum"

actual_digest=$(shasum -a 256 "$candidate_dmg" | awk '{ print $1 }')
assert_built_release_identity "计算实际 digest"
assert_built_checksum_content_stability "计算实际 digest"
[[ "$source_digest" == "$actual_digest" ]] || fail "候选 DMG 与 checksum 不匹配"

assert_built_release_identity "cp 到 staging 前"
cp -p "$candidate_dmg" "$staging_dir/$candidate_dmg_name"
cp -p "$checksum_file" "$staging_dir/$checksum_name"
assert_built_release_identity "cp 到 staging"
assert_built_checksum_content_stability "cp 到 staging"
assert_built_dmg_digest "cp 到 staging"
staging_dmg_identity=$(file_identity "$staging_dir/$candidate_dmg_name") || fail "cp 后 staging DMG 缺失或不是普通文件"
staging_checksum_identity=$(file_identity "$staging_dir/$checksum_name") || fail "cp 后 staging checksum 缺失或不是普通文件"
cat >"$staging_dir/开始测试.md" <<'EOF'
# 朋友现场测试：Claude macOS candidate

这是仅供现场陪同的候选测试包，不是通用发行版，也不是公开 Release。朋友只需按本页操作，不需要访问或阅读源码仓库。

## 开始前

- 仅支持 macOS Apple Silicon 的现场候选版本。
- 先安装官方 Claude Desktop，并确认它可以正常启动；本工具不会修改、替换或重新分发官方 App。
- Friend Key 由测试组织者单独提供，必须是有限额的测试 Key。Key 不在本包中，也不要放进压缩包或截图。

## 测试内容

1. 打开 Friend Launcher，确认固定 HTTPS 网关可达，并查看目录与余额状态。
2. 使用单独提供的有限额 Friend Key 完成一次配置。
3. 在官方 Claude Desktop 中只发送最小、非私人内容，验证一次响应、重启后的状态和基本恢复流程。

## 一键恢复官方模式

结束测试后，先退出官方 Claude Desktop；在 Friend Launcher 点击一次“恢复官方模式”，看到成功提示后再启动官方 Claude Desktop。

## 出现问题

立即停止测试，记录时间、macOS 版本、操作步骤和错误摘要。不要发送 Friend Key、私人对话或完整日志，也不要把源码仓库发给朋友。
EOF

assert_kit_layout "$staging_kit_root"
assert_dmg_checksum "$staging_kit_root"
assert_staged_payload_matches_build "$staging_kit_root"

python3 - "$zip_path" "$staging_kit_root" <<'PY'
from pathlib import Path
import sys
import zipfile

zip_path = Path(sys.argv[1])
kit_root = Path(sys.argv[2])
with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(kit_root.rglob("*")):
        if path.is_file():
            archive.write(path, f"{kit_root.name}/{path.relative_to(kit_root).as_posix()}")
PY

assert_staged_payload_matches_build "$staging_kit_root"
assert_zip_layout "$zip_path"
unzip -q "$zip_path" -d "$unpacked_dir"
assert_kit_layout "$unpacked_dir/$kit_name"
assert_dmg_checksum "$unpacked_dir/$kit_name"
assert_payload_matches_build "$unpacked_dir/$kit_name"
bash "$scan_script" "$root_dir" "$staging_kit_root" "$zip_path" "$unpacked_dir/$kit_name"

zip_identity=$(file_identity "$zip_path")
[[ -n "$zip_identity" ]] || fail "无法记录 staging ZIP 文件边界"

if [[ -e "$final_zip" || -L "$final_zip" ]]; then
  fail "朋友测试 ZIP 目标在发布前出现；拒绝覆盖"
fi
same_directory_identity "$staging_root" "$staging_identity" || fail "ZIP staging 目录在发布前被替换"
if ! python3 "$atomic_publish_script" "$zip_path" "$final_zip"; then
  fail "朋友测试 ZIP 原子发布失败或目标已存在"
fi
[[ ! -e "$zip_path" && ! -L "$zip_path" ]] || fail "朋友测试 ZIP 原子发布失败或目标已存在"
same_file_identity "$final_zip" "$zip_identity" || fail "朋友测试 ZIP 发布后 identity 不匹配"

# Re-open the final ZIP from its published path into a new temporary directory.
assert_zip_layout "$final_zip"
mkdir "$post_unpacked_dir"
unzip -q "$final_zip" -d "$post_unpacked_dir"
assert_kit_layout "$post_unpacked_dir/$kit_name"
assert_dmg_checksum "$post_unpacked_dir/$kit_name"
assert_payload_matches_build "$post_unpacked_dir/$kit_name"
bash "$scan_script" "$root_dir" "$final_zip" "$post_unpacked_dir/$kit_name"
same_file_identity "$final_zip" "$zip_identity" || fail "朋友测试 ZIP 在发布复验后被替换"

echo "朋友现场测试套件: PASS: $final_zip"
