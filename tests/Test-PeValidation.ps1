[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$script = Join-Path $root "scripts\Inspect-WoaPe.ps1"
$testRoot = Join-Path $env:TEMP ("woa-pe-empty-" + [Guid]::NewGuid().ToString("N"))
[void][IO.Directory]::CreateDirectory($testRoot)

try {
    $report = Join-Path $testRoot "architecture.json"
    try {
        & $script -RootPath $testRoot -ExpectedArchitecture arm64 -ReportPath $report
        throw "PE validation accepted an empty directory."
    } catch {
        if ($_.Exception.Message -notlike "*architecture validation errors*") {
            throw "Unexpected PE validation rejection: $($_.Exception.Message)"
        }
    }
    $data = Get-Content -LiteralPath $report -Raw | ConvertFrom-Json
    if ($data.FileCount -ne 0 -or $data.InvalidCount -lt 1) {
        throw "Empty PE report did not record the validation error."
    }

    [pscustomobject]@{
        EmptyDirectory = "rejected"
        InvalidCount = $data.InvalidCount
    } | ConvertTo-Json
} finally {
    if (Test-Path -LiteralPath $testRoot) {
        [IO.Directory]::Delete($testRoot, $true)
    }
}
