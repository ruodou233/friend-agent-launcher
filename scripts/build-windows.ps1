$ErrorActionPreference = "Stop"

$rootDir = Split-Path -Parent $PSScriptRoot
$releaseDir = Join-Path $rootDir "release\windows"
$bundleDir = Join-Path $rootDir "src-tauri\target\release\bundle"

New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null
Get-ChildItem -Path $releaseDir -File -ErrorAction SilentlyContinue | Remove-Item -Force

function Copy-Installers {
    param([string]$Product)

    Get-ChildItem -Path $bundleDir -Recurse -File |
        Where-Object { $_.Extension -in @(".msi", ".exe") } |
        ForEach-Object {
            $extension = $_.Extension.ToLowerInvariant()
            $destination = Join-Path $releaseDir "Friend-$Product-0.1.0-windows-x64$extension"
            Copy-Item -LiteralPath $_.FullName -Destination $destination -Force
        }
}

Set-Location $rootDir
if (Test-Path -LiteralPath $bundleDir) {
    Remove-Item -LiteralPath $bundleDir -Recurse -Force
}
npm run desktop:build:claude
Copy-Installers -Product "Claude"

if (Test-Path -LiteralPath $bundleDir) {
    Remove-Item -LiteralPath $bundleDir -Recurse -Force
}
npm run desktop:build:codex
Copy-Installers -Product "Codex"

$installers = Get-ChildItem -Path $releaseDir -File |
    Where-Object { $_.Extension -in @(".msi", ".exe") } |
    Sort-Object Name

if (-not $installers) {
    throw "No Windows installer was generated."
}

$checksums = $installers | ForEach-Object {
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash.ToLowerInvariant()
    "$hash *$($_.Name)"
}
$checksums | Set-Content -Path (Join-Path $releaseDir "SHA256SUMS.txt") -Encoding ascii
