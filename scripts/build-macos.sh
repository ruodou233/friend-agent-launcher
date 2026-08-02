#!/usr/bin/env bash
set -euo pipefail

root_dir=$(cd "$(dirname "$0")/.." && pwd -P)
default_release_dir="$root_dir/release/macos"
release_dir="${FRIEND_RELEASE_DIR:-$default_release_dir}"
release_parent=$(dirname "$release_dir")
support_file="$root_dir/release-support.json"
verify_script="$root_dir/scripts/verify-release-support.py"
scan_script="$root_dir/scripts/scan-secrets.sh"
atomic_publish_script="$root_dir/scripts/atomic-publish-macos.py"
candidate_dmg_name="Friend-Claude-0.1.0-macos-arm64-candidate.dmg"
checksum_name="SHA256SUMS-candidate.txt"

cargo_target_dir=""
cargo_target_identity=""
mount_dir=""
mount_dir_identity=""
mounted_identity=""
mounted=0
staging_dir=""
staging_identity=""
staging_created=0
tauri_frontend_dir=""
tauri_config_overlay=""
staged_dmg_identity=""
staged_checksum_identity=""
staged_checksum_content_digest=""

fail() {
  echo "V1A release gate: BLOCKED: $1" >&2
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

verify_final_release_artifacts() {
  local context="$1"
  local final_dmg="$release_dir/$candidate_dmg_name"
  local final_checksum="$release_dir/$checksum_name"
  local final_digest
  local final_checksum_content_digest
  local recorded_digest
  local recorded_name

  same_directory_identity "$release_dir" "$staging_identity" || fail "$context 后 release/macos 目录 identity 不匹配"
  same_file_identity "$final_dmg" "$staged_dmg_identity" || fail "$context 后最终 DMG identity 不再是 staged 文件"
  same_file_identity "$final_checksum" "$staged_checksum_identity" || fail "$context 后最终 checksum identity 不再是 staged 文件"

  final_digest=$(shasum -a 256 "$final_dmg" | awk '{ print $1 }') || fail "$context 后无法计算最终 DMG digest"
  [[ "$final_digest" == "$source_digest" ]] || fail "$context 后最终 DMG digest 不匹配 staged digest"
  final_checksum_content_digest=$(shasum -a 256 "$final_checksum" | awk '{ print $1 }') || fail "$context 后无法计算最终 checksum 内容 digest"
  [[ "$final_checksum_content_digest" == "$staged_checksum_content_digest" ]] || fail "$context 后最终 checksum 内容不匹配 staged checksum"
  recorded_digest=$(awk 'NF { print $1; exit }' "$final_checksum")
  recorded_name=$(awk 'NF { print $NF; exit }' "$final_checksum")
  [[ "$recorded_digest" == "$source_digest" && "$recorded_name" == "$candidate_dmg_name" ]] || fail "$context 后最终 checksum 内容无效"
}

cleanup() {
  local exit_code=$?
  if [[ "$mounted" -eq 1 && -n "$mounted_identity" ]] &&
    same_directory_identity "$mount_dir" "$mounted_identity"; then
    hdiutil detach "$mount_dir" -quiet >/dev/null 2>&1 || true
    mounted=0
  fi
  if [[ -n "$mount_dir" && -n "$mount_dir_identity" ]] &&
    same_directory_identity "$mount_dir" "$mount_dir_identity"; then
    rmdir "$mount_dir" >/dev/null 2>&1 || true
  fi

  # Only this invocation's mktemp directory is eligible for recursive cleanup.
  # The identity check prevents deleting a directory that replaced the path.
  if [[ "$staging_created" -eq 1 && -n "$staging_dir" ]]; then
    case "$staging_dir" in
      "$release_parent"/.friend-release-macos.*)
        if same_directory_identity "$staging_dir" "$staging_identity"; then
          find "$staging_dir" -depth -delete >/dev/null 2>&1 || true
        fi
        ;;
    esac
  fi
  if [[ -n "$cargo_target_dir" && -n "$cargo_target_identity" ]]; then
    case "$cargo_target_dir" in
      "${TMPDIR:-/tmp}"/friend-agent-launcher-cargo-target.*)
        if same_directory_identity "$cargo_target_dir" "$cargo_target_identity"; then
          find "$cargo_target_dir" -depth -delete >/dev/null 2>&1 || true
        fi
        ;;
    esac
  fi
  exit "$exit_code"
}
trap cleanup EXIT

require_darwin_arm64() {
  [[ "$(uname -s)" == "Darwin" ]] || fail "Claude macOS candidate 构建要求 Darwin"
  [[ "$(uname -m)" == "arm64" ]] || fail "Claude macOS candidate 构建要求 arm64"
}

