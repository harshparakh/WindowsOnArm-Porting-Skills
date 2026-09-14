[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$ConfigurationPath,

    [Parameter(Mandatory)]
    [string]$OutputDirectory,

    [Parameter(Mandatory)]
    [string]$ArchitectureLabel,

    [Parameter(Mandatory)]
    [string]$ApplicationRoot,

    [Parameter(Mandatory)]
    [string]$EvidenceRoot,

    [Parameter(Mandatory)]
    [string]$AutomationToolRoot,

    [string[]]$ApprovedSetupCommand = @(),

    [ValidateRange(1, 1000)]
    [int]$Trials = 20,

    [switch]$Resume
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Windows.Forms

function Get-FullPath {
    param([Parameter(Mandatory)][string]$Path)

    return [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($Path))
}

function Get-StringSha256 {
    param(
        [Parameter(Mandatory)]
        [AllowEmptyString()]
        [string]$Value
    )

    $bytes = [Text.Encoding]::UTF8.GetBytes($Value)
    return [Convert]::ToHexString(
        [Security.Cryptography.SHA256]::HashData($bytes)).ToLowerInvariant()
}

function Get-DirectoryFingerprint {
    param([Parameter(Mandatory)][string]$Path)

    $root = Get-FullPath $Path
    $builder = [Text.StringBuilder]::new()
    foreach ($file in Get-ChildItem -LiteralPath $root -File -Recurse | Sort-Object FullName) {
        $relativePath = [IO.Path]::GetRelativePath($root, $file.FullName)
        [void]$builder.AppendLine($relativePath)
        [void]$builder.AppendLine($file.Length)
        [void]$builder.AppendLine(
            (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant())
    }
    return Get-StringSha256 $builder.ToString()
}

function Get-OptionalPropertyValue {
    param(
        [Parameter(Mandatory)]$InputObject,
        [Parameter(Mandatory)][string]$Name,
        $DefaultValue = $null
    )

    $property = $InputObject.PSObject.Properties[$Name]
    if ($property) {
        return $property.Value
    }

    return $DefaultValue
}

function Assert-ObjectProperties {
    param(
        [Parameter(Mandatory)]$InputObject,
        [Parameter(Mandatory)][string[]]$Allowed,
        [string[]]$Required = @(),
        [Parameter(Mandatory)][string]$Context
    )

    $names = @($InputObject.PSObject.Properties.Name)
    $unexpected = @($names | Where-Object { $Allowed -notcontains $_ })
    if ($unexpected.Count -gt 0) {
        throw "$Context contains unsupported properties: $($unexpected -join ', ')"
    }
    $missing = @($Required | Where-Object { $names -notcontains $_ })
    if ($missing.Count -gt 0) {
        throw "$Context is missing required properties: $($missing -join ', ')"
    }
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
    $rootPrefix = "$fullRoot\"
    $isRoot = $fullPath.Equals($fullRoot, [StringComparison]::OrdinalIgnoreCase)
    if (($isRoot -and -not $AllowRoot) -or
        (-not $isRoot -and -not $fullPath.StartsWith($rootPrefix, [StringComparison]::OrdinalIgnoreCase))) {
        throw "$Context is outside the trusted root '$fullRoot': $fullPath"
    }

    $relative = if ($isRoot) { "" } else { [IO.Path]::GetRelativePath($fullRoot, $fullPath) }
    $current = $fullRoot
    $segments = @($relative -split '[\\/]+' | Where-Object { $_ })
    foreach ($segment in $segments) {
        $current = Join-Path $current $segment
        if (-not (Test-Path -LiteralPath $current)) {
            break
        }
        $item = Get-Item -LiteralPath $current -Force
        if ($item.Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)) {
            throw "$Context traverses a reparse point: $current"
        }
    }

    return $fullPath
}

function Get-ApprovedSetupCommandMap {
    param([string[]]$Entries)

    $approved = @{}
    foreach ($entry in $Entries) {
        $parts = $entry -split '\|', 2
        if ($parts.Count -ne 2 -or -not [IO.Path]::IsPathRooted($parts[0])) {
            throw "Approved setup commands must use '<absolute executable>|<exact arguments>': $entry"
        }
        $path = Get-FullPath $parts[0]
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Approved setup executable does not exist: $path"
        }
        $approved["$path|$($parts[1])"] = $true
    }
    return $approved
}

function Resolve-ApprovedSetupCommand {
    param(
        [Parameter(Mandatory)][string]$Executable,
        [Parameter(Mandatory)][string]$Arguments,
        [Parameter(Mandatory)][hashtable]$ApprovedCommands,
        [Parameter(Mandatory)][string]$ScenarioId
    )

    $command = Get-Command $Executable -CommandType Application -ErrorAction Stop | Select-Object -First 1
    $path = Get-FullPath $command.Source
    $key = "$path|$Arguments"
    if (-not $ApprovedCommands.ContainsKey($key)) {
        throw "Scenario '$ScenarioId' setup command is not approved: $key"
    }
    return $path
}

