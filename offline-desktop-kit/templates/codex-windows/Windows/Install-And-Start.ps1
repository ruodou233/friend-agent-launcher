$ErrorActionPreference = 'Stop'

$PackageDir = Split-Path -Parent $PSScriptRoot
$PackageParent = Split-Path -Parent $PackageDir
$Assets = Join-Path $PackageDir 'assets'
$ManifestPath = Join-Path $PackageDir 'package-manifest.json'

if (-not [Environment]::Is64BitOperatingSystem) { throw 'This package requires 64-bit Windows.' }
if (-not (Test-Path -LiteralPath $ManifestPath)) { throw 'Missing package-manifest.json' }
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
    return [pscustomobject]@{
      InstalledVersion = [version]$installed.Version
      BundledVersion = $bundledVersion
      NewerClientDetected = ([version]$installed.Version -gt $bundledVersion)
    }
  }
  Add-AppxPackage -Path $Path
  return [pscustomobject]@{
    InstalledVersion = $bundledVersion
    BundledVersion = $bundledVersion
    NewerClientDetected = $false
  }
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
$clientResult = Install-AppxIfNeeded -Path $client

$tar = Get-Command tar.exe -ErrorAction SilentlyContinue
if (-not $tar) { throw 'Windows tar.exe is required to install Codex Primary Runtime.' }
$runtimeParent = Join-Path $DesktopProfile '.cache\codex-runtimes'
New-Item -ItemType Directory -Force -Path $runtimeParent | Out-Null

function Install-PinnedRuntime(
  [string]$Archive,
  [string]$ExpectedVersion,
  [string]$RuntimeParent,
  [bool]$NewerClientDetected
) {
  $target = Join-Path $RuntimeParent 'codex-primary-runtime'
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
  $expected = [version]$ExpectedVersion
  if ($NewerClientDetected) {
    if ($existingVersion -and $existingVersion -gt $expected) {
      Write-Host "A newer Codex client and newer Runtime $existingVersion are already installed; keeping them."
      return
    }
    throw "A newer Codex client is installed, but its Runtime is missing or not newer than bundled Runtime $ExpectedVersion. Use an updated offline kit."
  }
  if ($existingVersion -and $existingVersion -ge $expected) {
    Write-Host "Codex Primary Runtime $existingVersion is already installed; keeping it."
    return
  }

  $staging = Join-Path $RuntimeParent ".codex-primary-runtime.new-$([Guid]::NewGuid().ToString('N'))"
  $backup = Join-Path $RuntimeParent ".codex-primary-runtime.backup-$([Guid]::NewGuid().ToString('N'))"
  New-Item -ItemType Directory -Path $staging | Out-Null
  try {
    & $tar.Source -xzf $Archive -C $staging
    if ($LASTEXITCODE -ne 0) { throw "Codex Primary Runtime extraction failed: $LASTEXITCODE" }
    $stagedTarget = Join-Path $staging 'codex-primary-runtime'
    $stagedJson = Join-Path $stagedTarget 'runtime.json'
    $runtimeInfo = Get-Content -LiteralPath $stagedJson -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([string]$runtimeInfo.bundleVersion -ne $ExpectedVersion) {
      throw 'Codex Primary Runtime version mismatch.'
    }
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
}

Install-PinnedRuntime -Archive $runtimeArchive `
  -ExpectedVersion ([string]$manifest.codex_primary_runtime.version) `
  -RuntimeParent $runtimeParent `
  -NewerClientDetected ([bool]$clientResult.NewerClientDetected)

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
  if ($portable -and -not (Test-Path -LiteralPath (Join-Path $ccDir 'portable.ini'))) {
    Copy-Item -LiteralPath $portable.FullName -Destination (Join-Path $ccDir 'portable.ini')
  }
} finally {
  Remove-Item -LiteralPath $ccTemp -Recurse -Force -ErrorAction SilentlyContinue
}
$ccExe = Get-ChildItem -LiteralPath $ccDir -Recurse -Filter 'cc-switch.exe' | Select-Object -First 1
if (-not $ccExe) { throw 'CC Switch executable was not found.' }

Write-Host 'Offline installation completed. Opening CC Switch for provider configuration.'
Start-Process -FilePath $ccExe.FullName
Read-Host 'Press Enter to close'