validate_release_boundary() {
  local temp_root
  if [[ "$release_dir" == "$default_release_dir" ]]; then
    :
  else
    temp_root="${FRIEND_RELEASE_TEMP_ROOT-}"
    [[ -n "$temp_root" ]] || fail "FRIEND_RELEASE_DIR 只允许由 prepare 提供"
    [[ "$release_dir" == "$temp_root/release/macos" ]] || fail "FRIEND_RELEASE_DIR 超出 prepare temp_root 边界"
    [[ -d "$temp_root" && ! -L "$temp_root" ]] || fail "FRIEND_RELEASE_TEMP_ROOT 必须是实际目录"
  fi

  # The destination must be absent from the beginning; a symlink counts as
  # existing even when its target is missing.
  if [[ -e "$release_dir" || -L "$release_dir" ]]; then
    fail "release/macos 目标已存在；拒绝覆盖"
  fi

  if [[ -L "$release_parent" || ( -e "$release_parent" && ! -d "$release_parent" ) ]]; then
    fail "release/macos 的父目录必须是实际目录且不得是 symlink"
  fi
  if [[ ! -e "$release_parent" ]]; then
    local grandparent
    grandparent=$(dirname "$release_parent")
    [[ -d "$grandparent" && ! -L "$grandparent" ]] || fail "release/macos 的父目录无法安全创建"
  fi
}

require_darwin_arm64
command -v node >/dev/null 2>&1 || fail "缺少 Node.js"
command -v python3 >/dev/null 2>&1 || fail "缺少 python3"
command -v shasum >/dev/null 2>&1 || fail "缺少 shasum"
node_version=$(node --version)
[[ "$node_version" =~ ^v22\. ]] || fail "Node.js 22.x 是门禁基线；当前为 $node_version"

[[ -x "$verify_script" || -f "$verify_script" ]] || fail "release verifier 缺失"
[[ -f "$scan_script" ]] || fail "secret scan 脚本缺失"
[[ -f "$atomic_publish_script" ]] || fail "atomic publish helper 缺失"
validate_release_boundary

# This marker is only an accidental-misuse guard. CI, the support matrix,
# artifact allowlists, secret scan, and P0 evidence are the release authority.
export FRIEND_RELEASE_BUILD_WRAPPER="scripts/build-macos.sh"
python3 "$verify_script" \
  --file "$support_file" \
  --product claude \
  --system macos \
  --action build \
  --require-host macos \
  --require-wrapper

# The default parent may be absent, but create only this one real directory;
# never let mkdir -p follow a symlink in the release boundary.
if [[ ! -e "$release_parent" ]]; then
  mkdir "$release_parent"
fi
[[ -d "$release_parent" && ! -L "$release_parent" ]] || fail "release/macos 的父目录必须是实际目录且不得是 symlink"
[[ ! -e "$release_dir" && ! -L "$release_dir" ]] || fail "release/macos 目标在构建前出现；拒绝覆盖"

# The staging directory is in the final parent, so directory rename is atomic
# on the same filesystem and cannot expose a half-published release tree.
staging_dir=$(mktemp -d "$release_parent/.friend-release-macos.XXXXXX")
staging_created=1
[[ -d "$staging_dir" && ! -L "$staging_dir" ]] || fail "无法创建安全的 release staging 目录"
staging_identity=$(directory_identity "$staging_dir")
[[ -n "$staging_identity" ]] || fail "无法记录 release staging 目录边界"

cargo_target_dir=$(mktemp -d "${TMPDIR:-/tmp}/friend-agent-launcher-cargo-target.XXXXXX")
cargo_target_identity=$(directory_identity "$cargo_target_dir")
[[ -n "$cargo_target_identity" ]] || fail "无法记录 cargo 临时目录边界"

# The overlay is inside this invocation's cargo temp tree, so the existing
# identity-guarded recursive cleanup removes it with the rest of the temp tree.
tauri_frontend_dir="$cargo_target_dir/frontend"
tauri_config_overlay="$cargo_target_dir/tauri.claude.overlay.json"
mkdir "$tauri_frontend_dir"
[[ -d "$tauri_frontend_dir" && ! -L "$tauri_frontend_dir" ]] || fail "无法创建安全的 Tauri 临时 frontend 目录"
frontend_dir_shell=$(python3 -c 'import shlex, sys; print(shlex.quote(sys.argv[1]))' "$tauri_frontend_dir")
tauri_before_build_command="python3 scripts/verify-release-support.py --product claude --system macos --action build --require-host macos --require-wrapper && node node_modules/vite/bin/vite.js build --mode claude --outDir $frontend_dir_shell"
python3 - "$tauri_config_overlay" "$tauri_frontend_dir" "$tauri_before_build_command" <<'PY'
import json
import os
import sys

overlay_path, frontend_dir, before_build_command = sys.argv[1:]
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
file_descriptor = os.open(overlay_path, flags, 0o600)
with os.fdopen(file_descriptor, "w", encoding="utf-8") as overlay:
    json.dump(
        {
            "build": {
                "frontendDist": frontend_dir,
                "beforeBuildCommand": before_build_command,
            }
        },
        overlay,
        ensure_ascii=False,
        indent=2,
    )
    overlay.write("\n")
PY
[[ -f "$tauri_config_overlay" && ! -L "$tauri_config_overlay" ]] || fail "无法创建安全的 Tauri config overlay"

