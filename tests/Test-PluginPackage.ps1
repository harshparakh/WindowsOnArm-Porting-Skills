[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$manifestPath = Join-Path $root "plugin.json"
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json

if ($manifest.name -notmatch "^[a-z0-9]+(-[a-z0-9]+)*$") {
    throw "Plugin name is not kebab-case: $($manifest.name)"
}
if ($manifest.version -notmatch "^\d+\.\d+\.\d+([-.][0-9A-Za-z.-]+)?$") {
    throw "Plugin version is not SemVer-compatible: $($manifest.version)"
}

$skillDirectories = @(Get-ChildItem -LiteralPath (Join-Path $root "skills") -Directory)
$expectedSkills = @("woa-port", "woa-scout", "woa-verify")
$actualSkills = @($skillDirectories.Name | Sort-Object)
if (($actualSkills -join ",") -ne ($expectedSkills -join ",")) {
    throw "Expected skills: $($expectedSkills -join ', ')"
}
foreach ($skillDirectory in $skillDirectories) {
    $skillPath = Join-Path $skillDirectory.FullName "SKILL.md"
    if (-not (Test-Path -LiteralPath $skillPath)) {
        throw "Missing SKILL.md: $skillPath"
    }
    $content = Get-Content -LiteralPath $skillPath -Raw
    if ($content -notmatch "(?s)^---\s+name:\s+$([regex]::Escape($skillDirectory.Name))\s+description:\s+.+?---") {
        throw "Invalid skill frontmatter: $skillPath"
    }
}

$powerShellScripts = @(Get-ChildItem -LiteralPath (Join-Path $root "scripts") -Filter "*.ps1" -File)
foreach ($script in $powerShellScripts) {
    $tokens = $null
    $errors = $null
    [void][System.Management.Automation.Language.Parser]::ParseFile(
        $script.FullName,
        [ref]$tokens,
        [ref]$errors)
    if (@($errors).Count -gt 0) {
        throw "PowerShell parse failure in $($script.Name): $($errors.Message -join '; ')"
    }
}

$jsonFiles = @(
    Get-ChildItem -LiteralPath (Join-Path $root "schemas") -Filter "*.json" -File
    Get-ChildItem -LiteralPath (Join-Path $root "examples") -Filter "*.json" -File -Recurse
)
foreach ($jsonFile in $jsonFiles) {
    Get-Content -LiteralPath $jsonFile.FullName -Raw | ConvertFrom-Json | Out-Null
}

& (Join-Path $root "tests\Test-BenchmarkSecurity.ps1") | Out-Null
& (Join-Path $root "tests\Test-ComparisonValidation.ps1") | Out-Null
& (Join-Path $root "tests\Test-PeValidation.ps1") | Out-Null
& (Join-Path $root "tests\Test-ProcessValidation.ps1") | Out-Null

python -m unittest discover -s (Join-Path $root "tests") -p "test_*.py"
if ($LASTEXITCODE -ne 0) {
    throw "Python unit tests failed with exit code $LASTEXITCODE"
}

[pscustomobject]@{
    Plugin = $manifest.name
    Version = $manifest.version
    Skills = $skillDirectories.Count
    PowerShellScripts = $powerShellScripts.Count
    JsonFiles = $jsonFiles.Count
    PythonTests = "passed"
} | ConvertTo-Json