function Wait-AutomationWindow {
    param(
        [Parameter(Mandatory)][int]$ProcessId,
        [Parameter(Mandatory)][string]$Title,
        [Parameter(Mandatory)][int]$TimeoutMilliseconds
    )

    $processCondition = [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::ProcessIdProperty,
        $ProcessId)
    $titleCondition = [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::NameProperty,
        $Title)
    $condition = [System.Windows.Automation.AndCondition]::new(
        [System.Windows.Automation.Condition[]]@($processCondition, $titleCondition))
    $deadline = [DateTime]::UtcNow.AddMilliseconds($TimeoutMilliseconds)

    do {
        $window = [System.Windows.Automation.AutomationElement]::RootElement.FindFirst(
            [System.Windows.Automation.TreeScope]::Children,
            $condition)
        if ($window) {
            return $window
        }

        Start-Sleep -Milliseconds 10
    } while ([DateTime]::UtcNow -lt $deadline)

    return $null
}

function Wait-AutomationElement {
    param(
        [Parameter(Mandatory)][System.Windows.Automation.AutomationElement]$Root,
        [Parameter(Mandatory)][string]$AutomationId,
        [Parameter(Mandatory)][int]$TimeoutMilliseconds
    )

    $condition = [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::AutomationIdProperty,
        $AutomationId)
    $deadline = [DateTime]::UtcNow.AddMilliseconds($TimeoutMilliseconds)

    do {
        $element = $Root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $condition)
        if ($element) {
            return $element
        }

        Start-Sleep -Milliseconds 5
    } while ([DateTime]::UtcNow -lt $deadline)

    return $null
}

function Wait-WindowHidden {
    param(
        [Parameter(Mandatory)][int]$ProcessId,
        [Parameter(Mandatory)][string]$Title,
        [Parameter(Mandatory)][int]$TimeoutMilliseconds
    )

    $deadline = [DateTime]::UtcNow.AddMilliseconds($TimeoutMilliseconds)
    do {
        $window = Wait-AutomationWindow -ProcessId $ProcessId -Title $Title -TimeoutMilliseconds 20
        if (-not $window) {
            return $true
        }

        try {
            if ($window.Current.IsOffscreen) {
                return $true
            }
        } catch {
            return $true
        }

        Start-Sleep -Milliseconds 10
    } while ([DateTime]::UtcNow -lt $deadline)

    return $false
}

function Stop-ApplicationProcesses {
    param(
        [Parameter(Mandatory)][string]$ProcessName,
        [Parameter(Mandatory)][string]$ExecutablePath
    )

    $targetPath = Get-FullPath $ExecutablePath
    $processes = @(Get-Process -Name $ProcessName -ErrorAction SilentlyContinue)
    foreach ($process in $processes) {
        try {
            if ((Get-FullPath $process.Path) -ne $targetPath) {
                continue
            }
        } catch {
            continue
        }

        Stop-Process -Id $process.Id
        $process.WaitForExit(15000) | Out-Null
    }
}

function Restore-Profile {
    param(
        [Parameter(Mandatory)][string]$TemplatePath,
        [Parameter(Mandatory)][string]$ProfilePath,
        [Parameter(Mandatory)][string]$TrustedTemplateRoot,
        [Parameter(Mandatory)][string]$TrustedApplicationRoot
    )

    $source = Assert-DescendantPath `
        -Path $TemplatePath `
        -Root $TrustedTemplateRoot `
        -Context "Profile template"
    $destination = Assert-DescendantPath `
        -Path $ProfilePath `
        -Root $TrustedApplicationRoot `
        -Context "Profile destination"
    if (-not (Test-Path -LiteralPath $source)) {
        throw "Profile template does not exist: $source"
    }

    if (Test-Path -LiteralPath $destination) {
        [IO.Directory]::Delete($destination, $true)
    }
    Copy-Item -LiteralPath $source -Destination $destination -Recurse
}

function Get-ResultNames {
    param(
        [Parameter(Mandatory)][System.Windows.Automation.AutomationElement]$Window,
        [Parameter(Mandatory)][string]$ResultListAutomationId
    )

    $resultList = $Window.FindFirst(
        [System.Windows.Automation.TreeScope]::Descendants,
        [System.Windows.Automation.PropertyCondition]::new(
            [System.Windows.Automation.AutomationElement]::AutomationIdProperty,
            $ResultListAutomationId))
    if (-not $resultList) {
        return @()
    }

    $listItemCondition = [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::ListItem)
    $items = $resultList.FindAll([System.Windows.Automation.TreeScope]::Descendants, $listItemCondition)
    $names = @()
    foreach ($item in $items) {
        try {
            if (-not [string]::IsNullOrWhiteSpace($item.Current.Name)) {
                $names += $item.Current.Name
            }
        } catch {
        }
    }

    return $names
}