export CARGO_TARGET_DIR="$cargo_target_dir"

cd "$root_dir"
# Tauri merges --config arguments in order; the temp overlay intentionally wins
# over the existing Claude config only for the frontend build paths/command.
node "$root_dir/node_modules/@tauri-apps/cli/tauri.js" build \
  --config src-tauri/tauri.claude.conf.json \
  --config "$tauri_config_overlay" -- --locked

bundle_dir="$cargo_target_dir/release/bundle"
python3 "$verify_script" \
  --action artifact-check \
  --candidate-bundle "$bundle_dir"

staged_dmg="$staging_dir/$candidate_dmg_name"
staged_checksum="$staging_dir/$checksum_name"
cp -p "$bundle_dir/dmg/Friend Claude_0.1.0_aarch64.dmg" "$staged_dmg"
source_digest=$(shasum -a 256 "$staged_dmg" | awk '{ print $1 }')
[[ "$source_digest" =~ ^[0-9A-Fa-f]{64}$ ]] || fail "candidate DMG checksum 生成失败"
printf '%s  %s\n' "$source_digest" "$candidate_dmg_name" >"$staged_checksum"

python3 "$verify_script" \
  --action artifact-check \
  --release-dir "$staging_dir" \
  --require-release-files

staged_dmg_identity=$(file_identity "$staged_dmg") || fail "无法记录 staged DMG 文件 identity"
staged_checksum_identity=$(file_identity "$staged_checksum") || fail "无法记录 staged checksum 文件 identity"
staged_checksum_content_digest=$(shasum -a 256 "$staged_checksum" | awk '{ print $1 }') || fail "无法记录 staged checksum 内容 digest"
[[ "$staged_checksum_content_digest" =~ ^[0-9A-Fa-f]{64}$ ]] || fail "staged checksum 内容 digest 无效"

command -v hdiutil >/dev/null 2>&1 || fail "hdiutil 是扫描 candidate DMG 所必需的"
mount_dir=$(mktemp -d "${TMPDIR:-/tmp}/friend-agent-dmg.XXXXXX")
mount_dir_identity=$(directory_identity "$mount_dir")
[[ -n "$mount_dir_identity" ]] || fail "无法记录 DMG 挂载目录边界"

# Read-only mounting exposes the DMG's real app bundle to the same scanner.
same_directory_identity "$mount_dir" "$mount_dir_identity" || fail "hdiutil attach 前 DMG 挂载目录 identity 不匹配"
hdiutil attach -readonly -nobrowse -mountpoint "$mount_dir" "$staged_dmg" >/dev/null
mounted=1
mounted_identity=$(directory_identity "$mount_dir")
[[ -n "$mounted_identity" ]] || fail "无法记录已挂载 DMG 的目录边界"
python3 "$verify_script" \
  --action artifact-check \
  --dmg-mount "$mount_dir"

app_binary="$mount_dir/Friend Claude.app/Contents/MacOS/friend-agent-launcher"
[[ -x /usr/bin/lipo ]] || fail "缺少 /usr/bin/lipo"
app_archs=$(/usr/bin/lipo -archs "$app_binary") || fail "无法读取 candidate Mach-O 架构"
python3 "$verify_script" \
  --action macho-archs \
  --mach-o-archs "$app_archs"

bash "$scan_script" "$root_dir" "$mount_dir"

same_directory_identity "$mount_dir" "$mounted_identity" || fail "DMG 挂载目录在 detach 前被替换"
hdiutil detach "$mount_dir" -quiet >/dev/null
mounted=0
same_directory_identity "$mount_dir" "$mount_dir_identity" || fail "DMG 挂载目录在 detach 后被替换"
rmdir "$mount_dir" >/dev/null
mount_dir=""
mount_dir_identity=""
mounted_identity=""

same_directory_identity "$staging_dir" "$staging_identity" || fail "release staging 目录在发布前被替换"
[[ ! -e "$release_dir" && ! -L "$release_dir" ]] || fail "release/macos 在发布前出现；拒绝覆盖"
if ! python3 "$atomic_publish_script" "$staging_dir" "$release_dir"; then
  fail "release/macos 原子发布失败或目标已存在"
fi
[[ ! -e "$staging_dir" && ! -L "$staging_dir" ]] || fail "release/macos 原子发布失败或目标已存在"
same_directory_identity "$release_dir" "$staging_identity" || fail "release/macos 发布后 identity 不匹配"
verify_final_release_artifacts "release/macos 原子发布"

# Validate the renamed tree without ever deleting the final path. If a race
# replaced the destination, this check fails while leaving that external path.
python3 "$verify_script" \
  --action artifact-check \
  --release-dir "$release_dir" \
  --require-release-files
same_directory_identity "$release_dir" "$staging_identity" || fail "release/macos 在发布复验后被替换"
verify_final_release_artifacts "最终 release verifier"
staging_created=0
staging_dir=""

echo "V1A release gate: PASS: Claude macOS arm64 candidate built at $release_dir/$candidate_dmg_name"
