#!/bin/zsh
set -euo pipefail

PACKAGE_DIR="$(cd -- "$(dirname -- "$0")/.." && pwd)"
MANIFEST="$PACKAGE_DIR/package-manifest.json"
ASSETS="$PACKAGE_DIR/assets"
ACTIVE_MOUNT=""

die() { print -u2 "安装失败：$*"; exit 1; }
read_manifest() { /usr/bin/plutil -extract "$1" raw -o - "$MANIFEST"; }
cleanup_mount() {
  if [[ -n "$ACTIVE_MOUNT" ]]; then
    /usr/bin/hdiutil detach "$ACTIVE_MOUNT" -quiet >/dev/null 2>&1 || true
    /bin/rmdir "$ACTIVE_MOUNT" >/dev/null 2>&1 || true
    ACTIVE_MOUNT=""
  fi
}
trap cleanup_mount EXIT INT TERM

# When an AI helper launches this script through sudo, $HOME may be /var/root.
# Always install into the real desktop user's home instead of the elevated account.
CONSOLE_USER="$(/usr/bin/stat -f '%Su' /dev/console 2>/dev/null || true)"
if [[ -z "$CONSOLE_USER" || "$CONSOLE_USER" == "root" || "$CONSOLE_USER" == "loginwindow" ]]; then
  CONSOLE_USER="$(/usr/bin/id -un)"
fi
[[ "$CONSOLE_USER" != "root" ]] || die "未找到已登录的桌面用户，请以普通用户重新运行。"
TARGET_HOME="$(/usr/bin/dscl . -read "/Users/$CONSOLE_USER" NFSHomeDirectory 2>/dev/null | /usr/bin/awk '{print $2}')"
[[ -d "$TARGET_HOME" ]] || die "无法确定桌面用户的主目录。"
TARGET_GROUP="$(/usr/bin/id -gn "$CONSOLE_USER")"
USER_APPS="$TARGET_HOME/Applications"

assert_sha256() {
  local file="$1" expected="$2"
  [[ -f "$file" ]] || die "缺少离线素材：$file"
  local actual
  actual="$(/usr/bin/shasum -a 256 "$file" | /usr/bin/awk '{print $1}')"
  [[ "$actual" == "$expected" ]] || die "SHA-256 校验失败：$file"
}

verify_app() {
  local app="$1" expected_bundle="$2" expected_team="$3" label="$4"
  local actual_bundle actual_team
  actual_bundle="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$app/Contents/Info.plist" 2>/dev/null || true)"
  [[ "$actual_bundle" == "$expected_bundle" ]] || die "$label 的 Bundle ID 不匹配：$actual_bundle"
  /usr/bin/codesign --verify --deep --strict "$app" >/dev/null 2>&1 || die "$label 的代码签名校验失败。"
  /usr/sbin/spctl --assess --type execute "$app" >/dev/null 2>&1 || die "$label 未通过 Gatekeeper。"
  actual_team="$(/usr/bin/codesign -dv --verbose=4 "$app" 2>&1 | /usr/bin/awk -F= '/^TeamIdentifier=/{print $2; exit}')"
  [[ "$actual_team" == "$expected_team" ]] || die "$label 的签名 Team ID 不匹配：$actual_team"
}

version_at_least() {
  /usr/bin/awk -v have="$1" -v need="$2" 'BEGIN {
    hn=split(have,h,"."); nn=split(need,n,"."); max=(hn>nn?hn:nn)
    for (i=1;i<=max;i++) { hv=h[i]+0; nv=n[i]+0; if (hv>nv) exit 0; if (hv<nv) exit 1 }
    exit 0
  }'
}

install_dmg_app() {
  local dmg="$1" expected_bundle="$2" expected_team="$3" target_name="$4"
  /usr/bin/hdiutil verify "$dmg" >/dev/null 2>&1 || die "DMG 校验失败：$dmg"
  ACTIVE_MOUNT="$(mktemp -d /tmp/friend-desktop-installer.XXXXXX)"
  /usr/bin/hdiutil attach "$dmg" -nobrowse -readonly -mountpoint "$ACTIVE_MOUNT" >/dev/null || die "无法挂载：$dmg"
  local app target existing_target source_version target_version
  app="$(/usr/bin/find "$ACTIVE_MOUNT" -maxdepth 2 -name '*.app' -print -quit)"
  [[ -n "$app" ]] || die "DMG 中没有找到 App：$dmg"
  verify_app "$app" "$expected_bundle" "$expected_team" "$target_name"
  target="$USER_APPS/$target_name.app"
  source_version="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$app/Contents/Info.plist")"
  existing_target=""
  if [[ -d "$target" ]]; then
    existing_target="$target"
  elif [[ -d "/Applications/$target_name.app" ]]; then
    existing_target="/Applications/$target_name.app"
  fi
  if [[ -n "$existing_target" ]]; then
    verify_app "$existing_target" "$expected_bundle" "$expected_team" "$target_name (installed)"
    target_version="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$existing_target/Contents/Info.plist")"
    if version_at_least "$target_version" "$source_version"; then
      print "$target_name $target_version 已安装，跳过同版或较旧的离线客户端。"
      cleanup_mount
      return
    fi
    if [[ "$existing_target" == "$target" ]]; then /bin/rm -rf "$target"; fi
  fi
  /usr/bin/ditto "$app" "$target"
  cleanup_mount
}