function Find-MatchingResult {
    param(
        [Parameter(Mandatory)]
        [AllowEmptyCollection()]
        [string[]]$Names,
        [Parameter(Mandatory)]$Scenario,
        [System.Diagnostics.Process]$SetupProcess
    )

    $expectedMode = Get-OptionalPropertyValue -InputObject $Scenario -Name 'expectedMode'
    $mode = if ($expectedMode) {
        [string]$expectedMode
    } else {
        [string](Get-OptionalPropertyValue -InputObject $Scenario -Name 'matchMode' -DefaultValue 'contains')
    }
    switch ($mode) {
        'process_name_pid' {
            if (-not $SetupProcess) {
                throw "Scenario '$($Scenario.id)' requires a setup process."
            }
            $prefix = "$($SetupProcess.ProcessName) - $($SetupProcess.Id)"
            return $Names | Where-Object { $_.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) } | Select-Object -First 1
        }
        'regex' {
            return $Names | Where-Object { $_ -match [string]$Scenario.expectedPattern } | Select-Object -First 1
        }
        'startsWith' {
            return $Names | Where-Object {
                $_.StartsWith([string]$Scenario.expected, [StringComparison]::OrdinalIgnoreCase)
            } | Select-Object -First 1
        }
        'allContains' {
            return $Names | Where-Object {
                $candidate = $_
                foreach ($token in @($Scenario.expectedTokens)) {
                    if ($candidate.IndexOf([string]$token, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
                        return $false
                    }
                }
                return $true
            } | Select-Object -First 1
        }
        default {
            return $Names | Where-Object {
                $_.IndexOf([string]$Scenario.expected, [StringComparison]::OrdinalIgnoreCase) -ge 0
            } | Select-Object -First 1
        }
    }
}

