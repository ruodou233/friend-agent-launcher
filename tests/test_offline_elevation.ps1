$ErrorActionPreference = 'Stop'
$templateDir = Join-Path (Split-Path -Parent $PSScriptRoot) 'offline-desktop-kit/templates'
foreach ($file in Get-ChildItem $templateDir -Recurse -Filter '*.ps1') {
  $tokens = $null
  $errors = $null
  $ast = [Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$tokens, [ref]$errors)
  if ($errors.Count) { throw ($errors | Out-String) }
  Write-Output "PARSE OK: $($file.FullName)"
}
$source = Join-Path $templateDir 'claude-windows/Windows/Install-And-Start.ps1'
$tokens = $null
$errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($source, [ref]$tokens, [ref]$errors)
$assignment = $ast.Find({
  param($node)
  $node -is [Management.Automation.Language.AssignmentStatementAst] -and $node.Left.Extent.Text -eq '$installCommand'
}, $true)
if (-not $assignment) { throw 'Missing elevation command assignment' }
$expectedSid = 'S-1-5-21-100-200-300-1001'
$samples = @(
  'C:\Users\朋友\Downloads\Claude 离线包\assets\official-client.msix',
  'C:\Users\O''Brien\下载\$literal`name\assets\official-client.msix'
)
foreach ($sample in $samples) {
  $escapedPath = $sample.Replace("'", "''")
  $command = & ([scriptblock]::Create($assignment.Extent.Text + "`n; `$installCommand"))
  $childTokens = $null
  $childErrors = $null
  $child = [Management.Automation.Language.Parser]::ParseInput($command, [ref]$childTokens, [ref]$childErrors)
  if ($childErrors.Count) { throw ($childErrors | Out-String) }
  $appx = $child.Find({
    param($node)
    $node -is [Management.Automation.Language.CommandAst] -and $node.GetCommandName() -eq 'Add-AppxPackage'
  }, $true)
  if ($appx.CommandElements[-1].Value -cne $sample) { throw 'Path changed in elevated command' }
  $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($command))
  if ([Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($encoded)) -cne $command) {
    throw 'EncodedCommand roundtrip failed'
  }
  Write-Output 'ELEVATION COMMAND PARSE AND LITERAL PATH OK'
}