[[ "$(uname -s)" == "Darwin" ]] || die "本素材包仅支持 macOS。"
[[ -f "$MANIFEST" ]] || die "缺少 package-manifest.json。"
/bin/mkdir -p "$USER_APPS"

client_dmg="$ASSETS/$(read_manifest official_installer_file)"
assert_sha256 "$client_dmg" "$(read_manifest official_installer_sha256)"
install_dmg_app "$client_dmg" "$(read_manifest client_bundle_id)" "$(read_manifest official_team_identifier)" "$(read_manifest client_app_name)"

engine_version="$(read_manifest claude_code_engine_version)"
case "$(uname -m)" in
  arm64)
    engine_source="$ASSETS/claude-code-engine-arm64.app"
    engine_sha="$(read_manifest claude_code_engine_arm64_sha256)"
    engine_marker="$(read_manifest claude_code_engine_marker)"
    ;;
  x86_64)
    engine_source="$ASSETS/claude-code-engine-x64.app"
    engine_sha="$(read_manifest claude_code_engine_x64_sha256)"
    engine_marker="$(read_manifest claude_code_engine_marker_x64)"
    ;;
  *) die "不支持的 Mac 架构：$(uname -m)" ;;
esac
[[ -d "$engine_source" ]] || die "缺少当前架构的 Claude Code 引擎。"
assert_sha256 "$engine_source/Contents/MacOS/claude" "$engine_sha"
verify_app "$engine_source" "$(read_manifest claude_code_engine_bundle_id)" "$(read_manifest claude_code_engine_team_identifier)" "Claude Code Engine"
for engine_root in \
  "$TARGET_HOME/Library/Application Support/Claude/claude-code" \
  "$TARGET_HOME/Library/Application Support/Claude-3p/claude-code"
do
  engine_dir="$engine_root/$engine_version"
  /bin/mkdir -p "$engine_dir"
  if [[ -e "$engine_dir/claude.app" ]]; then
    /bin/rm -rf "$engine_dir/claude.app"
  fi
  /usr/bin/ditto "$engine_source" "$engine_dir/claude.app"
  /usr/bin/printf '%s' "$engine_marker" > "$engine_dir/.verified"
  /bin/chmod 600 "$engine_dir/.verified"
done

cc_dmg="$ASSETS/CC-Switch.dmg"
assert_sha256 "$cc_dmg" "$(read_manifest cc_switch_sha256)"
install_dmg_app "$cc_dmg" "$(read_manifest cc_switch_bundle_id)" "$(read_manifest cc_switch_team_identifier)" "CC Switch"

# Files created by an elevated helper must still belong to the desktop user.
if [[ "$(/usr/bin/id -u)" == "0" ]]; then
  for owned_path in \
    "$USER_APPS/$(read_manifest client_app_name).app" \
    "$USER_APPS/CC Switch.app" \
    "$TARGET_HOME/Library/Application Support/Claude/claude-code" \
    "$TARGET_HOME/Library/Application Support/Claude-3p/claude-code"
  do
    if [[ -e "$owned_path" ]]; then
      /usr/sbin/chown -R "$CONSOLE_USER:$TARGET_GROUP" "$owned_path"
    fi
  done
fi

print ''
print "安装完成：官方 Claude Desktop、普通/第三方线路的 Claude Code 引擎和 CC Switch 已安装给 $CONSOLE_USER。"
if [[ "${FRIEND_AGENT_KIT_SKIP_OPEN:-0}" != "1" ]]; then
  if [[ "$(/usr/bin/id -u)" == "0" ]]; then
    /usr/bin/sudo -u "$CONSOLE_USER" /usr/bin/open "$USER_APPS/CC Switch.app" >/dev/null 2>&1 || true
  else
    /usr/bin/open "$USER_APPS/CC Switch.app" >/dev/null 2>&1 || true
  fi
fi
