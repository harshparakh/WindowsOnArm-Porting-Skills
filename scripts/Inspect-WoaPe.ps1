[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$RootPath,

    [Parameter(Mandatory)]
    [ValidateSet("x64", "arm64")]
    [string]$ExpectedArchitecture,

    [Parameter(Mandatory)]
    [string]$ReportPath,

    [string[]]$AllowedMismatchPatterns = @(),

    [string[]]$RequiredRelativePath = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Reflection.Metadata

$root = [IO.Path]::GetFullPath($RootPath)
if (-not (Test-Path -LiteralPath $root)) {
    throw "Root path does not exist: $root"
}

$expectedMachines = switch ($ExpectedArchitecture) {
    "x64" { @("Amd64") }
    "arm64" { @("Arm64", "Arm64EC") }
}

$files = @(Get-ChildItem -LiteralPath $root -File -Force -Recurse |
    Where-Object { $_.Extension -in @(".exe", ".dll") })
$results = @()
$validationErrors = @()
if ($files.Count -eq 0) {
    $validationErrors += "No EXE or DLL files were found under the inspected root."
}
foreach ($requiredPath in $RequiredRelativePath) {
    if ([IO.Path]::IsPathRooted($requiredPath) -or $requiredPath -match "(^|[\\/])\.\.([\\/]|$)") {
        throw "RequiredRelativePath must remain relative: $requiredPath"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $root $requiredPath) -PathType Leaf)) {
        $validationErrors += "Required file is missing: $requiredPath"
    }
}

foreach ($file in $files) {
    $relativePath = [IO.Path]::GetRelativePath($root, $file.FullName)
    $stream = [IO.File]::Open($file.FullName, "Open", "Read", "ReadWrite")
    $reader = $null
    try {
        $reader = [System.Reflection.PortableExecutable.PEReader]::new($stream)
        $headers = $reader.PEHeaders
        if (-not $headers.PEHeader) {
            throw "Missing PE header"
        }

        $machine = $headers.CoffHeader.Machine.ToString()
        $corFlags = if ($headers.CorHeader) { $headers.CorHeader.Flags } else { $null }
        $isIlOnly = $corFlags -and $corFlags.HasFlag(
            [System.Reflection.PortableExecutable.CorFlags]::ILOnly)
        $requires32Bit = $corFlags -and $corFlags.HasFlag(
            [System.Reflection.PortableExecutable.CorFlags]::Requires32Bit)
        $isAnyCpu = $reader.HasMetadata -and $isIlOnly -and $machine -eq "I386" -and -not $requires32Bit
        $allowedByPattern = @($AllowedMismatchPatterns |
            Where-Object { $relativePath -like $_ }).Count -gt 0
        $valid = $isAnyCpu -or $expectedMachines -contains $machine -or $allowedByPattern
        $classification = if ($isAnyCpu) {
            "managed-anycpu"
        } elseif ($reader.HasMetadata) {
            "managed-$($machine.ToLowerInvariant())"
        } else {
            "native-$($machine.ToLowerInvariant())"
        }

        $results += [pscustomobject]@{
            Path = $relativePath
            Size = $file.Length
            Machine = $machine
            HasMetadata = $reader.HasMetadata
            CorFlags = if ($corFlags) { $corFlags.ToString() } else { $null }
            Classification = $classification
            AllowedByPattern = $allowedByPattern
            Valid = $valid
        }
    } catch {
        $results += [pscustomobject]@{
            Path = $relativePath
            Size = $file.Length
            Machine = $null
            HasMetadata = $null
            CorFlags = $null
            Classification = "invalid-pe"
            AllowedByPattern = $false
            Valid = $false
            Error = $_.Exception.Message
        }
    } finally {
        if ($reader) {
            $reader.Dispose()
        }
        $stream.Dispose()
    }
}

$invalid = @($results | Where-Object { -not $_.Valid })
$invalidCount = $invalid.Count + $validationErrors.Count
$report = [pscustomobject]@{
    SchemaVersion = 1
    GeneratedAt = [DateTimeOffset]::Now.ToString("o")
    RootPath = $root
    ExpectedArchitecture = $ExpectedArchitecture
    ExpectedMachines = $expectedMachines
    AllowedMismatchPatterns = $AllowedMismatchPatterns
    RequiredRelativePath = $RequiredRelativePath
    FileCount = $results.Count
    InvalidCount = $invalidCount
    ValidationErrors = $validationErrors
    Summary = @($results |
        Group-Object Classification |
        Sort-Object Name |
        ForEach-Object {
            [pscustomobject]@{
                Classification = $_.Name
                Count = $_.Count
            }
        })
    Files = $results
}

$reportDirectory = Split-Path -Parent $ReportPath
if ($reportDirectory) {
    [void][IO.Directory]::CreateDirectory($reportDirectory)
}
$report | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ReportPath -Encoding utf8
$results | Export-Csv -LiteralPath ([IO.Path]::ChangeExtension($ReportPath, ".csv")) -NoTypeInformation -Encoding utf8

$report | Select-Object ExpectedArchitecture, FileCount, InvalidCount, Summary | ConvertTo-Json -Depth 5

if ($invalidCount -gt 0) {
    $invalid | Select-Object Path, Machine, Classification, Error | Format-Table -AutoSize
    if ($validationErrors.Count -gt 0) {
        $validationErrors | ForEach-Object { Write-Warning $_ }
    }
    throw "$invalidCount architecture validation errors were found for $ExpectedArchitecture."
}
