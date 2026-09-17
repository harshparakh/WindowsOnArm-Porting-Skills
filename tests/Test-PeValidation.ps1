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

    $hidden = Join-Path $testRoot "hidden.dll"
    [IO.File]::WriteAllText($hidden, "invalid hidden PE fixture")
    [IO.File]::SetAttributes($hidden, [IO.FileAttributes]::Hidden)
    try {
        & $script -RootPath $testRoot -ExpectedArchitecture arm64 -ReportPath $report | Out-Null
        throw "PE validation ignored a hidden DLL."
    } catch {
        if ($_.Exception.Message -notlike "*architecture validation errors*") {
            throw "Unexpected hidden PE rejection: $($_.Exception.Message)"
        }
    }
    $hiddenData = Get-Content -LiteralPath $report -Raw | ConvertFrom-Json
    if ($hiddenData.FileCount -ne 1 -or $hiddenData.InvalidCount -ne 1 -or $hiddenData.Files[0].Path -ne "hidden.dll") {
        throw "Hidden PE files were not included in the complete inventory."
    }

    [pscustomobject]@{
        EmptyDirectory = "rejected"
        InvalidCount = $data.InvalidCount
        HiddenInvalidDll = "rejected"
    } | ConvertTo-Json
} finally {
    if (Test-Path -LiteralPath $testRoot) {
        [IO.Directory]::Delete($testRoot, $true)
    }
}
