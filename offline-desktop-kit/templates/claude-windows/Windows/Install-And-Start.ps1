$ErrorActionPreference = 'Stop'

$PackageDir = Split-Path -Parent $PSScriptRoot
$Assets = Join-Path $PackageDir 'assets'
$ManifestPath = Join-Path $PackageDir 'package-manifest.json'

if (-not [Environment]::Is64BitOperatingSystem) {
  throw '此素材包仅支持 64 位 Windows。'
}
if (-not (Test-Path -LiteralPath $ManifestPath)) {
  throw '缺少 package-manifest.json。'
}

$manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json

function Get-DesktopUserProfile {
  $profilePath = $env:USERPROFILE
  $script:DesktopAccount = [Security.Principal.WindowsIdentity]::GetCurrent().Name
  try {
    $explorer = Get-CimInstance Win32_Process -Filter "Name='explorer.exe'" |
      Sort-Object CreationDate | Select-Object -First 1
    if ($explorer) {
      $owner = Invoke-CimMethod -InputObject $explorer -MethodName GetOwner
      if ($owner.ReturnValue -eq 0 -and $owner.User) {
        $script:DesktopAccount = "$($owner.Domain)\$($owner.User)"
        $account = New-Object Security.Principal.NTAccount($owner.Domain, $owner.User)
        $sid = $account.Translate([Security.Principal.SecurityIdentifier]).Value
        $desktopProfile = Get-CimInstance Win32_UserProfile -Filter "SID='$sid'"
        if ($desktopProfile.LocalPath) { $profilePath = $desktopProfile.LocalPath }
      }
    }
  } catch {
    Write-Warning "无法查询桌面用户，将使用当前用户目录：$($_.Exception.Message)"
  }
  if (-not (Test-Path -LiteralPath $profilePath)) { throw '无法确定已登录用户的主目录。' }
  return $profilePath
}

$DesktopProfile = Get-DesktopUserProfile
$CurrentAccount = [Security.Principal.WindowsIdentity]::GetCurrent().Name
if ($CurrentAccount -ine $DesktopAccount) {
  throw "安装进程当前运行为 $CurrentAccount，但桌面用户是 $DesktopAccount。请让 Agent 在桌面用户会话中运行本脚本，不要用 SYSTEM 或另一个管理员账户。"
}
$RoamingAppData = Join-Path $DesktopProfile 'AppData\Roaming'
$LocalAppData = Join-Path $DesktopProfile 'AppData\Local'
$StateDir = Join-Path $LocalAppData 'FriendDesktopAgentKit'
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null

function Assert-FileHash([string]$Path, [string]$Expected) {
  if (-not (Test-Path -LiteralPath $Path)) { throw "缺少离线素材：$Path" }
  $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($actual -ne $Expected.ToLowerInvariant()) { throw "SHA-256 校验失败：$Path" }
}

function Assert-Signed([string]$Path, [string]$ExpectedPublisher) {
  $signature = Get-AuthenticodeSignature -LiteralPath $Path
  if ($signature.Status -ne 'Valid') { throw "数字签名校验失败：$Path ($($signature.Status))" }
  if ($ExpectedPublisher) {
    $subject = [string]$signature.SignerCertificate.Subject
    if ($subject -notmatch [regex]::Escape($ExpectedPublisher)) {
      throw "数字签名发布者不匹配：$subject"
    }
  }
}

function Install-AppxIfNeeded([string]$Path) {
  Add-Type -AssemblyName System.IO.Compression.FileSystem
  $archive = [IO.Compression.ZipFile]::OpenRead($Path)
  try {
    $entry = $archive.GetEntry('AppxManifest.xml')
    if (-not $entry) { throw "MSIX 中缺少 AppxManifest.xml：$Path" }
    $reader = New-Object IO.StreamReader($entry.Open())
    try { [xml]$appxManifest = $reader.ReadToEnd() } finally { $reader.Dispose() }
  } finally {
    $archive.Dispose()
  }
  $identityName = [string]$appxManifest.Package.Identity.Name
  $bundledVersion = [version]$appxManifest.Package.Identity.Version
  $installed = Get-AppxPackage -Name $identityName | Sort-Object { [version]$_.Version } -Descending | Select-Object -First 1
  if ($installed -and ([version]$installed.Version -ge $bundledVersion)) {
    Write-Host "Claude Desktop $($installed.Version) 已安装，不重复安装较旧或同版本。"
    return
  }
  Add-AppxPackage -Path $Path
}

