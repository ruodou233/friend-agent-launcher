$ErrorActionPreference = 'Stop'

$PackageDir = Split-Path -Parent $PSScriptRoot
$PackageParent = Split-Path -Parent $PackageDir
$Assets = Join-Path $PackageDir 'assets'
$ManifestPath = Join-Path $PackageDir 'package-manifest.json'

if (-not [Environment]::Is64BitOperatingSystem) { throw 'This package requires 64-bit Windows.' }
if (-not (Test-Path -LiteralPath $ManifestPath)) { throw 'Missing package-manifest.json' }
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
    Write-Warning "Unable to query the desktop user; using the current user profile."
  }
  if (-not (Test-Path -LiteralPath $profilePath)) { throw 'Cannot resolve the signed-in user profile.' }
  return $profilePath
}

$DesktopProfile = Get-DesktopUserProfile
$CurrentAccount = [Security.Principal.WindowsIdentity]::GetCurrent().Name
if ($CurrentAccount -ine $DesktopAccount) {
  throw "Installer is running as $CurrentAccount, but the desktop belongs to $DesktopAccount. Run this script inside the desktop user's session, not as SYSTEM or another administrator."
}
$LocalAppData = Join-Path $DesktopProfile 'AppData\Local'
$StateDir = Join-Path $LocalAppData 'FriendDesktopAgentKit'

function Assert-Hash([string]$Path, [string]$Expected) {
  if (-not (Test-Path -LiteralPath $Path)) { throw "Missing required offline asset: $Path" }
  $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
  if ($actual -ne $Expected.ToLowerInvariant()) { throw "SHA-256 mismatch: $Path" }
}

function Assert-Signature([string]$Path, [string]$ExpectedPublisher) {
  $signature = Get-AuthenticodeSignature -LiteralPath $Path
  if ($signature.Status -ne 'Valid') { throw "Invalid publisher signature ($($signature.Status)): $Path" }
  $subject = [string]$signature.SignerCertificate.Subject
  if ($subject -notmatch [regex]::Escape($ExpectedPublisher)) {
    throw "Unexpected publisher: $subject"
  }
}

function Install-AppxIfNeeded([string]$Path) {
  Add-Type -AssemblyName System.IO.Compression.FileSystem
  $archive = [IO.Compression.ZipFile]::OpenRead($Path)
  try {
    $entry = $archive.GetEntry('AppxManifest.xml')
    if (-not $entry) { throw "AppxManifest.xml is missing from $Path" }
    $reader = New-Object IO.StreamReader($entry.Open())
    try { [xml]$appxManifest = $reader.ReadToEnd() } finally { $reader.Dispose() }
  } finally {
    $archive.Dispose()
  }
  $identityName = [string]$appxManifest.Package.Identity.Name
  $bundledVersion = [version]$appxManifest.Package.Identity.Version
  $installed = Get-AppxPackage -Name $identityName | Sort-Object { [version]$_.Version } -Descending | Select-Object -First 1
  if ($installed -and ([version]$installed.Version -ge $bundledVersion)) {
    Write-Host "Codex Desktop $($installed.Version) is already installed; skipping the same or older bundled MSIX."
    return
  }
  Add-AppxPackage -Path $Path
}

$client = Join-Path $PackageDir $manifest.official_client.file
$ccZip = Join-Path $PackageDir $manifest.cc_switch.file
$webview = Join-Path $PackageDir $manifest.webview2_runtime.file
$runtimeArchive = Join-Path (Join-Path $PackageParent $manifest.codex_primary_runtime.sibling_package) 'codex-primary-runtime.tar.gz'

Assert-Hash $client $manifest.official_client.sha256
Assert-Hash $ccZip $manifest.cc_switch.sha256
Assert-Hash $webview $manifest.webview2_runtime.sha256
Assert-Hash $runtimeArchive $manifest.codex_primary_runtime.sha256
Assert-Signature $client $manifest.official_client.publisher_match
Assert-Signature $webview $manifest.webview2_runtime.publisher_match

Write-Host 'Installing Microsoft WebView2 Runtime from the bundled official installer...'
$webviewProcess = Start-Process -FilePath $webview -ArgumentList @('/silent', '/install') -PassThru -Wait
if ($webviewProcess.ExitCode -ne 0) { throw "WebView2 Runtime installer exited with code $($webviewProcess.ExitCode)" }

Write-Host 'Installing the bundled official Codex desktop client...'
Install-AppxIfNeeded -Path $client

$tar = Get-Command tar.exe -ErrorAction SilentlyContinue
if (-not $tar) { throw 'Windows tar.exe is required to install Codex Primary Runtime.' }
$runtimeParent = Join-Path $DesktopProfile '.cache\codex-runtimes'
New-Item -ItemType Directory -Force -Path $runtimeParent | Out-Null
& $tar.Source -xzf $runtimeArchive -C $runtimeParent
if ($LASTEXITCODE -ne 0) { throw "Codex Primary Runtime extraction failed: $LASTEXITCODE" }
$runtimeJson = Join-Path $runtimeParent 'codex-primary-runtime\runtime.json'
$runtimeInfo = Get-Content -LiteralPath $runtimeJson -Raw -Encoding UTF8 | ConvertFrom-Json
if ([string]$runtimeInfo.bundleVersion -ne [string]$manifest.codex_primary_runtime.version) {
  throw 'Codex Primary Runtime version mismatch.'
}

New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
$ccDir = Join-Path $StateDir 'CC-Switch'
New-Item -ItemType Directory -Force -Path $ccDir | Out-Null
$ccTemp = Join-Path $env:TEMP "FriendDesktopAgentKit-CCSwitch-$([Guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Force -Path $ccTemp | Out-Null
try {
  Expand-Archive -LiteralPath $ccZip -DestinationPath $ccTemp -Force
  $newExe = Get-ChildItem -LiteralPath $ccTemp -Recurse -Filter 'cc-switch.exe' | Select-Object -First 1
  if (-not $newExe) { throw 'CC Switch executable was not found in the bundled archive.' }
  Copy-Item -LiteralPath $newExe.FullName -Destination (Join-Path $ccDir 'cc-switch.exe') -Force
  $portable = Get-ChildItem -LiteralPath $ccTemp -Recurse -Filter 'portable.ini' | Select-Object -First 1
  if ($portable) { Copy-Item -LiteralPath $portable.FullName -Destination (Join-Path $ccDir 'portable.ini') -Force }
} finally {
  Remove-Item -LiteralPath $ccTemp -Recurse -Force -ErrorAction SilentlyContinue
}
$ccExe = Get-ChildItem -LiteralPath $ccDir -Recurse -Filter 'cc-switch.exe' | Select-Object -First 1
if (-not $ccExe) { throw 'CC Switch executable was not found.' }

Write-Host 'Offline installation completed. Opening CC Switch for provider configuration.'
Start-Process -FilePath $ccExe.FullName
Read-Host 'Press Enter to close'
