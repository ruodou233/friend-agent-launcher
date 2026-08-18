$ErrorActionPreference = 'Stop'

$AddonDir = Split-Path -Parent $PSScriptRoot
$Archive = Join-Path $AddonDir 'codex-primary-runtime.tar.gz'
$ManifestPath = Join-Path $AddonDir 'runtime-manifest.json'
if (-not [Environment]::Is64BitOperatingSystem) { throw 'This package requires 64-bit Windows.' }
if (-not (Test-Path -LiteralPath $Archive)) { throw 'Missing codex-primary-runtime.tar.gz' }
if (-not (Test-Path -LiteralPath $ManifestPath)) { throw 'Missing runtime-manifest.json' }

$manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json

function Get-InteractiveDesktopContext {
  $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
  $serviceSids = @('S-1-5-18', 'S-1-5-19', 'S-1-5-20')
  if ($serviceSids -contains $identity.User.Value) {
    throw "The installer cannot run as service account $($identity.Name). Run it from the signed-in desktop user's session."
  }
  $sessionId = (Get-Process -Id $PID -ErrorAction Stop).SessionId
  if ($sessionId -le 0) { throw 'No interactive Windows user session was detected.' }
  $explorer = Get-Process -Name explorer -ErrorAction SilentlyContinue |
    Where-Object { $_.SessionId -eq $sessionId } | Select-Object -First 1
  if (-not $explorer) { throw 'No Windows desktop (explorer.exe) exists in the current session.' }
  try {
    $desktopProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $($explorer.Id)" -ErrorAction Stop
    $owner = Invoke-CimMethod -InputObject $desktopProcess -MethodName GetOwner -ErrorAction Stop
    if ($owner.ReturnValue -ne 0 -or -not $owner.User) { throw 'Cannot read the explorer.exe owner.' }
    $desktopAccount = New-Object Security.Principal.NTAccount($owner.Domain, $owner.User)
    $desktopSid = $desktopAccount.Translate([Security.Principal.SecurityIdentifier])
    if ($desktopSid.Value -ne $identity.User.Value) {
      throw "The installer runs as $($identity.Name), but this desktop belongs to $($desktopAccount.Value)."
    }
    $desktopProfile = Get-CimInstance Win32_UserProfile -Filter "SID='$($desktopSid.Value)'" -ErrorAction Stop |
      Select-Object -First 1
    if (-not $desktopProfile.LocalPath -or -not (Test-Path -LiteralPath $desktopProfile.LocalPath)) {
      throw 'Cannot resolve the current desktop user profile.'
    }
  } catch {
    throw "Cannot prove the current desktop account; installation stopped: $($_.Exception.Message)"
  }
  return [pscustomobject]@{ Account = $desktopAccount.Value; Profile = $desktopProfile.LocalPath }
}
$DesktopContext = Get-InteractiveDesktopContext
$DesktopProfile = $DesktopContext.Profile

$actualHash = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actualHash -ne ([string]$manifest.sha256).ToLowerInvariant()) { throw 'Codex Primary Runtime SHA-256 mismatch.' }

$tar = Get-Command tar.exe -ErrorAction SilentlyContinue
if (-not $tar) { throw 'Windows tar.exe is required to install Codex Primary Runtime.' }
$runtimeParent = Join-Path $DesktopProfile '.cache\codex-runtimes'
New-Item -ItemType Directory -Force -Path $runtimeParent | Out-Null
$target = Join-Path $runtimeParent 'codex-primary-runtime'
$runtimeJson = Join-Path $target 'runtime.json'
$existingVersion = $null
if (Test-Path -LiteralPath $runtimeJson) {
  try {
    $existingInfo = Get-Content -LiteralPath $runtimeJson -Raw -Encoding UTF8 | ConvertFrom-Json
    $existingVersion = [version]([string]$existingInfo.bundleVersion)
  } catch {
    Write-Warning 'The existing Codex Runtime has no readable version and will be replaced after staged validation.'
  }
}
$expectedVersion = [version]([string]$manifest.runtime_version)
$installedClient = Get-AppxPackage -Name ([string]$manifest.compatible_client.identity_name) |
  Sort-Object { [version]$_.Version } -Descending | Select-Object -First 1
$newerClientDetected = $installedClient -and (
  [version]$installedClient.Version -gt [version]([string]$manifest.compatible_client.max_version)
)
if ($newerClientDetected) {
  if ($existingVersion -and $existingVersion -gt $expectedVersion) {
    Write-Host "A newer Codex client and newer Runtime $existingVersion are already installed; keeping them."
    Read-Host 'Press Enter to close'
    exit 0
  }
  throw "Codex Desktop $($installedClient.Version) is newer than this kit, but its Runtime is missing or not newer than $expectedVersion. Use a matching updated offline kit."
}
if ($existingVersion -and $existingVersion -ge $expectedVersion) {
  Write-Host "Codex Primary Runtime $existingVersion is already installed; keeping it."
  Read-Host 'Press Enter to close'
  exit 0
}

$staging = Join-Path $runtimeParent ".codex-primary-runtime.new-$([Guid]::NewGuid().ToString('N'))"
$backup = Join-Path $runtimeParent ".codex-primary-runtime.backup-$([Guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Path $staging | Out-Null
try {
  & $tar.Source -xzf $Archive -C $staging
  if ($LASTEXITCODE -ne 0) { throw "Codex Primary Runtime extraction failed: $LASTEXITCODE" }
  $stagedTarget = Join-Path $staging 'codex-primary-runtime'
  $stagedJson = Join-Path $stagedTarget 'runtime.json'
  $runtimeInfo = Get-Content -LiteralPath $stagedJson -Raw -Encoding UTF8 | ConvertFrom-Json
  if ([string]$runtimeInfo.bundleVersion -ne [string]$manifest.runtime_version) { throw 'Codex Primary Runtime version mismatch.' }
  if (Test-Path -LiteralPath $target) { Move-Item -LiteralPath $target -Destination $backup }
  try {
    Move-Item -LiteralPath $stagedTarget -Destination $target
  } catch {
    if ((Test-Path -LiteralPath $backup) -and -not (Test-Path -LiteralPath $target)) {
      Move-Item -LiteralPath $backup -Destination $target
    }
    throw
  }
  if (Test-Path -LiteralPath $backup) { Remove-Item -LiteralPath $backup -Recurse -Force }
} finally {
  if (Test-Path -LiteralPath $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
}
Write-Host "Codex Primary Runtime $($manifest.runtime_version) installed after staged validation."
Read-Host 'Press Enter to close'