function Invoke-QueryScenario {
    param(
        [Parameter(Mandatory)][System.Windows.Automation.AutomationElement]$Window,
        [Parameter(Mandatory)][System.Windows.Automation.ValuePattern]$ValuePattern,
        [Parameter(Mandatory)][string]$ResultListAutomationId,
        [Parameter(Mandatory)]$Scenario,
        [Parameter(Mandatory)][System.Diagnostics.Process]$ApplicationProcess,
        [Parameter(Mandatory)][int]$TimeoutMilliseconds,
        [Parameter(Mandatory)][hashtable]$ApprovedSetupCommands
    )

    $setupProcess = $null
    $setupProcessPath = Get-OptionalPropertyValue -InputObject $Scenario -Name 'setupProcess'
    if ($setupProcessPath) {
        $setupArguments = [string](Get-OptionalPropertyValue `
                -InputObject $Scenario `
                -Name 'setupArguments' `
                -DefaultValue '')
        $resolvedSetupPath = Resolve-ApprovedSetupCommand `
            -Executable ([string]$setupProcessPath) `
            -Arguments $setupArguments `
            -ApprovedCommands $ApprovedSetupCommands `
            -ScenarioId ([string]$Scenario.id)
        $setupProcess = Start-Process `
            -FilePath $resolvedSetupPath `
            -ArgumentList $setupArguments `
            -WindowStyle Hidden `
            -PassThru
        Start-Sleep -Milliseconds 500
    }

    try {
        $ValuePattern.SetValue('')
        Start-Sleep -Milliseconds 100
        $ApplicationProcess.Refresh()
        $cpuBefore = $ApplicationProcess.TotalProcessorTime.TotalMilliseconds
        $watch = [Diagnostics.Stopwatch]::StartNew()
        $ValuePattern.SetValue([string]$Scenario.query)
        $deadline = [DateTime]::UtcNow.AddMilliseconds($TimeoutMilliseconds)
        $matchedName = $null
        $observedNames = @()

        do {
            $observedNames = @(Get-ResultNames -Window $Window -ResultListAutomationId $ResultListAutomationId)
            $matchedName = Find-MatchingResult -Names $observedNames -Scenario $Scenario -SetupProcess $setupProcess
            if (-not $matchedName) {
                Start-Sleep -Milliseconds 5
            }
        } while (-not $matchedName -and [DateTime]::UtcNow -lt $deadline)

        $watch.Stop()
        $ApplicationProcess.Refresh()
        return [pscustomobject]@{
            Id = [string]$Scenario.id
            Category = [string]$Scenario.category
            Query = [string]$Scenario.query
            Passed = [bool]$matchedName
            ElapsedMilliseconds = [math]::Round($watch.Elapsed.TotalMilliseconds, 3)
            CpuMilliseconds = [math]::Round(
                $ApplicationProcess.TotalProcessorTime.TotalMilliseconds - $cpuBefore,
                3)
            MatchedName = $matchedName
            ObservedNames = $observedNames
        }
    } finally {
        if ($setupProcess -and -not $setupProcess.HasExited) {
            Stop-Process -Id $setupProcess.Id
            $setupProcess.WaitForExit(10000) | Out-Null
        }
    }
}

function Get-Median {
    param([Parameter(Mandatory)][double[]]$Values)

    $sorted = @($Values | Sort-Object)
    if ($sorted.Count % 2 -eq 1) {
        return $sorted[[int][math]::Floor($sorted.Count / 2)]
    }

    $upper = $sorted.Count / 2
    return ($sorted[$upper - 1] + $sorted[$upper]) / 2
}

function Get-NearestRankPercentile {
    param(
        [Parameter(Mandatory)][double[]]$Values,
        [Parameter(Mandatory)][ValidateRange(0.01, 1.0)][double]$Percentile
    )

    $sorted = @($Values | Sort-Object)
    $index = [math]::Max(0, [math]::Ceiling($Percentile * $sorted.Count) - 1)
    return $sorted[$index]
}

function Get-MetricSummary {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][double[]]$Values
    )

    return [pscustomobject]@{
        Metric = $Name
        Count = $Values.Count
        Median = [math]::Round((Get-Median -Values $Values), 3)
        P95 = [math]::Round((Get-NearestRankPercentile -Values $Values -Percentile 0.95), 3)
        Minimum = [math]::Round(($Values | Measure-Object -Minimum).Minimum, 3)
        Maximum = [math]::Round(($Values | Measure-Object -Maximum).Maximum, 3)
        Raw = $Values
    }
}

function Write-BenchmarkCheckpoint {
    param(
        [Parameter(Mandatory)][string]$OutputPath,
        [Parameter(Mandatory)][string]$Architecture,
        [Parameter(Mandatory)]$RunIdentity,
        [Parameter(Mandatory)][int]$TargetTrials,
        [Parameter(Mandatory)][int]$CompletedTrials,
        [Parameter(Mandatory)]
        [AllowEmptyCollection()]
        [object[]]$TrialResults,
        [Parameter(Mandatory)]
        [AllowEmptyCollection()]
        [object[]]$QueryResults
    )

    $checkpoint = [pscustomobject]@{
        SchemaVersion = 1
        UpdatedAtEt = [DateTimeOffset]::Now.ToString('o')
        Architecture = $Architecture
        RunIdentity = $RunIdentity
        TargetTrials = $TargetTrials
        CompletedTrials = $CompletedTrials
        TrialResults = $TrialResults
        QueryResults = $QueryResults
    }
    $checkpoint |
        ConvertTo-Json -Depth 15 |
        Set-Content -LiteralPath (Join-Path $OutputPath 'benchmark-checkpoint.json') -Encoding utf8
    $TrialResults |
        Export-Csv -LiteralPath (Join-Path $OutputPath 'launch-and-process-samples.checkpoint.csv') -NoTypeInformation -Encoding utf8
    $QueryResults |
        Select-Object Trial, Architecture, Id, Category, Query, Passed, ElapsedMilliseconds, CpuMilliseconds, MatchedName |
        Export-Csv -LiteralPath (Join-Path $OutputPath 'query-samples.checkpoint.csv') -NoTypeInformation -Encoding utf8
}

$trustedApplicationRoot = Get-FullPath $ApplicationRoot
$trustedEvidenceRoot = Get-FullPath $EvidenceRoot
$trustedAutomationRoot = Get-FullPath $AutomationToolRoot
foreach ($trustedRoot in @($trustedApplicationRoot, $trustedEvidenceRoot, $trustedAutomationRoot)) {
    if (-not (Test-Path -LiteralPath $trustedRoot -PathType Container)) {
        throw "Trusted root does not exist: $trustedRoot"
    }
    $rootItem = Get-Item -LiteralPath $trustedRoot -Force
    if ($rootItem.Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)) {
        throw "Trusted root must not be a reparse point: $trustedRoot"
    }
}
$applicationPrefix = "$($trustedApplicationRoot.TrimEnd('\'))\"
$evidencePrefix = "$($trustedEvidenceRoot.TrimEnd('\'))\"
if ($trustedApplicationRoot.Equals($trustedEvidenceRoot, [StringComparison]::OrdinalIgnoreCase) -or
    $trustedApplicationRoot.StartsWith($evidencePrefix, [StringComparison]::OrdinalIgnoreCase) -or
    $trustedEvidenceRoot.StartsWith($applicationPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "ApplicationRoot and EvidenceRoot must be disjoint directories."
}

$configPath = Assert-DescendantPath `
    -Path $ConfigurationPath `
    -Root $trustedEvidenceRoot `
    -Context "Configuration"
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw "Configuration file does not exist: $configPath"
}
$outputPath = Assert-DescendantPath `
    -Path $OutputDirectory `
    -Root $trustedEvidenceRoot `
    -Context "Output directory"
$config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json

Assert-ObjectProperties `
    -InputObject $config `
    -Allowed @("schemaVersion", "application", "timeouts", "shortcuts", "scenarios") `
    -Required @("schemaVersion", "application", "timeouts", "shortcuts", "scenarios") `
    -Context "Benchmark configuration"
if ([int]$config.schemaVersion -ne 1) {
    throw "Unsupported benchmark schema version: $($config.schemaVersion)"
}
Assert-ObjectProperties `
    -InputObject $config.application `
    -Allowed @(
        "executablePath",
        "workingDirectory",
        "activationExecutablePath",
        "windowTitle",
        "settingsWindowTitle",
        "queryAutomationId",
        "resultListAutomationId",
        "profilePath",
        "profileTemplatePath",
        "logsRelativePath",
        "winAppPath"
    ) `
    -Required @(
        "executablePath",
        "workingDirectory",
        "activationExecutablePath",
        "windowTitle",
        "settingsWindowTitle",
        "queryAutomationId",
        "resultListAutomationId",
        "profilePath",
        "profileTemplatePath"
    ) `
    -Context "application"
Assert-ObjectProperties `
    -InputObject $config.timeouts `
    -Allowed @("launchMilliseconds", "queryMilliseconds", "readyDelayMilliseconds") `
    -Required @("launchMilliseconds", "queryMilliseconds", "readyDelayMilliseconds") `
    -Context "timeouts"
Assert-ObjectProperties `
    -InputObject $config.shortcuts `
    -Allowed @("hide", "settings", "settingsWinApp", "hideWinApp") `
    -Required @("hide", "settings") `
    -Context "shortcuts"

$scenarios = @($config.scenarios)
if ($scenarios.Count -lt 1 -or $scenarios.Count -gt 100) {
    throw "Benchmark configuration must contain 1 to 100 scenarios."
}
$approvedSetupCommands = Get-ApprovedSetupCommandMap -Entries $ApprovedSetupCommand
$scenarioIds = @{}
foreach ($scenario in $scenarios) {
    Assert-ObjectProperties `
        -InputObject $scenario `
        -Allowed @(
            "id",
            "category",
            "query",
            "expected",
            "expectedMode",
            "matchMode",
            "expectedPattern",
            "expectedTokens",
            "setupProcess",
            "setupArguments"
        ) `
        -Required @("id", "category", "query") `
        -Context "scenario"
    $scenarioId = [string]$scenario.id
    if ($scenarioId -notmatch "^[a-z0-9]+(?:-[a-z0-9]+)*$") {
        throw "Scenario id must be kebab-case: $scenarioId"
    }
    if ($scenarioIds.ContainsKey($scenarioId)) {
        throw "Scenario id is duplicated: $scenarioId"
    }
    $scenarioIds[$scenarioId] = $true
    if ([string]::IsNullOrWhiteSpace([string]$scenario.category) -or
        [string]::IsNullOrWhiteSpace([string]$scenario.query) -or
        ([string]$scenario.query).Length -gt 4096) {
        throw "Scenario '$scenarioId' has an invalid category or query."
    }

    $expectedMode = Get-OptionalPropertyValue -InputObject $scenario -Name "expectedMode"
    $matchMode = if ($expectedMode) {
        [string]$expectedMode
    } else {
        [string](Get-OptionalPropertyValue -InputObject $scenario -Name "matchMode" -DefaultValue "contains")
    }
    if ($matchMode -notin @("contains", "startsWith", "regex", "allContains", "process_name_pid")) {
        throw "Scenario '$scenarioId' uses unsupported match mode '$matchMode'."
    }
    switch ($matchMode) {
        "regex" {
            $pattern = Get-OptionalPropertyValue -InputObject $scenario -Name "expectedPattern"
            if ([string]::IsNullOrWhiteSpace([string]$pattern)) {
                throw "Scenario '$scenarioId' requires expectedPattern."
            }
            [void][regex]::new([string]$pattern)
        }
        "allContains" {
            $tokens = @(Get-OptionalPropertyValue -InputObject $scenario -Name "expectedTokens")
            if ($tokens.Count -eq 0) {
                throw "Scenario '$scenarioId' requires expectedTokens."
            }
        }
        "process_name_pid" {
            if (-not (Get-OptionalPropertyValue -InputObject $scenario -Name "setupProcess")) {
                throw "Scenario '$scenarioId' requires an approved setup process."
            }
        }
        default {
            $expected = Get-OptionalPropertyValue -InputObject $scenario -Name "expected"
            if ([string]::IsNullOrWhiteSpace([string]$expected)) {
                throw "Scenario '$scenarioId' requires expected."
            }
        }
    }

    $setupProcess = Get-OptionalPropertyValue -InputObject $scenario -Name "setupProcess"
    if ($setupProcess) {
        $setupArguments = [string](Get-OptionalPropertyValue `
                -InputObject $scenario `
                -Name "setupArguments" `
                -DefaultValue "")
        [void](Resolve-ApprovedSetupCommand `
                -Executable ([string]$setupProcess) `
                -Arguments $setupArguments `
                -ApprovedCommands $approvedSetupCommands `
                -ScenarioId $scenarioId)
    }
}

