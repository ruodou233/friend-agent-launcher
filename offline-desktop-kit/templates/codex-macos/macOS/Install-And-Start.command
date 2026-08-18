#!/bin/zsh
set -euo pipefail

PACKAGE_DIR="$(cd -- "$(dirname -- "$0")/.." && pwd)"
MANIFEST="$PACKAGE_DIR/package-manifest.json"
ASSETS="$PACKAGE_DIR/assets"
ACTIVE_MOUNT=""

die() { print -u2 "Installation failed: $*"; exit 1; }
read_manifest() { /usr/bin/plutil -extract "$1" raw -o - "$MANIFEST"; }
cleanup_mount() {
  if [[ -n "$ACTIVE_MOUNT" ]]; then
    /usr/bin/hdiutil detach "$ACTIVE_MOUNT" -quiet >/dev/null 2>&1 || true
    /bin/rmdir "$ACTIVE_MOUNT" >/dev/null 2>&1 || true
    ACTIVE_MOUNT=""
  fi
}
trap cleanup_mount EXIT INT TERM

CONSOLE_USER="$(/usr/bin/stat -f '%Su' /dev/console 2>/dev/null || true)"
if [[ -z "$CONSOLE_USER" || "$CONSOLE_USER" == "root" || "$CONSOLE_USER" == "loginwindow" ]]; then
  CONSOLE_USER="$(/usr/bin/id -un)"
fi
[[ "$CONSOLE_USER" != "root" ]] || die "No signed-in desktop user was found. Run the installer from a normal user session."
TARGET_HOME="$(/usr/bin/dscl . -read "/Users/$CONSOLE_USER" NFSHomeDirectory 2>/dev/null | /usr/bin/awk '{print $2}')"
[[ -d "$TARGET_HOME" ]] || die "Cannot resolve the desktop user's home directory."
TARGET_GROUP="$(/usr/bin/id -gn "$CONSOLE_USER")"
USER_APPS="$TARGET_HOME/Applications"

[[ "$(uname -s)" == "Darwin" ]] || die "This package only supports macOS."
[[ -f "$MANIFEST" ]] || die "Missing package-manifest.json"

client_label="$(read_manifest client_label)"
client_app_name="$(read_manifest client_app_name)"
client_bundle_id="$(read_manifest client_bundle_id)"
official_team_identifier="$(read_manifest official_team_identifier)"
client_asset="$(read_manifest official_installer_file)"

mkdir -p "$USER_APPS"

version_at_least() {
  /usr/bin/awk -v have="$1" -v need="$2" 'BEGIN {
    hn=split(have,h,"."); nn=split(need,n,"."); max=(hn>nn?hn:nn)
    for (i=1;i<=max;i++) { hv=h[i]+0; nv=n[i]+0; if (hv>nv) exit 0; if (hv<nv) exit 1 }
    exit 0
  }'
}

