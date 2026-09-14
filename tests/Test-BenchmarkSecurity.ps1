[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$pluginRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$benchmark = Join-Path $pluginRoot "scripts\Invoke-WoaUiBenchmark.ps1"
$testRoot = Join-Path $env:TEMP ("woa-benchmark-security-" + [Guid]::NewGuid().ToString("N"))
$applicationRoot = Join-Path $testRoot "application"
$evidenceRoot = Join-Path $testRoot "evidence"
$automationRoot = Join-Path $testRoot "automation"
$template = Join-Path $evidenceRoot "template"

function New-TestConfiguration {
    param(
        [Parameter(Mandatory)][string]$ProfilePath,
        [Parameter(Mandatory)][string]$LogsRelativePath,
        [Parameter(Mandatory)]$Scenario
    )

    return [ordered]@{
        schemaVersion = 1
        application = [ordered]@{
            executablePath = Join-Path $applicationRoot "app.exe"
            workingDirectory = $applicationRoot
            activationExecutablePath = Join-Path $applicationRoot "app.exe"
            windowTitle = "Test"
            settingsWindowTitle = "Settings"
            queryAutomationId = "Query"
            resultListAutomationId = "Results"
            profilePath = $ProfilePath
            profileTemplatePath = $template
            logsRelativePath = $LogsRelativePath
            winAppPath = Join-Path $automationRoot "winapp.cmd"
        }
        timeouts = [ordered]@{
            launchMilliseconds = 100
            queryMilliseconds = 100
            readyDelayMilliseconds = 0
        }
        shortcuts = [ordered]@{
            hide = "{ESC}"
            settings = "^i"
        }
        scenarios = @($Scenario)
    }
}

function Assert-Rejected {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)]$Configuration,
        [Parameter(Mandatory)][string]$ExpectedMessage
    )

    $configPath = Join-Path $evidenceRoot "$Name.json"
    $outputPath = Join-Path $evidenceRoot "$Name-output"
    $Configuration | ConvertTo-Json -Depth 15 | Set-Content -LiteralPath $configPath -Encoding utf8
    try {
        & $benchmark `
            -ConfigurationPath $configPath `
            -OutputDirectory $outputPath `
            -ArchitectureLabel "security-test" `
            -ApplicationRoot $applicationRoot `
            -EvidenceRoot $evidenceRoot `
            -AutomationToolRoot $automationRoot `
            -Trials 1
        throw "Benchmark accepted unsafe configuration '$Name'."
    } catch {
        if ($_.Exception.Message -notlike "*$ExpectedMessage*") {
            throw "Unexpected rejection for '$Name': $($_.Exception.Message)"
        }
    }
}

try {
    [void][IO.Directory]::CreateDirectory($applicationRoot)
    [void][IO.Directory]::CreateDirectory($evidenceRoot)
    [void][IO.Directory]::CreateDirectory($automationRoot)
    [void][IO.Directory]::CreateDirectory($template)
    [IO.File]::WriteAllText((Join-Path $applicationRoot "app.exe"), "")
    [IO.File]::WriteAllText((Join-Path $automationRoot "winapp.cmd"), "")

    $basicScenario = [ordered]@{
        id = "basic"
        category = "Test"
        query = "test"
        matchMode = "contains"
        expected = "test"
    }
    Assert-Rejected `
        -Name "profile-escape" `
        -Configuration (New-TestConfiguration `
            -ProfilePath (Join-Path $testRoot "outside\UserData") `
            -LogsRelativePath "Logs" `
            -Scenario $basicScenario) `
        -ExpectedMessage "Profile destination is outside the trusted root"

    Assert-Rejected `
        -Name "log-traversal" `
        -Configuration (New-TestConfiguration `
            -ProfilePath (Join-Path $applicationRoot "UserData") `
            -LogsRelativePath "..\..\outside" `
            -Scenario $basicScenario) `
        -ExpectedMessage "Log path is outside the trusted root"

    $processScenario = [ordered]@{
        id = "process"
        category = "Test"
        query = "process"
        expectedMode = "process_name_pid"
        setupProcess = "cmd.exe"
        setupArguments = "/c exit 0"
    }
    Assert-Rejected `
        -Name "unapproved-command" `
        -Configuration (New-TestConfiguration `
            -ProfilePath (Join-Path $applicationRoot "UserData") `
            -LogsRelativePath "Logs" `
            -Scenario $processScenario) `
        -ExpectedMessage "setup command is not approved"

    $equalRootConfig = New-TestConfiguration `
        -ProfilePath (Join-Path $applicationRoot "UserData") `
        -LogsRelativePath "Logs" `
        -Scenario $basicScenario
    $equalRootPath = Join-Path $evidenceRoot "equal-root.json"
    $equalRootConfig | ConvertTo-Json -Depth 15 | Set-Content -LiteralPath $equalRootPath -Encoding utf8
    try {
        & $benchmark `
            -ConfigurationPath $equalRootPath `
            -OutputDirectory (Join-Path $evidenceRoot "equal-root-output") `
            -ArchitectureLabel "security-test" `
            -ApplicationRoot $evidenceRoot `
            -EvidenceRoot $evidenceRoot `
            -AutomationToolRoot $automationRoot `
            -Trials 1
        throw "Benchmark accepted equal application and evidence roots."
    } catch {
        if ($_.Exception.Message -notlike "*must be disjoint*") {
            throw "Unexpected equal-root rejection: $($_.Exception.Message)"
        }
    }

    $resumeConfig = New-TestConfiguration `
        -ProfilePath (Join-Path $applicationRoot "UserData") `
        -LogsRelativePath "Logs" `
        -Scenario $basicScenario
    $resumeConfigPath = Join-Path $evidenceRoot "resume.json"
    $resumeOutput = Join-Path $evidenceRoot "resume-output"
    [void][IO.Directory]::CreateDirectory($resumeOutput)
    $resumeConfig | ConvertTo-Json -Depth 15 | Set-Content -LiteralPath $resumeConfigPath -Encoding utf8
    [pscustomobject]@{
        SchemaVersion = 1
        Architecture = "security-test"
        RunIdentity = [pscustomobject]@{ Sha256 = "invalid" }
        TargetTrials = 2
        CompletedTrials = 0
        TrialResults = @()
        QueryResults = @()
    } | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath (Join-Path $resumeOutput "benchmark-checkpoint.json") -Encoding utf8
    try {
        & $benchmark `
            -ConfigurationPath $resumeConfigPath `
            -OutputDirectory $resumeOutput `
            -ArchitectureLabel "security-test" `
            -ApplicationRoot $applicationRoot `
            -EvidenceRoot $evidenceRoot `
            -AutomationToolRoot $automationRoot `
            -Trials 2 `
            -Resume
        throw "Benchmark accepted an incompatible resume checkpoint."
    } catch {
        if ($_.Exception.Message -notlike "*run identity does not match*") {
            throw "Unexpected resume rejection: $($_.Exception.Message)"
        }
    }

    [pscustomobject]@{
        ProfileEscape = "rejected"
        LogTraversal = "rejected"
        UnapprovedCommand = "rejected"
        EqualRoots = "rejected"
        IncompatibleResume = "rejected"
    } | ConvertTo-Json
} finally {
    if (Test-Path -LiteralPath $testRoot) {
        [IO.Directory]::Delete($testRoot, $true)
    }
}