$applicationPath = Assert-DescendantPath `
    -Path ([string]$config.application.executablePath) `
    -Root $trustedApplicationRoot `
    -Context "Application executable"
$applicationDirectory = Assert-DescendantPath `
    -Path ([string]$config.application.workingDirectory) `
    -Root $trustedApplicationRoot `
    -Context "Application working directory" `
    -AllowRoot
$activationPath = Assert-DescendantPath `
    -Path ([string]$config.application.activationExecutablePath) `
    -Root $trustedApplicationRoot `
    -Context "Activation executable"
$profilePath = Assert-DescendantPath `
    -Path ([string]$config.application.profilePath) `
    -Root $trustedApplicationRoot `
    -Context "Profile destination"
$profileTemplatePath = Assert-DescendantPath `
    -Path ([string]$config.application.profileTemplatePath) `
    -Root $trustedEvidenceRoot `
    -Context "Profile template"
if (-not (Test-Path -LiteralPath $applicationPath -PathType Leaf) -or
    -not (Test-Path -LiteralPath $activationPath -PathType Leaf) -or
    -not (Test-Path -LiteralPath $applicationDirectory -PathType Container) -or
    -not (Test-Path -LiteralPath $profileTemplatePath -PathType Container)) {
    throw "Application, activation, working-directory, or template path is missing."
}

$processName = [IO.Path]::GetFileNameWithoutExtension($applicationPath)
$windowTitle = [string]$config.application.windowTitle
$settingsWindowTitle = [string]$config.application.settingsWindowTitle
$queryAutomationId = [string]$config.application.queryAutomationId
$resultListAutomationId = [string]$config.application.resultListAutomationId
$launchTimeout = [int]$config.timeouts.launchMilliseconds
$queryTimeout = [int]$config.timeouts.queryMilliseconds
$readyDelay = [int]$config.timeouts.readyDelayMilliseconds
if ($launchTimeout -lt 100 -or $launchTimeout -gt 300000 -or
    $queryTimeout -lt 100 -or $queryTimeout -gt 300000 -or
    $readyDelay -lt 0 -or $readyDelay -gt 300000) {
    throw "Timeouts are outside the supported range."
}
foreach ($requiredText in @($windowTitle, $settingsWindowTitle, $queryAutomationId, $resultListAutomationId)) {
    if ([string]::IsNullOrWhiteSpace($requiredText) -or $requiredText.Length -gt 256) {
        throw "Window titles and automation ids must contain 1 to 256 characters."
    }
}