install_dmg_app() {
  local dmg="$1"
  local expected_bundle="$2"
  local target_name="$3"
  local expected_team="${4:-}"
  ACTIVE_MOUNT="$(mktemp -d /tmp/friend-desktop-installer.XXXXXX)"
  /usr/bin/hdiutil attach "$dmg" -nobrowse -readonly -mountpoint "$ACTIVE_MOUNT" >/dev/null || die "Cannot mount $dmg"
  local app target existing_target source_version target_version
  app="$(/usr/bin/find "$ACTIVE_MOUNT" -maxdepth 2 -name '*.app' -print -quit)"
  [[ -n "$app" ]] || die "No app found in $dmg"
  local actual_bundle
  actual_bundle="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$app/Contents/Info.plist" 2>/dev/null || true)"
  [[ "$actual_bundle" == "$expected_bundle" ]] || die "Unexpected app identity: $actual_bundle"
  /usr/bin/codesign --verify --deep --strict "$app" >/dev/null 2>&1 || die "Code signature verification failed: $target_name"
  /usr/sbin/spctl --assess --type execute "$app" >/dev/null 2>&1 || die "Gatekeeper rejected: $target_name"
  if [[ -n "$expected_team" ]]; then
    local actual_team
    actual_team="$(/usr/bin/codesign -dv --verbose=4 "$app" 2>&1 | /usr/bin/awk -F= '/^TeamIdentifier=/{print $2; exit}')"
    [[ "$actual_team" == "$expected_team" ]] || die "Unexpected signing team for $target_name: $actual_team"
  fi
  target="$USER_APPS/$target_name.app"
  source_version="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$app/Contents/Info.plist")"
  existing_target=""
  if [[ -d "$target" ]]; then
    existing_target="$target"
  elif [[ -d "/Applications/$target_name.app" ]]; then
    existing_target="/Applications/$target_name.app"
  fi
  if [[ -n "$existing_target" ]]; then
    local target_bundle target_team
    target_bundle="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$existing_target/Contents/Info.plist" 2>/dev/null || true)"
    target_team="$(/usr/bin/codesign -dv --verbose=4 "$existing_target" 2>&1 | /usr/bin/awk -F= '/^TeamIdentifier=/{print $2; exit}')"
    /usr/bin/codesign --verify --deep --strict "$existing_target" >/dev/null 2>&1 || die "Installed $target_name has an invalid signature."
    [[ "$target_bundle" == "$expected_bundle" && "$target_team" == "$expected_team" ]] || die "Installed $target_name has an unexpected identity."
    target_version="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "$existing_target/Contents/Info.plist")"
    if version_at_least "$target_version" "$source_version"; then
      print "$target_name $target_version is already installed; skipping the same or older bundled client."
      cleanup_mount
      return
    fi
    if [[ "$existing_target" == "$target" ]]; then /bin/rm -rf "$target"; fi
  fi
  /usr/bin/ditto "$app" "$target"
  cleanup_mount
}

client_installer="$ASSETS/$client_asset"
[[ -f "$client_installer" ]] || die "离线安装包缺少官方 $client_label 安装器；本包不会访问境外下载源。"
install_dmg_app "$client_installer" "$client_bundle_id" "$client_app_name" "$official_team_identifier"

runtime_archive="$ASSETS/codex-primary-runtime.tar.xz"
runtime_sha="$(read_manifest codex_primary_runtime_sha256)"
[[ -f "$runtime_archive" ]] || die "离线包缺少 Codex Primary Runtime。"
actual_runtime_sha="$(/usr/bin/shasum -a 256 "$runtime_archive" | /usr/bin/awk '{print $1}')"
[[ "$actual_runtime_sha" == "$runtime_sha" ]] || die "Codex Primary Runtime 校验失败。"
runtime_parent="$TARGET_HOME/.cache/codex-runtimes"
/bin/mkdir -p "$runtime_parent"
/usr/bin/tar -xJf "$runtime_archive" -C "$runtime_parent"
installed_runtime="$(/usr/bin/plutil -extract bundleVersion raw -o - "$runtime_parent/codex-primary-runtime/runtime.json" 2>/dev/null || true)"
expected_runtime="$(read_manifest codex_primary_runtime_version)"
[[ "$installed_runtime" == "$expected_runtime" ]] || die "Codex Primary Runtime 版本不匹配。"

cc_dmg="$ASSETS/CC-Switch.dmg"
[[ -f "$cc_dmg" ]] || die "离线安装包缺少 CC Switch；本包不会访问境外下载源。"
install_dmg_app "$cc_dmg" "$(read_manifest cc_switch_bundle_id)" "CC Switch" "$(read_manifest cc_switch_team_identifier)"

if [[ "$(/usr/bin/id -u)" == "0" ]]; then
  for owned_path in \
    "$USER_APPS/$client_app_name.app" \
    "$USER_APPS/CC Switch.app" \
    "$TARGET_HOME/.cache/codex-runtimes"
  do
    if [[ -e "$owned_path" ]]; then
      /usr/sbin/chown -R "$CONSOLE_USER:$TARGET_GROUP" "$owned_path"
    fi
  done
fi

print "Installed official $client_label, its offline runtime, and CC Switch for $CONSOLE_USER."
print "No API key was bundled. Ask the local Agent to configure the chosen Provider and verify a real request."
