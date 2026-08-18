$ErrorActionPreference = 'Stop'

$AddonDir = Split-Path -Parent $PSScriptRoot
$Archive = Join-Path $AddonDir 'codex-primary-runtime.tar.gz'
$ManifestPath = Join-Path $AddonDir 'runtime-manifest.json'
if (-not [Environment]::Is64BitOperatingSystem) { throw 'This package requires 64-bit Windows.' }
if (-not (Test-Path -LiteralPath $Archive)) { throw 'Missing codex-primary-runtime.tar.gz' }
if (-not (Test-Path -LiteralPath $ManifestPath)) { throw 'Missing runtime-manifest.json' }

$manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json

$DesktopProfile = $env:USERPROFILE
$DesktopAccount = [Security.Principal.WindowsIdentity]::GetCurrent().Name
try {
  $explorer = Get-CimInstance Win32_Process -Filter "Name='explorer.exe'" | Sort-Object CreationDate | Select-Object -First 1
  if ($explorer) {
    $owner = Invoke-CimMethod -InputObject $explorer -MethodName GetOwner
    if ($owner.ReturnValue -eq 0 -and $owner.User) {
      $DesktopAccount = "$($owner.Domain)\$($owner.User)"
      $account = New-Object Security.Principal.NTAccount($owner.Domain, $owner.User)
      $sid = $account.Translate([Security.Principal.SecurityIdentifier]).Value
      $desktopUserProfile = Get-CimInstance Win32_UserProfile -Filter "SID='$sid'"
      if ($desktopUserProfile.LocalPath) { $DesktopProfile = $desktopUserProfile.LocalPath }
    }
  }
} catch {
  Write-Warning 'Unable to query the desktop user; using the current user profile.'
}
$CurrentAccount = [Security.Principal.WindowsIdentity]::GetCurrent().Name
if ($CurrentAccount -ine $DesktopAccount) {
  throw "Installer is running as $CurrentAccount, but the desktop belongs to $DesktopAccount. Run it inside the desktop user's session, not as SYSTEM or another administrator."
}

$actualHash = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualHash -ne ([string]$manifest.sha256).ToLowerInvariant()) { throw 'Codex Primary Runtime SHA-256 mismatch.' }

$tar = Get-Command tar.exe -ErrorAction SilentlyContinue
if (-not $tar) { throw 'Windows tar.exe is required to install Codex Primary Runtime.' }
$runtimeParent = Join-Path $DesktopProfile '.cache\codex-runtimes'
New-Item -ItemType Directory -Force -Path $runtimeParent | Out-Null
& $tar.Source -xzf $Archive -C $runtimeParent
if ($LASTEXITCODE -ne 0) { throw "Codex Primary Runtime extraction failed: $LASTEXITCODE" }
$runtimeJson = Join-Path $runtimeParent 'codex-primary-runtime\runtime.json'
$runtimeInfo = Get-Content -LiteralPath $runtimeJson -Raw -Encoding UTF8 | ConvertFrom-Json
if ([string]$runtimeInfo.bundleVersion -ne [string]$manifest.runtime_version) { throw 'Codex Primary Runtime version mismatch.' }
Write-Host "Codex Primary Runtime $($manifest.runtime_version) installed."
Read-Host 'Press Enter to close'