$hideShortcut = [string]$config.shortcuts.hide
$settingsShortcut = [string]$config.shortcuts.settings
$settingsWinAppKeys = [string](Get-OptionalPropertyValue `
        -InputObject $config.shortcuts `
        -Name 'settingsWinApp' `
        -DefaultValue 'ctrl+i')
$hideWinAppKeys = [string](Get-OptionalPropertyValue `
        -InputObject $config.shortcuts `
        -Name 'hideWinApp' `
        -DefaultValue 'esc')
$winAppPath = Get-OptionalPropertyValue -InputObject $config.application -Name 'winAppPath'
if ($winAppPath) {
    $winAppPath = Assert-DescendantPath `
        -Path ([string]$winAppPath) `
        -Root $trustedAutomationRoot `
        -Context "WinApp executable"
    if (-not (Test-Path -LiteralPath $winAppPath -PathType Leaf)) {
        throw "WinApp executable does not exist: $winAppPath"
    }
}

$logsPath = $null
$logsRelativePath = Get-OptionalPropertyValue -InputObject $config.application -Name 'logsRelativePath'
if ($logsRelativePath) {
    if ([IO.Path]::IsPathRooted([string]$logsRelativePath)) {
        throw "logsRelativePath must be relative."
    }
    $logsPath = Assert-DescendantPath `
        -Path (Join-Path $profilePath ([string]$logsRelativePath)) `
        -Root $profilePath `
        -Context "Log path"
}

$identityDetails = [ordered]@{
    ArchitectureLabel = $ArchitectureLabel
    TargetTrials = $Trials
    ConfigurationSha256 = (Get-FileHash -LiteralPath $configPath -Algorithm SHA256).Hash.ToLowerInvariant()
    ApplicationSha256 = (Get-FileHash -LiteralPath $applicationPath -Algorithm SHA256).Hash.ToLowerInvariant()
    ActivationSha256 = (Get-FileHash -LiteralPath $activationPath -Algorithm SHA256).Hash.ToLowerInvariant()
    ProfileTemplateSha256 = Get-DirectoryFingerprint $profileTemplatePath
    AutomationToolSha256 = if ($winAppPath) {
        (Get-FileHash -LiteralPath $winAppPath -Algorithm SHA256).Hash.ToLowerInvariant()
    } else {
        $null
    }
    ApprovedSetupCommands = @($ApprovedSetupCommand | Sort-Object)
}
$identityJson = $identityDetails | ConvertTo-Json -Depth 6 -Compress
$runIdentity = [pscustomobject]@{
    Sha256 = Get-StringSha256 $identityJson
    Details = $identityDetails
}

[void][IO.Directory]::CreateDirectory($outputPath)
$trialResults = @()
$queryResults = @()
$startTrial = 1
$checkpointPath = Join-Path $outputPath 'benchmark-checkpoint.json'
if ($Resume -and (Test-Path -LiteralPath $checkpointPath)) {
    $checkpoint = Get-Content -LiteralPath $checkpointPath -Raw | ConvertFrom-Json
    $checkpointProperties = @($checkpoint.PSObject.Properties.Name)
    if ($checkpointProperties -notcontains "RunIdentity" -or
        $checkpointProperties -notcontains "TargetTrials") {
        throw "Checkpoint predates run-identity validation and cannot be resumed."
    }
    if ([string]$checkpoint.Architecture -ne $ArchitectureLabel -or
        [string]$checkpoint.RunIdentity.Sha256 -ne [string]$runIdentity.Sha256) {
        throw "Checkpoint run identity does not match the requested benchmark."
    }
    if ([int]$checkpoint.TargetTrials -ne $Trials) {
        throw "Checkpoint target trials '$($checkpoint.TargetTrials)' do not match '$Trials'."
    }
    if ([int]$checkpoint.CompletedTrials -lt 0 -or
        [int]$checkpoint.CompletedTrials -gt $Trials -or
        @($checkpoint.TrialResults).Count -ne [int]$checkpoint.CompletedTrials -or
        @($checkpoint.QueryResults).Count -ne ([int]$checkpoint.CompletedTrials * $scenarios.Count)) {
        throw "Checkpoint sample counts are inconsistent."
    }

    $trialResults = @($checkpoint.TrialResults)
    $queryResults = @($checkpoint.QueryResults)
    $startTrial = [int]$checkpoint.CompletedTrials + 1
    Write-Host "Resuming after trial $($checkpoint.CompletedTrials)"
}

