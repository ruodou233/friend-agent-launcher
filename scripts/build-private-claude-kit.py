#!/usr/bin/env python3
"""Build a fail-closed, fresh-install Friend Claude candidate kit."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import sys
import textwrap
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit


REPO_ROOT = Path(__file__).resolve().parents[1]
PROFILE_ID = "friend-private-candidate-v1"
OWNER = "friend-agent-private-kit"
PRODUCT = "claude"
INSTALLER_NAMES = {"macos": "Claude.dmg", "windows": "Claude.msix"}


class BuildError(Exception):
    pass


def require_text(label: str, value: str) -> str:
    if not value or not value.strip() or "\n" in value or "\r" in value or "\x00" in value:
        raise BuildError(f"{label} must be non-empty and contain no newline")
    return value


def outside_repo(path: Path, label: str) -> None:
    try:
        path.relative_to(REPO_ROOT)
    except ValueError:
        return
    raise BuildError(f"{label} must be outside the repository")


def input_file(raw: str, label: str) -> Path:
    candidate = Path(raw).expanduser()
    if candidate.is_symlink():
        raise BuildError(f"{label} must not be a symlink")
    path = candidate.resolve(strict=False)
    outside_repo(path, label)
    if path.is_symlink() or not path.is_file():
        raise BuildError(f"{label} must be a regular file")
    return path


def output_directory(raw: str) -> Path:
    candidate = Path(raw).expanduser()
    if candidate.is_symlink():
        raise BuildError("output-dir must not be a symlink")
    path = candidate.resolve(strict=False)
    outside_repo(path, "output-dir")
    if path.exists() and not path.is_dir():
        raise BuildError("output-dir must be a directory")
    return path


def read_key(path: Path) -> str:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        data = path.read_bytes()
        value = data.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise BuildError("key-file must be readable UTF-8 text") from exc
    if mode & 0o077:
        raise BuildError("key-file permissions must not be wider than 0600")
    return require_text("key-file contents", value)


def parse_gateway(raw: str) -> str:
    require_text("gateway-url", raw)
    if any(char.isspace() for char in raw):
        raise BuildError("gateway-url must be a pure HTTPS origin")
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise BuildError("gateway-url must be a pure HTTPS origin") from exc
    if port not in (None, 443):
        raise BuildError("gateway-url must be a pure HTTPS origin")
    if (
        parts.scheme.lower() != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or port is None and ":" in parts.netloc.rsplit("@", 1)[-1] and parts.netloc.endswith(":")
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        raise BuildError("gateway-url must be a pure HTTPS origin")
    hostname = parts.hostname.lower().rstrip(".")
    if not hostname:
        raise BuildError("gateway-url must be a pure HTTPS origin")
    if ":" in hostname:
        hostname = f"[{hostname}]"
    return f"https://{hostname}{':443' if port == 443 else ''}"


def validate_installer_url(raw: str, platform: str, installer_name: str) -> None:
    require_text("installer-url", raw)
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise BuildError("installer-url is not an allowed official URL") from exc
    if (
        parts.scheme.lower() != "https"
        or parts.hostname.lower() != "downloads.claude.ai"
        or port is not None
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise BuildError("installer-url is not an allowed official URL")
    path = unquote(parts.path)
    suffix = ".dmg" if platform == "macos" else ".msix"
    prefix_parts = ["releases", "darwin"] if platform == "macos" else ["releases", "win32", "x64"]
    path_parts = path.split("/")
    variable_parts = path_parts[1 : 1 + len(prefix_parts)]
    expected_tail_segments = 3 if platform == "macos" else 2
    if (
        len(path_parts) != 1 + len(prefix_parts) + expected_tail_segments
        or path_parts[0] != ""
        or variable_parts != prefix_parts
        or any(not segment or segment in {".", ".."} or "\\" in segment for segment in path_parts[1:])
        or not path_parts[-1].endswith(suffix)
        or path_parts[-1] == suffix
    ):
        raise BuildError("installer-url is not an allowed official URL")
    if not installer_name.lower().endswith(suffix):
        raise BuildError("installer extension does not match installer-url")


def parse_expiry(raw: str) -> str:
    require_text("expires-at", raw)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", raw):
        raise BuildError("expires-at must be a UTC timestamp ending in Z")
    try:
        value = datetime.fromisoformat(raw[:-1]).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise BuildError("expires-at must be a valid UTC timestamp") from exc
    if value <= datetime.now(timezone.utc):
        raise BuildError("expires-at must be in the future")
    return raw


def parse_uuid(raw: str) -> str:
    require_text("deployment-uuid", raw)
    if not re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", raw):
        raise BuildError("deployment-uuid must be a UUID")
    try:
        return str(uuid.UUID(raw))
    except ValueError as exc:
        raise BuildError("deployment-uuid must be a UUID") from exc


def json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def render(template: str, values: dict[str, str]) -> str:
    result = textwrap.dedent(template).lstrip()
    for key, value in values.items():
        result = result.replace(f"__{key}__", value)
    return result


MAC_INSTALL = r'''#!/bin/bash
set -euo pipefail
umask 077

OWNER='__OWNER__'
PROFILE_ID='__PROFILE_ID__'
PROFILE_SHA='__PROFILE_SHA__'
META_SHA='__META_SHA__'
MANIFEST_SHA='__MANIFEST_SHA__'
PROFILE_B64='__PROFILE_B64__'
META_B64='__META_B64__'
MANIFEST_B64='__MANIFEST_B64__'
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd -P)"
CONFIG_PARENT="$HOME/Library/Application Support/Claude-3p"
CONFIG="$CONFIG_PARENT/configLibrary"
PROFILE="$CONFIG/$PROFILE_ID.json"
META="$CONFIG/_meta.json"
MANIFEST_DIR="$HOME/Library/Application Support/Friend Agent Launcher"
MANIFEST="$MANIFEST_DIR/private-claude-manifest-v1.json"
DMG="$SCRIPT_DIR/Claude.dmg"
PROFILE_TMP=''
META_TMP=''
MANIFEST_TMP=''
PROFILE_CREATED=0
META_CREATED=0
MANIFEST_CREATED=0
MOUNT_DIR=''
MOUNTED=0
INSTALL_SUCCEEDED=0

fail() { echo "Install failed: $1" >&2; exit 1; }
check_not_symlink() {
  [[ ! -L "$1" ]] || fail "$2 must not be a symlink"
}
assert_absent() {
  check_not_symlink "$1" "$2"
  [[ ! -e "$1" ]] || fail "$2 already exists"
}
hash_file() { shasum -a 256 "$1" | awk '{print $1}'; }
remove_owned() {
  local path="$1" expected="$2" actual
  if [[ -f "$path" && ! -L "$path" ]]; then
    actual="$(hash_file "$path" 2>/dev/null || true)"
    [[ "$actual" == "$expected" ]] && rm -f -- "$path"
  fi
}
cleanup() {
  local status=$?
  set +e
  if [[ "$MOUNTED" -eq 1 ]]; then
    hdiutil detach "$MOUNT_DIR" -force >/dev/null 2>&1
  fi
  [[ -z "$PROFILE_TMP" ]] || rm -f -- "$PROFILE_TMP"
  [[ -z "$META_TMP" ]] || rm -f -- "$META_TMP"
  [[ -z "$MANIFEST_TMP" ]] || rm -f -- "$MANIFEST_TMP"
  if [[ "$status" -ne 0 && "$INSTALL_SUCCEEDED" -ne 1 ]]; then
    [[ "$PROFILE_CREATED" -eq 1 ]] && remove_owned "$PROFILE" "$PROFILE_SHA"
    [[ "$META_CREATED" -eq 1 ]] && remove_owned "$META" "$META_SHA"
    [[ "$MANIFEST_CREATED" -eq 1 ]] && remove_owned "$MANIFEST" "$MANIFEST_SHA"
  fi
  exit "$status"
}
trap cleanup EXIT

[[ "$(uname -s)" == "Darwin" ]] || fail "macOS is required"
if pgrep -x Claude >/dev/null 2>&1; then fail "Claude is running"; fi
[[ -f "$DMG" && ! -L "$DMG" ]] || fail "official installer is missing"
for policy in \
  "/Library/Managed Preferences/com.anthropic.Claude.plist" \
  "$HOME/Library/Managed Preferences/com.anthropic.Claude.plist"; do
  [[ ! -e "$policy" && ! -L "$policy" ]] || fail "managed Claude policy detected"
done
check_not_symlink "$CONFIG_PARENT" "configLibrary parent"
check_not_symlink "$CONFIG" "configLibrary"
check_not_symlink "$PROFILE" "profile"
check_not_symlink "$META" "metadata"
if [[ -e "$CONFIG" ]]; then
  [[ -d "$CONFIG" ]] || fail "configLibrary is not a directory"
  [[ -z "$(find "$CONFIG" -mindepth 1 -print -quit)" ]] || fail "configLibrary is not empty"
fi
assert_absent "$PROFILE" "profile"
assert_absent "$META" "metadata"
check_not_symlink "$MANIFEST_DIR" "manifest directory"
assert_absent "$MANIFEST" "manifest"

mkdir -p "$CONFIG"
check_not_symlink "$CONFIG_PARENT" "configLibrary parent"
[[ -d "$CONFIG" && ! -L "$CONFIG" ]] || fail "configLibrary could not be created"
PROFILE_TMP="$(mktemp "$CONFIG/.friend-private-profile.XXXXXX")"
printf '%s' "$PROFILE_B64" | base64 -D > "$PROFILE_TMP"
chmod 600 "$PROFILE_TMP"
META_TMP="$(mktemp "$CONFIG/.friend-private-meta.XXXXXX")"
printf '%s' "$META_B64" | base64 -D > "$META_TMP"
chmod 600 "$META_TMP"
sync
mv "$PROFILE_TMP" "$PROFILE"
PROFILE_TMP=''
PROFILE_CREATED=1
mv "$META_TMP" "$META"
META_TMP=''
META_CREATED=1
[[ "$(hash_file "$PROFILE")" == "$PROFILE_SHA" ]] || fail "profile verification failed"
[[ "$(hash_file "$META")" == "$META_SHA" ]] || fail "metadata verification failed"

mkdir -p "$MANIFEST_DIR"
check_not_symlink "$MANIFEST_DIR" "manifest directory"
[[ -d "$MANIFEST_DIR" ]] || fail "manifest directory could not be created"
MANIFEST_TMP="$(mktemp "$MANIFEST_DIR/.private-claude-manifest.XXXXXX")"
printf '%s' "$MANIFEST_B64" | base64 -D > "$MANIFEST_TMP"
chmod 600 "$MANIFEST_TMP"
sync
mv "$MANIFEST_TMP" "$MANIFEST"
MANIFEST_TMP=''
MANIFEST_CREATED=1
[[ "$(hash_file "$MANIFEST")" == "$MANIFEST_SHA" ]] || fail "manifest verification failed"

APP=''
for app_path in \
  "/Applications/Claude.app" \
  "$HOME/Applications/Claude.app"; do
  [[ ! -L "$app_path" ]] || fail "Claude.app must not be a symlink"
  [[ ! -e "$app_path" || -d "$app_path" ]] || fail "Claude.app is not a directory"
done
if [[ -d "/Applications/Claude.app" ]]; then
  APP="/Applications/Claude.app"
elif [[ -d "$HOME/Applications/Claude.app" ]]; then
  APP="$HOME/Applications/Claude.app"
else
  APP_DIR="$HOME/Applications"
  check_not_symlink "$APP_DIR" "Applications directory"
  if [[ -e "$APP_DIR" ]]; then
    [[ -d "$APP_DIR" ]] || fail "Applications path is not a directory"
  else
    mkdir -p "$APP_DIR"
  fi
  check_not_symlink "$APP_DIR" "Applications directory"
  assert_absent "$HOME/Applications/Claude.app" "Claude.app"
  MOUNT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/friend-claude-mount.XXXXXX")"
  hdiutil attach "$DMG" -nobrowse -readonly -mountpoint "$MOUNT_DIR" >/dev/null
  MOUNTED=1
  APP_SOURCE="$(find "$MOUNT_DIR" -type d -name 'Claude.app' -print -quit)"
  [[ -n "$APP_SOURCE" && -d "$APP_SOURCE" ]] || fail "Claude.app was not found in the installer"
  ditto "$APP_SOURCE" "$HOME/Applications/Claude.app"
  hdiutil detach "$MOUNT_DIR" >/dev/null
  MOUNTED=0
  rmdir "$MOUNT_DIR" 2>/dev/null || true
  APP="$HOME/Applications/Claude.app"
fi
[[ -d "$APP" && ! -L "$APP" ]] || fail "Claude.app is not a real directory"
open "$APP"
INSTALL_SUCCEEDED=1
echo "Friend Claude installed."
'''


MAC_RESTORE = r'''#!/bin/bash
set -euo pipefail
umask 077

OWNER='__OWNER__'
PROFILE_ID='__PROFILE_ID__'
PROFILE_SHA='__PROFILE_SHA__'
META_SHA='__META_SHA__'
MANIFEST_SHA='__MANIFEST_SHA__'
CONFIG_PARENT="$HOME/Library/Application Support/Claude-3p"
CONFIG="$CONFIG_PARENT/configLibrary"
PROFILE="$CONFIG/$PROFILE_ID.json"
META="$CONFIG/_meta.json"
MANIFEST_PARENT="$HOME/Library/Application Support/Friend Agent Launcher"
MANIFEST="$MANIFEST_PARENT/private-claude-manifest-v1.json"

fail() { echo "Restore failed: $1" >&2; exit 1; }
check_not_symlink() { [[ ! -L "$1" ]] || fail "$2 must not be a symlink"; }
hash_file() { shasum -a 256 "$1" | awk '{print $1}'; }

[[ "$(uname -s)" == "Darwin" ]] || fail "macOS is required"
if pgrep -x Claude >/dev/null 2>&1; then fail "Claude is running"; fi
check_not_symlink "$CONFIG_PARENT" "configLibrary parent"
check_not_symlink "$CONFIG" "configLibrary"
check_not_symlink "$PROFILE" "profile"
check_not_symlink "$META" "metadata"
check_not_symlink "$MANIFEST_PARENT" "manifest parent"
check_not_symlink "$MANIFEST" "manifest"
[[ -f "$MANIFEST" ]] || fail "owned manifest is missing"
[[ "$(hash_file "$MANIFEST")" == "$MANIFEST_SHA" ]] || fail "manifest ownership hash mismatch"
manifest_owner="$(sed -n 's/.*"owner":"\([^"]*\)".*/\1/p' "$MANIFEST")"
manifest_profile_hash="$(sed -n 's/.*"profile_sha256":"\([0-9a-f]\{64\}\)".*/\1/p' "$MANIFEST")"
manifest_meta_hash="$(sed -n 's/.*"meta_sha256":"\([0-9a-f]\{64\}\)".*/\1/p' "$MANIFEST")"
[[ "$manifest_owner" == "$OWNER" ]] || fail "manifest owner mismatch"
[[ "$manifest_profile_hash" == "$PROFILE_SHA" ]] || fail "profile ownership hash mismatch"
[[ "$manifest_meta_hash" == "$META_SHA" ]] || fail "metadata ownership hash mismatch"
[[ -f "$PROFILE" && ! -L "$PROFILE" ]] || fail "owned profile is missing"
[[ -f "$META" && ! -L "$META" ]] || fail "owned metadata is missing"
[[ "$(hash_file "$PROFILE")" == "$manifest_profile_hash" ]] || fail "profile was modified"
[[ "$(hash_file "$META")" == "$manifest_meta_hash" ]] || fail "metadata was modified"
rm -f -- "$PROFILE" "$META" "$MANIFEST"
if [[ -d "$CONFIG" && ! -L "$CONFIG" ]]; then
  [[ -z "$(find "$CONFIG" -mindepth 1 -print -quit)" ]] && rmdir "$CONFIG"
fi
echo "Friend package configuration removed; all three target files matched their package hashes. Claude.app was not uninstalled and test sessions were not removed."
'''


WINDOWS_INSTALL = r'''$ErrorActionPreference = 'Stop'

$OWNER = '__OWNER__'
$PROFILE_ID = '__PROFILE_ID__'
$PROFILE_SHA = '__PROFILE_SHA__'
$META_SHA = '__META_SHA__'
$MANIFEST_SHA = '__MANIFEST_SHA__'
$PROFILE_B64 = '__PROFILE_B64__'
$META_B64 = '__META_B64__'
$MANIFEST_B64 = '__MANIFEST_B64__'
$ProfileCreated = $false
$MetaCreated = $false
$ManifestCreated = $false
$ProfileTmp = $null
$MetaTmp = $null
$ManifestTmp = $null

function Get-Existing([string]$Path) {
    return Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
}
function Assert-NotReparse([string]$Path, [string]$Label) {
    $item = Get-Existing $Path
    if ($null -ne $item -and (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)) {
        throw "$Label must not be a reparse point."
    }
}
function Assert-Absent([string]$Path, [string]$Label) {
    Assert-NotReparse $Path $Label
    if ($null -ne (Get-Existing $Path)) { throw "$Label already exists." }
}
function Ensure-Directory([string]$Path, [string]$Label) {
    Assert-NotReparse $Path $Label
    $item = Get-Existing $Path
    if ($null -eq $item) {
        New-Item -ItemType Directory -LiteralPath $Path -Force | Out-Null
    } elseif (-not $item.PSIsContainer) {
        throw "$Label is not a directory."
    }
    Assert-NotReparse $Path $Label
}
function Get-Sha256([string]$Path) {
    return ((Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash).ToLowerInvariant()
}
function New-RandomSibling([string]$Target) {
    $parent = Split-Path -Parent $Target
    do { $candidate = Join-Path $parent ('.friend-private-' + [IO.Path]::GetRandomFileName()) }
    while ($null -ne (Get-Existing $candidate))
    return $candidate
}
function Write-Utf8NoBomFlush([string]$Path, [string]$Text) {
    $encoding = New-Object System.Text.UTF8Encoding -ArgumentList $false
    [IO.File]::WriteAllText($Path, $Text, $encoding)
    $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    try { $stream.Flush($true) } finally { $stream.Dispose() }
}
function Remove-IfOwned([string]$Path, [string]$Expected) {
    $item = Get-Existing $Path
    if ($null -ne $item -and $item.PSIsContainer) { return }
    if ($null -ne $item -and (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -eq 0)) {
        if ((Get-Sha256 $Path) -eq $Expected) { Remove-Item -LiteralPath $Path -Force }
    }
}
function Remove-Temp([string]$Path) {
    if ($null -ne $Path) { Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue }
}

try {
    if ($null -ne (Get-Process -Name 'Claude' -ErrorAction SilentlyContinue)) { throw 'Claude is running.' }
    foreach ($policyPath in @(
        'Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Policies\Claude',
        'Registry::HKEY_CURRENT_USER\SOFTWARE\Policies\Claude'
    )) {
        if ($null -ne (Get-Item -LiteralPath $policyPath -ErrorAction SilentlyContinue)) {
            throw 'Managed Claude policy detected.'
        }
    }
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) { throw 'LOCALAPPDATA is required.' }
    $LocalAppData = $env:LOCALAPPDATA
    Assert-NotReparse $LocalAppData 'LOCALAPPDATA'
    if (-not [IO.Directory]::Exists($LocalAppData)) { throw 'LOCALAPPDATA is missing.' }
    $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
    $PackageRoot = Split-Path -Parent $ScriptDir
    $Installer = Join-Path $PackageRoot 'Claude.msix'
    $ConfigParent = Join-Path $LocalAppData 'Claude-3p'
    $ConfigLibrary = Join-Path $ConfigParent 'configLibrary'
    $Profile = Join-Path $ConfigLibrary ($PROFILE_ID + '.json')
    $Meta = Join-Path $ConfigLibrary '_meta.json'
    $ManifestParent = Join-Path $LocalAppData 'Friend Agent Launcher'
    $Manifest = Join-Path $ManifestParent 'private-claude-manifest-v1.json'
    Assert-NotReparse $Installer 'official installer'
    $installerItem = Get-Existing $Installer
    if ($null -eq $installerItem -or $installerItem.PSIsContainer) { throw 'Official installer is missing.' }
    Assert-NotReparse $ConfigParent 'configLibrary parent'
    Assert-NotReparse $ConfigLibrary 'configLibrary'
    Assert-NotReparse $Profile 'profile'
    Assert-NotReparse $Meta 'metadata'
    Assert-NotReparse $ManifestParent 'manifest directory'
    Assert-Absent $Manifest 'manifest'
    $configItem = Get-Existing $ConfigLibrary
    if ($null -ne $configItem) {
        if (-not $configItem.PSIsContainer) { throw 'configLibrary is not a directory.' }
        $child = Get-ChildItem -LiteralPath $ConfigLibrary -Force | Select-Object -First 1
        if ($null -ne $child) { throw 'configLibrary is not empty.' }
    }
    Assert-Absent $Profile 'profile'
    Assert-Absent $Meta 'metadata'

    Ensure-Directory $ConfigParent 'configLibrary parent'
    Ensure-Directory $ConfigLibrary 'configLibrary'
    $ProfileText = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($PROFILE_B64))
    $MetaText = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($META_B64))
    $ProfileTmp = New-RandomSibling $Profile
    Write-Utf8NoBomFlush $ProfileTmp $ProfileText
    Move-Item -LiteralPath $ProfileTmp -Destination $Profile -ErrorAction Stop
    $ProfileTmp = $null
    $ProfileCreated = $true
    $MetaTmp = New-RandomSibling $Meta
    Write-Utf8NoBomFlush $MetaTmp $MetaText
    Move-Item -LiteralPath $MetaTmp -Destination $Meta -ErrorAction Stop
    $MetaTmp = $null
    $MetaCreated = $true
    if ((Get-Sha256 $Profile) -ne $PROFILE_SHA) { throw 'Profile verification failed.' }
    if ((Get-Sha256 $Meta) -ne $META_SHA) { throw 'Metadata verification failed.' }

    Ensure-Directory $ManifestParent 'manifest directory'
    Assert-Absent $Manifest 'manifest'
    $ManifestText = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($MANIFEST_B64))
    $ManifestTmp = New-RandomSibling $Manifest
    Write-Utf8NoBomFlush $ManifestTmp $ManifestText
    Move-Item -LiteralPath $ManifestTmp -Destination $Manifest -ErrorAction Stop
    $ManifestTmp = $null
    $ManifestCreated = $true
    if ((Get-Sha256 $Manifest) -ne $MANIFEST_SHA) { throw 'Manifest verification failed.' }
    Add-AppxPackage -Path $Installer -ErrorAction Stop
    Write-Host 'Friend Claude installed. Open Claude from the Start menu.'
} catch {
    try {
        Remove-Temp $ProfileTmp
        Remove-Temp $MetaTmp
        Remove-Temp $ManifestTmp
        if ($ProfileCreated) { Remove-IfOwned $Profile $PROFILE_SHA }
        if ($MetaCreated) { Remove-IfOwned $Meta $META_SHA }
        if ($ManifestCreated) { Remove-IfOwned $Manifest $MANIFEST_SHA }
    } catch { }
    Write-Error 'Install failed; only matching owned files were rolled back.'
    exit 1
}
'''


WINDOWS_RESTORE = r'''$ErrorActionPreference = 'Stop'

$OWNER = '__OWNER__'
$PROFILE_ID = '__PROFILE_ID__'
$PROFILE_SHA = '__PROFILE_SHA__'
$META_SHA = '__META_SHA__'
$MANIFEST_SHA = '__MANIFEST_SHA__'

function Get-Existing([string]$Path) {
    return Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
}
function Assert-NotReparse([string]$Path, [string]$Label) {
    $item = Get-Existing $Path
    if ($null -ne $item -and (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)) {
        throw "$Label must not be a reparse point."
    }
}
function Get-Sha256([string]$Path) {
    return ((Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash).ToLowerInvariant()
}

try {
    if ($null -ne (Get-Process -Name 'Claude' -ErrorAction SilentlyContinue)) { throw 'Claude is running.' }
    if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) { throw 'LOCALAPPDATA is required.' }
    $LocalAppData = $env:LOCALAPPDATA
    Assert-NotReparse $LocalAppData 'LOCALAPPDATA'
    if (-not [IO.Directory]::Exists($LocalAppData)) { throw 'LOCALAPPDATA is missing.' }
    $ConfigParent = Join-Path $LocalAppData 'Claude-3p'
    $ConfigLibrary = Join-Path $ConfigParent 'configLibrary'
    $Profile = Join-Path $ConfigLibrary ($PROFILE_ID + '.json')
    $Meta = Join-Path $ConfigLibrary '_meta.json'
    $ManifestParent = Join-Path $LocalAppData 'Friend Agent Launcher'
    $Manifest = Join-Path $ManifestParent 'private-claude-manifest-v1.json'
    Assert-NotReparse $ConfigParent 'configLibrary parent'
    Assert-NotReparse $ConfigLibrary 'configLibrary'
    Assert-NotReparse $Profile 'profile'
    Assert-NotReparse $Meta 'metadata'
    Assert-NotReparse $ManifestParent 'manifest parent'
    Assert-NotReparse $Manifest 'manifest'
    $manifestItem = Get-Existing $Manifest
    if ($null -eq $manifestItem -or $manifestItem.PSIsContainer) { throw 'Owned manifest is missing.' }
    if ((Get-Sha256 $Manifest) -ne $MANIFEST_SHA) { throw 'Manifest ownership hash mismatch.' }
    $manifestObject = [IO.File]::ReadAllText($Manifest) | ConvertFrom-Json
    if ($null -eq $manifestObject -or $manifestObject.owner -ne $OWNER) { throw 'Manifest owner mismatch.' }
    if ($manifestObject.profile_sha256 -ne $PROFILE_SHA -or $manifestObject.meta_sha256 -ne $META_SHA) {
        throw 'Manifest ownership hashes mismatch.'
    }
    $profileItem = Get-Existing $Profile
    $metaItem = Get-Existing $Meta
    if ($null -eq $profileItem -or $profileItem.PSIsContainer -or $null -eq $metaItem -or $metaItem.PSIsContainer) {
        throw 'Owned profile or metadata is missing.'
    }
    if ((Get-Sha256 $Profile) -ne $PROFILE_SHA -or (Get-Sha256 $Meta) -ne $META_SHA) {
        throw 'Owned profile or metadata was modified.'
    }
    Remove-Item -LiteralPath $Profile, $Meta, $Manifest -Force
    $configItem = Get-Existing $ConfigLibrary
    if ($null -ne $configItem) {
        if (-not $configItem.PSIsContainer) { throw 'configLibrary is not a directory.' }
        $child = Get-ChildItem -LiteralPath $ConfigLibrary -Force | Select-Object -First 1
        if ($null -eq $child) { Remove-Item -LiteralPath $ConfigLibrary -Force }
    }
    Write-Host 'Friend package configuration removed; all three target files matched their package hashes. Claude AppX was not uninstalled and test sessions were not removed.'
} catch {
    Write-Error 'Restore failed; no files were removed unless all three target files matched their package hashes.'
    exit 1
}
'''


WINDOWS_INSTALL_CMD = r'''@echo off
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0support\Install.ps1"
set "EXIT_CODE=%ERRORLEVEL%"
pause
exit /b %EXIT_CODE%
'''


WINDOWS_RESTORE_CMD = r'''@echo off
echo Restore removes only package configuration when all three target files match their package hashes; modified files are rejected.
echo It does not uninstall Claude AppX or remove test sessions.
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0support\Restore.ps1"
set "EXIT_CODE=%ERRORLEVEL%"
pause
exit /b %EXIT_CODE%
'''


def build(args: argparse.Namespace) -> tuple[Path, str]:
    platform = args.platform
    installer = input_file(args.installer, "installer")
    key_file = input_file(args.key_file, "key-file")
    output_dir = output_directory(args.output_dir)
    key = read_key(key_file)
    gateway = parse_gateway(args.gateway_url)
    require_text("quota-label", args.quota_label)
    require_text("validation-status", args.validation_status)
    version = require_text("version", args.version)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+\-]*", version):
        raise BuildError("version must be a safe filename component")
    expiry = parse_expiry(args.expires_at)
    deployment_uuid = parse_uuid(args.deployment_uuid)
    models = []
    for model in args.models:
        require_text("model", model)
        if model not in models:
            models.append(model)
    if not models or models[0] != "claude-fable-5":
        raise BuildError("models must contain claude-fable-5 first")
    try:
        installer_bytes = installer.read_bytes()
    except OSError as exc:
        raise BuildError("installer must be readable") from exc
    if len(installer_bytes) < 1024 * 1024 and not args.allow_small_test_installer:
        raise BuildError("installer must be at least 1 MiB unless --allow-small-test-installer is set")
    installer_hash = sha256(installer_bytes)
    if installer_hash != args.installer_sha256.lower():
        raise BuildError("installer sha256 does not match")
    installer_url = args.installer_url
    validate_installer_url(installer_url, platform, installer.name)
    profile = {
        "friend": {
            "owner": OWNER,
            "product": PRODUCT,
            "generation_id": PROFILE_ID,
            "manifest_version": 1,
        },
        "inferenceProvider": "gateway",
        "inferenceCredentialKind": "static",
        "inferenceGatewayBaseUrl": gateway,
        "inferenceGatewayApiKey": key,
        "inferenceGatewayAuthScheme": "bearer",
        "inferenceModels": [{"name": model} for model in models],
        "disableDeploymentModeChooser": True,
        "deploymentOrganizationUuid": deployment_uuid,
    }
    metadata = {
        "appliedId": PROFILE_ID,
        "entries": [{
            "id": PROFILE_ID,
            "name": "Friend Gateway",
            "friend_owner": OWNER,
            "friend_generation_id": PROFILE_ID,
            "product": PRODUCT,
        }],
    }
    profile_data = json_bytes(profile)
    metadata_data = json_bytes(metadata)
    manifest = {
        "owner": OWNER,
        "profile_sha256": sha256(profile_data),
        "meta_sha256": sha256(metadata_data),
    }
    manifest_data = json_bytes(manifest)
    values = {
        "OWNER": OWNER,
        "PROFILE_ID": PROFILE_ID,
        "PROFILE_SHA": sha256(profile_data),
        "META_SHA": sha256(metadata_data),
        "MANIFEST_SHA": sha256(manifest_data),
        "PROFILE_B64": b64(profile_data),
        "META_B64": b64(metadata_data),
        "MANIFEST_B64": b64(manifest_data),
    }
    root = f"Friend-Claude-{platform}-{version}-candidate"
    if platform == "macos":
        install_data = render(MAC_INSTALL, values).encode("utf-8")
        restore_data = render(MAC_RESTORE, values).encode("utf-8")
        install_name, restore_name = "Install.command", "Restore.command"
        install_mode = restore_mode = 0o755
        support_entries = []
    else:
        install_data = WINDOWS_INSTALL_CMD.encode("ascii")
        restore_data = WINDOWS_RESTORE_CMD.encode("ascii")
        install_script_data = render(WINDOWS_INSTALL, values).encode("ascii")
        restore_script_data = render(WINDOWS_RESTORE, values).encode("ascii")
        install_name, restore_name = "Install.cmd", "Restore.cmd"
        install_mode = restore_mode = 0o644
        support_entries = [
            (f"{root}/support/Install.ps1", install_script_data, 0o644),
            (f"{root}/support/Restore.ps1", restore_script_data, 0o644),
        ]
    if platform == "windows":
        platform_readme = (
            "Windows：退出 Claude 后双击 Install.cmd 安装；测试结束并退出 Claude 后双击 Restore.cmd。\n"
            "        Windows：support 目录是内部文件，请勿直接操作。"
        )
    else:
        platform_readme = "macOS：退出 Claude 后运行 Install.command；测试结束并退出 Claude 后运行 Restore.command。"
    readme = textwrap.dedent(f"""\
        Friend Claude {platform} {version} candidate

        这是一个非官方配置包，不代表 Anthropic 或 Claude 官方发行物。它使用原版 Claude Desktop 界面，
        但请求会经过构建时指定的第三方 HTTPS 网关。配置中的 Key 可被本机用户提取，请勿把它当作不可导出的凭据。

        测试信息
        - 额度标签：{args.quota_label}
        - UTC 到期时间：{expiry}
        - validation status：{args.validation_status}
        - 部署 UUID：{deployment_uuid}
        - 模型：{", ".join(models)}

        仅适合没有既有 Claude-3p configLibrary 配置的测试机。安装程序发现既有配置、策略、符号链接或 Claude 正在运行时会拒绝执行，
        不会合并或备份现有配置，也不会碰现有对话。Restore 只移除本包配置；只在三个目标文件与本包预期哈希均匹配时删除，若任一文件被修改则拒绝。
        Restore 不卸载 Claude.app/AppX，也不删除测试会话。
        请勿把本包或其中的 Key 上传到公开平台。

        {platform_readme}
        {"Windows 真机状态由 validation-status 原样展示；安装成功后请从 Start menu 打开 Claude。" if platform == "windows" else "安装成功后会打开 Claude.app。"}
        """).encode("utf-8")
    zip_name = f"{root}.zip"
    final = output_dir / zip_name
    if final.exists() or final.is_symlink():
        raise BuildError("output ZIP already exists")
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BuildError("output-dir could not be created") from exc
    if output_dir.is_symlink() or not output_dir.is_dir():
        raise BuildError("output-dir must be a real directory")
    entries = [
        (f"{root}/{INSTALLER_NAMES[platform]}", installer_bytes, 0o644),
        (f"{root}/{install_name}", install_data, install_mode),
        (f"{root}/{restore_name}", restore_data, restore_mode),
        (f"{root}/README.txt", readme, 0o644),
    ] + support_entries
    zip_created = False
    zip_complete = False
    zip_fd: int | None = None
    try:
        zip_fd = os.open(final, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        zip_created = True
        os.chmod(final, 0o600)
        with os.fdopen(zip_fd, "w+b") as handle:
            zip_fd = None
            with zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, data, mode in entries:
                    info = zipfile.ZipInfo(name)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = (stat.S_IFREG | mode) << 16
                    archive.writestr(info, data)
        digest = sha256(final.read_bytes())
        zip_complete = True
    except FileExistsError as exc:
        raise BuildError("output ZIP already exists") from exc
    except (OSError, zipfile.BadZipFile) as exc:
        raise BuildError("output ZIP could not be written") from exc
    finally:
        if zip_fd is not None:
            try:
                os.close(zip_fd)
            except OSError:
                pass
        if zip_created and not zip_complete:
            try:
                if not final.is_symlink() and final.is_file():
                    final.unlink()
            except OSError:
                pass
    return final, digest


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Build a private Claude fresh-install candidate kit")
    result.add_argument("--platform", choices=("macos", "windows"), required=True)
    result.add_argument("--installer", required=True)
    result.add_argument("--installer-url", required=True)
    result.add_argument("--installer-sha256", required=True)
    result.add_argument("--key-file", required=True)
    result.add_argument("--gateway-url", required=True)
    result.add_argument("--output-dir", required=True)
    result.add_argument("--models", nargs="+", required=True)
    result.add_argument("--quota-label", required=True)
    result.add_argument("--expires-at", required=True)
    result.add_argument("--deployment-uuid", required=True)
    result.add_argument("--validation-status", required=True)
    result.add_argument("--version", required=True)
    result.add_argument("--allow-small-test-installer", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        output, digest = build(args)
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"path={output} zip_sha256={digest} platform={args.platform} version={args.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