$client = Join-Path $Assets $manifest.official_client.file
$engine = Join-Path $Assets $manifest.claude_code_engine.file
$gitInstaller = Join-Path $Assets $manifest.git_for_windows.file
$webViewInstaller = Join-Path $Assets $manifest.webview2.file
$ccZip = Join-Path $Assets $manifest.cc_switch.file

Assert-FileHash $client $manifest.official_client.sha256
Assert-FileHash $engine $manifest.claude_code_engine.sha256
Assert-FileHash $gitInstaller $manifest.git_for_windows.sha256
Assert-FileHash $webViewInstaller $manifest.webview2.sha256
Assert-FileHash $ccZip $manifest.cc_switch.sha256

Assert-Signed $client $manifest.official_client.publisher_contains
Assert-Signed $engine ''
Assert-Signed $gitInstaller ''
Assert-Signed $webViewInstaller $manifest.webview2.publisher_contains

Write-Host '正在安装微软 WebView2 离线运行时…'
$webViewProcess = Start-Process -FilePath $webViewInstaller -ArgumentList @('/silent', '/install') -PassThru -Wait
if ($webViewProcess.ExitCode -ne 0) { throw "WebView2 安装失败：$($webViewProcess.ExitCode)" }

if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) {
  Write-Host '正在安装 Git for Windows…'
  $gitProcess = Start-Process -FilePath $gitInstaller -ArgumentList @('/VERYSILENT', '/NORESTART', '/CURRENTUSER', '/SP-') -PassThru -Wait
  if ($gitProcess.ExitCode -ne 0) { throw "Git for Windows 安装失败：$($gitProcess.ExitCode)" }
}

Write-Host '正在安装官方 Claude Desktop…'
Install-AppxIfNeeded -Path $client

$engineDirs = @(
  (Join-Path $RoamingAppData "Claude\claude-code\$($manifest.claude_code_engine.version)"),
  (Join-Path $LocalAppData "Claude-3p\claude-code\$($manifest.claude_code_engine.version)")
)
foreach ($engineDir in $engineDirs) {
  New-Item -ItemType Directory -Force -Path $engineDir | Out-Null
  Copy-Item -LiteralPath $engine -Destination (Join-Path $engineDir 'claude.exe') -Force
  [IO.File]::WriteAllText((Join-Path $engineDir '.verified'), [string]$manifest.claude_code_engine.marker, (New-Object Text.UTF8Encoding($false)))
}

$ccDir = Join-Path $StateDir 'CC-Switch'
New-Item -ItemType Directory -Force -Path $ccDir | Out-Null
$ccTemp = Join-Path $env:TEMP "FriendDesktopAgentKit-CCSwitch-$([Guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Force -Path $ccTemp | Out-Null
try {
  Expand-Archive -LiteralPath $ccZip -DestinationPath $ccTemp -Force
  $newExe = Get-ChildItem -LiteralPath $ccTemp -Recurse -Filter 'cc-switch.exe' | Select-Object -First 1
  if (-not $newExe) { throw '解压后未找到 cc-switch.exe。' }
  Copy-Item -LiteralPath $newExe.FullName -Destination (Join-Path $ccDir 'cc-switch.exe') -Force
  $portable = Get-ChildItem -LiteralPath $ccTemp -Recurse -Filter 'portable.ini' | Select-Object -First 1
  if ($portable -and -not (Test-Path -LiteralPath (Join-Path $ccDir 'portable.ini'))) {
    Copy-Item -LiteralPath $portable.FullName -Destination (Join-Path $ccDir 'portable.ini')
  }
} finally {
  Remove-Item -LiteralPath $ccTemp -Recurse -Force -ErrorAction SilentlyContinue
}
$ccExe = Get-Item -LiteralPath (Join-Path $ccDir 'cc-switch.exe')

Write-Host ''
Write-Host '安装完成。普通与第三方线路的 Claude Code 引擎均已预置。'
Write-Host '正在打开 CC Switch，请在其界面中配置可用的 Provider。'
Start-Process -FilePath $ccExe.FullName
Read-Host '按 Enter 关闭此窗口'