for ($trial = $startTrial; $trial -le $Trials; $trial++) {
    Stop-ApplicationProcesses -ProcessName $processName -ExecutablePath $applicationPath
    Restore-Profile `
        -TemplatePath $profileTemplatePath `
        -ProfilePath $profilePath `
        -TrustedTemplateRoot $trustedEvidenceRoot `
        -TrustedApplicationRoot $trustedApplicationRoot

    $coldWatch = [Diagnostics.Stopwatch]::StartNew()
    $applicationProcess = Start-Process `
        -FilePath $applicationPath `
        -WorkingDirectory $applicationDirectory `
        -PassThru
    $window = Wait-AutomationWindow `
        -ProcessId $applicationProcess.Id `
        -Title $windowTitle `
        -TimeoutMilliseconds $launchTimeout
    if (-not $window) {
        throw "Trial $trial did not expose '$windowTitle' within $launchTimeout ms."
    }
    $queryElement = Wait-AutomationElement `
        -Root $window `
        -AutomationId $queryAutomationId `
        -TimeoutMilliseconds $launchTimeout
    if (-not $queryElement) {
        throw "Trial $trial did not expose '$queryAutomationId'."
    }
    $coldWatch.Stop()

    $valuePattern = [System.Windows.Automation.ValuePattern]$queryElement.GetCurrentPattern(
        [System.Windows.Automation.ValuePattern]::Pattern)
    Start-Sleep -Milliseconds $readyDelay

    foreach ($scenario in $scenarios) {
        $scenarioResult = Invoke-QueryScenario `
            -Window $window `
            -ValuePattern $valuePattern `
            -ResultListAutomationId $resultListAutomationId `
            -Scenario $scenario `
            -ApplicationProcess $applicationProcess `
            -TimeoutMilliseconds $queryTimeout `
            -ApprovedSetupCommands $approvedSetupCommands
        $scenarioResult | Add-Member -NotePropertyName Trial -NotePropertyValue $trial
        $scenarioResult | Add-Member -NotePropertyName Architecture -NotePropertyValue $ArchitectureLabel
        $queryResults += $scenarioResult
    }

    $valuePattern.SetValue('')
    Start-Sleep -Milliseconds 100
    $hidden = $false
    for ($hideAttempt = 1; $hideAttempt -le 3 -and -not $hidden; $hideAttempt++) {
        if ($winAppPath) {
            $hideFocus = & $winAppPath ui focus $queryAutomationId `
                -w ([long]$window.Current.NativeWindowHandle) `
                --json 2>&1
            if ($LASTEXITCODE -ne 0) {
                throw "Trial $trial could not focus the search window before hiding it: $($hideFocus -join ' ')"
            }
            $hideInput = & $winAppPath ui send-keys $hideWinAppKeys `
                -w ([long]$window.Current.NativeWindowHandle) `
                --target $queryAutomationId `
                --via send-input `
                --json 2>&1
            if ($LASTEXITCODE -ne 0) {
                throw "Trial $trial could not hide the search window: $($hideInput -join ' ')"
            }
        } else {
            $window.SetFocus()
            Start-Sleep -Milliseconds 50
            [System.Windows.Forms.SendKeys]::SendWait($hideShortcut)
        }

        $hidden = Wait-WindowHidden `
            -ProcessId $applicationProcess.Id `
            -Title $windowTitle `
            -TimeoutMilliseconds 2000
    }
    if (-not $hidden) {
        throw "Trial $trial did not hide '$windowTitle' after 3 attempts."
    }

    $warmWatch = [Diagnostics.Stopwatch]::StartNew()
    Start-Process -FilePath $activationPath -WorkingDirectory (Split-Path -Parent $activationPath) | Out-Null
    $window = Wait-AutomationWindow `
        -ProcessId $applicationProcess.Id `
        -Title $windowTitle `
        -TimeoutMilliseconds $launchTimeout
    if (-not $window) {
        throw "Trial $trial warm activation did not expose '$windowTitle'."
    }
    $queryElement = Wait-AutomationElement `
        -Root $window `
        -AutomationId $queryAutomationId `
        -TimeoutMilliseconds $launchTimeout
    if (-not $queryElement) {
        throw "Trial $trial warm activation did not expose '$queryAutomationId'."
    }
    $warmWatch.Stop()

    $settingsWindow = $null
    if ($winAppPath) {
        $settingsAttemptErrors = @()
        for ($settingsAttempt = 1; $settingsAttempt -le 3 -and -not $settingsWindow; $settingsAttempt++) {
            $settingsFocus = & $winAppPath ui focus $queryAutomationId `
                -w ([long]$window.Current.NativeWindowHandle) `
                --json 2>&1
            if ($LASTEXITCODE -ne 0) {
                $settingsAttemptErrors += "focus attempt $settingsAttempt`: $($settingsFocus -join ' ')"
                continue
            }

            $attemptWatch = [Diagnostics.Stopwatch]::StartNew()
            $settingsInput = & $winAppPath ui send-keys $settingsWinAppKeys `
                -w ([long]$window.Current.NativeWindowHandle) `
                --target $queryAutomationId `
                --via send-input `
                --json 2>&1
            if ($LASTEXITCODE -ne 0) {
                $attemptWatch.Stop()
                $settingsAttemptErrors += "send attempt $settingsAttempt`: $($settingsInput -join ' ')"
                continue
            }

            $settingsWindow = Wait-AutomationWindow `
                -ProcessId $applicationProcess.Id `
                -Title $settingsWindowTitle `
                -TimeoutMilliseconds $launchTimeout
            $attemptWatch.Stop()
            if ($settingsWindow) {
                $settingsWatch = $attemptWatch
            } else {
                $settingsAttemptErrors += "window attempt $settingsAttempt`: '$settingsWindowTitle' did not appear"
            }
        }

        if (-not $settingsWindow) {
            throw "Trial $trial could not open Settings after 3 attempts: $($settingsAttemptErrors -join ' | ')"
        }
    } else {
        $settingsWatch = [Diagnostics.Stopwatch]::StartNew()
        $window.SetFocus()
        Start-Sleep -Milliseconds 50
        [System.Windows.Forms.SendKeys]::SendWait($settingsShortcut)
        $settingsWindow = Wait-AutomationWindow `
            -ProcessId $applicationProcess.Id `
            -Title $settingsWindowTitle `
            -TimeoutMilliseconds $launchTimeout
        $settingsWatch.Stop()
    }
    if (-not $settingsWindow) {
        throw "Trial $trial did not expose '$settingsWindowTitle'."
    }

    $applicationProcess.Refresh()
    $trialResults += [pscustomobject]@{
        Trial = $trial
        Architecture = $ArchitectureLabel
        ProcessId = $applicationProcess.Id
        ProcessColdWindowMilliseconds = [math]::Round($coldWatch.Elapsed.TotalMilliseconds, 3)
        WarmActivationMilliseconds = [math]::Round($warmWatch.Elapsed.TotalMilliseconds, 3)
        SettingsOpenMilliseconds = [math]::Round($settingsWatch.Elapsed.TotalMilliseconds, 3)
        WorkingSetBytes = $applicationProcess.WorkingSet64
        PrivateMemoryBytes = $applicationProcess.PrivateMemorySize64
        CpuMilliseconds = [math]::Round($applicationProcess.TotalProcessorTime.TotalMilliseconds, 3)
    }

    if ($logsPath -and (Test-Path -LiteralPath $logsPath)) {
        $trialLogPath = Join-Path $outputPath ("logs\trial-{0:D2}" -f $trial)
        [void][IO.Directory]::CreateDirectory((Split-Path -Parent $trialLogPath))
        Copy-Item -LiteralPath $logsPath -Destination $trialLogPath -Recurse
    }

    Write-BenchmarkCheckpoint `
        -OutputPath $outputPath `
        -Architecture $ArchitectureLabel `
        -RunIdentity $runIdentity `
        -TargetTrials $Trials `
        -CompletedTrials $trial `
        -TrialResults $trialResults `
        -QueryResults $queryResults
    Write-Host "Completed trial $trial of $Trials"

    Stop-Process -Id $applicationProcess.Id
    $applicationProcess.WaitForExit(15000) | Out-Null
}

