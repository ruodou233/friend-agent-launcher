#!/usr/bin/env bash
set -euo pipefail

root_dir=$(cd "$(dirname "$0")/.." && pwd)
release_dir="$root_dir/release/macos"
support_file="$root_dir/release-support.json"
verify_script="$root_dir/scripts/verify-release-support.py"
scan_script="$root_dir/scripts/scan-secrets.sh"
candidate_dmg="$release_dir/Friend-Claude-0.1.0-macos-arm64-candidate.dmg"
checksum_file="$release_dir/SHA256SUMS-candidate.txt"
candidate_dmg_name="Friend Claude_0.1.0_aarch64.dmg"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "V1A release gate: Claude macOS candidate builds require macOS." >&2
  exit 1
fi

node_version=$(node --version)
if [[ ! "$node_version" =~ ^v22\. ]]; then
  echo "V1A release gate: Node.js 22.x is required; found $node_version." >&2
  exit 1
fi

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

mkdir -p "$release_dir"
python3 "$verify_script" \
  --action artifact-check \
  --release-dir "$release_dir"
rm -f "$candidate_dmg" "$checksum_file"

cargo_target_dir="$(mktemp -d "${TMPDIR:-/tmp}/friend-agent-launcher-cargo-target.XXXXXX")"
mount_dir=""
mounted=0
cleanup() {
  if [[ "$mounted" -eq 1 ]]; then
    hdiutil detach "$mount_dir" -quiet >/dev/null 2>&1 || true
  fi
  if [[ -n "$mount_dir" && -d "$mount_dir" ]]; then
    rmdir "$mount_dir" >/dev/null 2>&1 || true
  fi
  if [[ -n "$cargo_target_dir" && -d "$cargo_target_dir" ]]; then
    find "$cargo_target_dir" -depth -delete >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

export CARGO_TARGET_DIR="$cargo_target_dir"

cd "$root_dir"
npm exec -- tauri build --config src-tauri/tauri.claude.conf.json -- --locked

bundle_dir="$cargo_target_dir/release/bundle"
python3 "$verify_script" \
  --action artifact-check \
  --candidate-bundle "$bundle_dir"
cp "$bundle_dir/dmg/$candidate_dmg_name" "$candidate_dmg"

shasum -a 256 \
  "$candidate_dmg" \
  >"$checksum_file"

python3 "$verify_script" \
  --action artifact-check \
  --release-dir "$release_dir" \
  --require-release-files

command -v hdiutil >/dev/null 2>&1 || {
  echo "V1A release gate: hdiutil is required to scan the candidate DMG." >&2
  exit 1
}

mount_dir="$(mktemp -d "${TMPDIR:-/tmp}/friend-agent-dmg.XXXXXX")"

# Read-only mounting exposes the DMG's real app bundle to the same scanner.
hdiutil attach -readonly -nobrowse -mountpoint "$mount_dir" "$candidate_dmg" >/dev/null
mounted=1
bash "$scan_script" "$root_dir" "$mount_dir"
