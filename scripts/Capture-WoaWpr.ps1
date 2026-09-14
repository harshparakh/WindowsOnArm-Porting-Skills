[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$ApplicationPath,

    [Parameter(Mandatory)]
    [string]$WorkingDirectory,

    [Parameter(Mandatory)]
    [string]$ApplicationRoot,

    [Parameter(Mandatory)]
    [string]$EvidenceRoot,

    [Parameter(Mandatory)]
    [string]$ProfileTemplatePath,

    [Parameter(Mandatory)]
    [string]$ProfilePath,

    [Parameter(Mandatory)]
    [string]$ScenarioPath,

    [Parameter(Mandatory)]
    [string]$TracePath,

    [string]$WindowTitle = "Flow.Launcher",

    [string]$QueryAutomationId = "QueryTextBox",

    [ValidateSet("GeneralProfile", "CPU", "FileIO", "DotNET", "XAMLActivity")]
    [string]$WprProfile = "GeneralProfile",

    [ValidateRange(0, 120000)]
    [int]$ReadyDelayMilliseconds = 12000
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

function Get-FullPath {
    param([Parameter(Mandatory)][string]$Path)

    return [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($Path))
}

function Assert-DescendantPath {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$Context,
        [switch]$AllowRoot
    )

    $fullPath = Get-FullPath $Path
    $fullRoot = (Get-FullPath $Root).TrimEnd('\')
    $isRoot = $fullPath.Equals($fullRoot, [StringComparison]::OrdinalIgnoreCase)
    $isDescendant = $fullPath.StartsWith("$fullRoot\", [StringComparison]::OrdinalIgnoreCase)
    if (($isRoot -and -not $AllowRoot) -or (-not $isRoot -and -not $isDescendant)) {
        throw "$Context is outside the trusted root '$fullRoot': $fullPath"
    }
    return $fullPath
}

function Wait-AutomationWindow {
    param(
        [Parameter(Mandatory)][int]$ProcessId,
        [Parameter(Mandatory)][string]$Title,
        [Parameter(Mandatory)][int]$TimeoutMilliseconds
    )

    $condition = [System.Windows.Automation.AndCondition]::new(
        [System.Windows.Automation.Condition[]]@(
            [System.Windows.Automation.PropertyCondition]::new(
                [System.Windows.Automation.AutomationElement]::ProcessIdProperty,
                $ProcessId),
            [System.Windows.Automation.PropertyCondition]::new(
                [System.Windows.Automation.AutomationElement]::NameProperty,
                $Title)
        ))
    $deadline = [DateTime]::UtcNow.AddMilliseconds($TimeoutMilliseconds)
    do {
        $window = [System.Windows.Automation.AutomationElement]::RootElement.FindFirst(
            [System.Windows.Automation.TreeScope]::Children,
            $condition)
        if ($window) {
            return $window
        }
        Start-Sleep -Milliseconds 20
    } while ([DateTime]::UtcNow -lt $deadline)
    return $null
}

function Invoke-Wpr {
    param(
        [Parameter(Mandatory)][string[]]$Arguments,
        [switch]$IgnoreFailure
    )

    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = @(& wpr.exe @Arguments 2>&1 | ForEach-Object { $_.ToString() })
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($exitCode -ne 0 -and -not $IgnoreFailure) {
        throw "wpr $($Arguments -join ' ') failed with exit code $exitCode`: $($output -join ' ')"
    }
    return $output
}

$principal = [Security.Principal.WindowsPrincipal]::new(
    [Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Windows Performance Recorder capture requires an elevated PowerShell process."
}

$trustedApplicationRoot = Get-FullPath $ApplicationRoot
$trustedEvidenceRoot = Get-FullPath $EvidenceRoot
if ($trustedApplicationRoot.Equals($trustedEvidenceRoot, [StringComparison]::OrdinalIgnoreCase) -or
    $trustedApplicationRoot.StartsWith("$($trustedEvidenceRoot.TrimEnd('\'))\", [StringComparison]::OrdinalIgnoreCase) -or
    $trustedEvidenceRoot.StartsWith("$($trustedApplicationRoot.TrimEnd('\'))\", [StringComparison]::OrdinalIgnoreCase)) {
    throw "ApplicationRoot and EvidenceRoot must be disjoint."
}

$application = Assert-DescendantPath -Path $ApplicationPath -Root $trustedApplicationRoot -Context "Application"
$workingDirectoryPath = Assert-DescendantPath -Path $WorkingDirectory -Root $trustedApplicationRoot -Context "Working directory" -AllowRoot
$profile = Assert-DescendantPath -Path $ProfilePath -Root $trustedApplicationRoot -Context "Profile destination"
$template = Assert-DescendantPath -Path $ProfileTemplatePath -Root $trustedEvidenceRoot -Context "Profile template"
$scenarioFile = Assert-DescendantPath -Path $ScenarioPath -Root $trustedEvidenceRoot -Context "Scenario file"
$trace = Assert-DescendantPath -Path $TracePath -Root $trustedEvidenceRoot -Context "Trace"
if (-not (Test-Path -LiteralPath $application -PathType Leaf) -or
    -not (Test-Path -LiteralPath $workingDirectoryPath -PathType Container) -or
    -not (Test-Path -LiteralPath $template -PathType Container) -or
    -not (Test-Path -LiteralPath $scenarioFile -PathType Leaf)) {
    throw "Application, working directory, profile template, or scenario file is missing."
}

$parsedQueries = Get-Content -LiteralPath $scenarioFile -Raw | ConvertFrom-Json
$queries = @([string[]]$parsedQueries)
if ($queries.Count -lt 1 -or $queries.Count -gt 20) {
    throw "Scenario file must contain 1 to 20 query strings."
}
foreach ($query in $queries) {
    if ($query -isnot [string] -or [string]::IsNullOrWhiteSpace($query) -or $query.Length -gt 4096) {
        throw "Scenario file contains an invalid query."
    }
}

$traceDirectory = Split-Path -Parent $trace
[void][IO.Directory]::CreateDirectory($traceDirectory)
if (Test-Path -LiteralPath $trace) {
    [IO.File]::Delete($trace)
}
if (Test-Path -LiteralPath $profile) {
    [IO.Directory]::Delete($profile, $true)
}
Copy-Item -LiteralPath $template -Destination $profile -Recurse

$process = $null
$recording = $false
[void](Invoke-Wpr -Arguments @("-cancel") -IgnoreFailure)
try {
    [void](Invoke-Wpr -Arguments @("-start", $WprProfile, "-filemode"))
    $recording = $true

    $process = Start-Process -FilePath $application -WorkingDirectory $workingDirectoryPath -PassThru
    $window = Wait-AutomationWindow -ProcessId $process.Id -Title $WindowTitle -TimeoutMilliseconds 45000
    if (-not $window) {
        throw "Application window '$WindowTitle' did not appear."
    }
    $queryElement = $window.FindFirst(
        [System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.PropertyCondition]::new(
            [System.Windows.Automation.AutomationElement]::AutomationIdProperty,
            $QueryAutomationId))
    if (-not $queryElement) {
        throw "Query element '$QueryAutomationId' did not appear."
    }
    $valuePattern = [System.Windows.Automation.ValuePattern]$queryElement.GetCurrentPattern(
        [System.Windows.Automation.ValuePattern]::Pattern)
    Start-Sleep -Milliseconds $ReadyDelayMilliseconds
    foreach ($queryText in $queries) {
        $valuePattern.SetValue("")
        Start-Sleep -Milliseconds 250
        $valuePattern.SetValue($queryText)
        Start-Sleep -Seconds 2
    }

    [void](Invoke-Wpr -Arguments @(
                "-stop",
                $trace,
                "Windows on Arm application startup and query trace"
            ))
    $recording = $false
} finally {
    if ($recording) {
        [void](Invoke-Wpr -Arguments @("-cancel") -IgnoreFailure)
    }
    if ($process -and -not $process.HasExited) {
        Stop-Process -Id $process.Id
        $process.WaitForExit(15000) | Out-Null
    }
}

$metadata = [pscustomobject]@{
    SchemaVersion = 1
    CapturedAtEt = [DateTimeOffset]::Now.ToString("o")
    WprProfile = $WprProfile
    Application = $application
    ApplicationSha256 = (Get-FileHash -LiteralPath $application -Algorithm SHA256).Hash.ToLowerInvariant()
    Queries = $queries
    Trace = $trace
    TraceSize = (Get-Item -LiteralPath $trace).Length
    TraceSha256 = (Get-FileHash -LiteralPath $trace -Algorithm SHA256).Hash.ToLowerInvariant()
}
$metadataPath = [IO.Path]::ChangeExtension($trace, ".json")
$metadata | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $metadataPath -Encoding utf8
$metadata | ConvertTo-Json -Depth 5