$summaries = @(
    Get-MetricSummary `
        -Name 'process-cold-window-ms' `
        -Values ([double[]]@($trialResults.ProcessColdWindowMilliseconds))
    Get-MetricSummary `
        -Name 'warm-activation-ms' `
        -Values ([double[]]@($trialResults.WarmActivationMilliseconds))
    Get-MetricSummary `
        -Name 'settings-open-ms' `
        -Values ([double[]]@($trialResults.SettingsOpenMilliseconds))
    Get-MetricSummary `
        -Name 'working-set-bytes' `
        -Values ([double[]]@($trialResults.WorkingSetBytes))
    Get-MetricSummary `
        -Name 'private-memory-bytes' `
        -Values ([double[]]@($trialResults.PrivateMemoryBytes))
    Get-MetricSummary `
        -Name 'process-cpu-ms' `
        -Values ([double[]]@($trialResults.CpuMilliseconds))
)

foreach ($scenario in $scenarios) {
    $samples = @($queryResults | Where-Object Id -eq $scenario.id)
    $summaries += Get-MetricSummary `
        -Name "query-$($scenario.id)-ms" `
        -Values ([double[]]@($samples.ElapsedMilliseconds))
}

$result = [pscustomobject]@{
    SchemaVersion = 1
    CapturedAtEt = [DateTimeOffset]::Now.ToString('o')
    Architecture = $ArchitectureLabel
    Trials = $Trials
    ConfigurationPath = $configPath
    RunIdentity = $runIdentity
    PercentileMethod = 'nearest-rank'
    TrialResults = $trialResults
    QueryResults = $queryResults
    Summaries = $summaries
}

$result | ConvertTo-Json -Depth 15 | Set-Content -LiteralPath (Join-Path $outputPath 'benchmark-results.json') -Encoding utf8
$trialResults | Export-Csv -LiteralPath (Join-Path $outputPath 'launch-and-process-samples.csv') -NoTypeInformation -Encoding utf8
$queryResults |
    Select-Object Trial, Architecture, Id, Category, Query, Passed, ElapsedMilliseconds, CpuMilliseconds, MatchedName |
    Export-Csv -LiteralPath (Join-Path $outputPath 'query-samples.csv') -NoTypeInformation -Encoding utf8
$summaries |
    Select-Object Metric, Count, Median, P95, Minimum, Maximum |
    Export-Csv -LiteralPath (Join-Path $outputPath 'summary.csv') -NoTypeInformation -Encoding utf8

$failedScenarios = @($queryResults | Where-Object { -not $_.Passed })
if ($failedScenarios.Count -gt 0) {
    throw "$($failedScenarios.Count) query samples failed. Raw results were saved to $outputPath."
}

$summaries | Select-Object Metric, Count, Median, P95, Minimum, Maximum | Format-Table -AutoSize
