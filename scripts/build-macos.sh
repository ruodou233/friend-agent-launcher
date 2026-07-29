#!/usr/bin/env bash
set -euo pipefail

root_dir=$(cd "$(dirname "$0")/.." && pwd)
release_dir="$root_dir/release/macos"

mkdir -p "$release_dir"
rm -f "$release_dir"/Friend-Claude-*.dmg "$release_dir"/Friend-Codex-*.dmg

cd "$root_dir"
npm run desktop:build:claude
cp "src-tauri/target/release/bundle/dmg/Friend Claude_0.1.0_aarch64.dmg" \
  "$release_dir/Friend-Claude-0.1.0-macos-arm64.dmg"

npm run desktop:build:codex
cp "src-tauri/target/release/bundle/dmg/Friend Codex_0.1.0_aarch64.dmg" \
  "$release_dir/Friend-Codex-0.1.0-macos-arm64.dmg"

shasum -a 256 "$release_dir"/*.dmg >"$release_dir/SHA256SUMS.txt"
