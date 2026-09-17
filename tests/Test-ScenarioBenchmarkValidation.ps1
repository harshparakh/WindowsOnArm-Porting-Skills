[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$pluginRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$runner = Join-Path $pluginRoot "scripts\Invoke-WoaScenarioBenchmark.ps1"
$schema = Join-Path $pluginRoot "schemas\woa-scenarios.schema.json"
$localApplicationData = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
if ([string]::IsNullOrWhiteSpace($localApplicationData)) {
    throw "LocalApplicationData is not available."
}
$scratchRoot = Join-Path $localApplicationData "AgencyCowork\browser-scratch\repo-to-arm\scenario-harness"
$testRoot = Join-Path $scratchRoot ("validation-" + [Guid]::NewGuid().ToString("N"))

function Write-Utf8File {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)]
        [AllowEmptyString()]
        [string]$Content
    )

    [void][IO.Directory]::CreateDirectory((Split-Path -Parent $Path))
    [IO.File]::WriteAllText($Path, $Content, [Text.UTF8Encoding]::new($false))
}

function Get-LowerFileHash {
    param([Parameter(Mandatory)][string]$Path)

    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Import-RunnerFunctions {
    $oldValue = $env:WOA_SCENARIO_BENCHMARK_DOT_SOURCE_ONLY
    $env:WOA_SCENARIO_BENCHMARK_DOT_SOURCE_ONLY = "1"
    try {
        . $runner `
            -ConfigurationPath (Join-Path $testRoot "placeholder-config.json") `
            -OutputDirectory (Join-Path $testRoot "placeholder-output") `
            -ArchitectureLabel "validation-test" `
            -ApplicationRoot (Join-Path $testRoot "placeholder-app") `
            -EvidenceRoot (Join-Path $testRoot "placeholder-evidence") `
            -ManifestPath (Join-Path $testRoot "placeholder-manifest.json") `
            -ApprovedManifestSha256 ("0" * 64)
    } finally {
        $env:WOA_SCENARIO_BENCHMARK_DOT_SOURCE_ONLY = $oldValue
    }
}

function New-TestLayout {
    param([Parameter(Mandatory)][string]$Name)

    $caseRoot = Join-Path $testRoot $Name
    $applicationRoot = Join-Path $caseRoot "application"
    $evidenceRoot = Join-Path $caseRoot "evidence"
    $templateRoot = Join-Path $evidenceRoot "templates\input"
    [void][IO.Directory]::CreateDirectory($applicationRoot)
    [void][IO.Directory]::CreateDirectory($templateRoot)
    Write-Utf8File -Path (Join-Path $applicationRoot "App.exe") -Content "fake-app"
    Write-Utf8File -Path (Join-Path $applicationRoot "Dependency.dll") -Content "fake-dependency"
    Write-Utf8File -Path (Join-Path $templateRoot "settings.json") -Content "{}"
    $manifest = [ordered]@{
        schemaVersion = 1
        files = @(
            [ordered]@{
                relativePath = "App.exe"
                sha256 = Get-LowerFileHash (Join-Path $applicationRoot "App.exe")
            },
            [ordered]@{
                relativePath = "Dependency.dll"
                sha256 = Get-LowerFileHash (Join-Path $applicationRoot "Dependency.dll")
            }
        )
    }
    $manifestPath = Join-Path $evidenceRoot "manifest.json"
    $manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $manifestPath -Encoding utf8
    $config = [ordered]@{
        schemaVersion = 1
        application = [ordered]@{
            executableRelativePath = "App.exe"
            workingDirectoryRelativePath = ""
            arguments = @("--profile", '${profileDir}', "--input", '${fixture:input}')
            mainWindowSelector = [ordered]@{
                automationId = "MainWindow"
                controlType = "Window"
            }
            launchReadySteps = @(
                [ordered]@{
                    type = "wait-element"
                    selector = [ordered]@{
                        automationId = "Home"
                        controlType = "Pane"
                    }
                    timeoutMilliseconds = 100
                }
            )
        }
        defaults = [ordered]@{
            actionTimeoutMilliseconds = 100
            pollingIntervalMilliseconds = 10
            processExitTimeoutMilliseconds = 100
        }
        fixtures = @(
            [ordered]@{
                id = "input"
                templatePath = $templateRoot
            }
        )
        scenarios = @(
            [ordered]@{
                id = "home-ready"
                name = "Home ready"
                steps = @(
                    [ordered]@{
                        type = "set-value"
                        selector = [ordered]@{
                            automationId = "FolderPath"
                            controlType = "Edit"
                        }
                        value = '${fixture:input}'
                        timeoutMilliseconds = 100
                    },
                    [ordered]@{
                        type = "assert-property"
                        selector = [ordered]@{
                            automationId = "FolderPath"
                            controlType = "Edit"
                        }
                        property = "Value"
                        expected = '${fixture:input}'
                        timeoutMilliseconds = 100
                    },
                    [ordered]@{
                        type = "assert-text"
                        expectedText = '${profileDir}'
                        timeoutMilliseconds = 100
                    }
                )
            }
        )
    }
    $configPath = Join-Path $evidenceRoot "scenario.json"
    $config | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $configPath -Encoding utf8
    return [pscustomobject]@{
        CaseRoot = $caseRoot
        ApplicationRoot = $applicationRoot
        EvidenceRoot = $evidenceRoot
        ConfigPath = $configPath
        ManifestPath = $manifestPath
        ManifestHash = Get-LowerFileHash $manifestPath
        OutputPath = Join-Path $evidenceRoot "output"
        TemplateRoot = $templateRoot
        Config = $config
    }
}

function Invoke-ValidationOnly {
    param(
        [Parameter(Mandatory)]$Layout,
        [string]$ConfigPath = $Layout.ConfigPath,
        [string]$ManifestPath = $Layout.ManifestPath,
        [string]$ManifestHash = $Layout.ManifestHash,
        [string]$ApplicationRoot = $Layout.ApplicationRoot,
        [string]$EvidenceRoot = $Layout.EvidenceRoot,
        [string]$OutputPath = $Layout.OutputPath
    )

    & $runner `
        -ConfigurationPath $ConfigPath `
        -OutputDirectory $OutputPath `
        -ArchitectureLabel "validation-test" `
        -ApplicationRoot $ApplicationRoot `
        -EvidenceRoot $EvidenceRoot `
        -ManifestPath $ManifestPath `
        -ApprovedManifestSha256 $ManifestHash `
        -Trials 1 `
        -Smoke `
        -ValidationOnly | Out-Null
}

function Assert-Rejected {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][scriptblock]$Action,
        [Parameter(Mandatory)]
        [AllowEmptyString()]
        [string]$ExpectedMessage
    )

    $rejected = $false
    try {
        & $Action
    } catch {
        $rejected = $true
        if (-not [string]::IsNullOrWhiteSpace($ExpectedMessage) -and
            $_.Exception.Message -notlike "*$ExpectedMessage*") {
            throw "Unexpected rejection for '$Name': $($_.Exception.Message)"
        }
    }
    if (-not $rejected) {
        throw "Expected '$Name' to be rejected."
    }
}

try {
    [void][IO.Directory]::CreateDirectory($testRoot)
    . Import-RunnerFunctions

    $schemaJson = Get-Content -LiteralPath $schema -Raw | ConvertFrom-Json
    if ($schemaJson.title -ne "Windows on Arm Scenario Benchmark Configuration") {
        throw "Scenario schema did not parse as the expected document."
    }

    if (-not (Test-ExpectedValue -Actual $false -Expected $false)) {
        throw "Boolean equality rejected matching Boolean values."
    }
    if (Test-ExpectedValue -Actual "False" -Expected $false) {
        throw "Boolean equality accepted a string observation."
    }
    if (Test-ExpectedValue -Actual "ready" -Expected "Ready") {
        throw "String equality was not ordinal and case-sensitive."
    }
    if (Test-ExpectedValue -Actual "5" -Expected 5) {
        throw "Numeric equality accepted a string observation."
    }

    foreach ($unsafeRelativePath in @(
            "App.exe:Zone.Identifier",
            "folder\CON\file.txt",
            "folder\bad. ",
            "folder\bad.",
            "folder\..\escape.txt")) {
        Assert-Rejected `
            -Name "unsafe-relative-$unsafeRelativePath" `
            -ExpectedMessage "" `
            -Action { Assert-RelativePath -Path $unsafeRelativePath -Context "Unsafe relative path" | Out-Null }
    }

    Assert-Rejected `
        -Name "drive-root" `
        -ExpectedMessage "drive root" `
        -Action { Assert-TrustedRoot -Path ([IO.Path]::GetPathRoot($localApplicationData)) -Context "Trusted root test" | Out-Null }
    Assert-Rejected `
        -Name "home-root" `
        -ExpectedMessage "user profile root" `
        -Action { Assert-TrustedRoot -Path ([Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)) -Context "Trusted root test" | Out-Null }
    Assert-Rejected `
        -Name "local-app-data-root" `
        -ExpectedMessage "LocalApplicationData root" `
        -Action { Assert-TrustedRoot -Path $localApplicationData -Context "Trusted root test" | Out-Null }

    $junctionTarget = Join-Path $testRoot "junction-target"
    $junctionRoot = Join-Path $testRoot "junction-root"
    [void][IO.Directory]::CreateDirectory((Join-Path $junctionTarget "child"))
    [void](New-Item -ItemType Junction -Path $junctionRoot -Target $junctionTarget)
    Assert-Rejected `
        -Name "reparse-ancestor" `
        -ExpectedMessage "reparse point ancestor" `
        -Action { Assert-TrustedRoot -Path (Join-Path $junctionRoot "child") -Context "Trusted root test" | Out-Null }

    $valid = New-TestLayout -Name "valid"
    Invoke-ValidationOnly -Layout $valid
    $validationReportPath = Join-Path $valid.OutputPath "validation-only.json"
    $validationReport = Get-Content -LiteralPath $validationReportPath -Raw | ConvertFrom-Json
    if (-not $validationReport.ValidationOnly -or
        $validationReport.Confidence -ne "validation-only-not-device-proof" -or
        $validationReport.Message -notlike "*No process was launched*") {
        throw "Validation-only report did not clearly avoid device proof claims."
    }

    $noArgumentConfig = New-TestLayout -Name "no-arguments"
    $noArgumentConfig.Config.application.arguments = @()
    $noArgumentConfig.Config.fixtures = @()
    $noArgumentConfig.Config.scenarios[0].steps = @(
        [ordered]@{
            type = "assert-text"
            expectedText = "Ready"
            timeoutMilliseconds = 100
        }
    )
    $noArgumentPath = Join-Path $noArgumentConfig.EvidenceRoot "no-arguments.json"
    $noArgumentConfig.Config | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $noArgumentPath -Encoding utf8
    Invoke-ValidationOnly -Layout $noArgumentConfig -ConfigPath $noArgumentPath

    $existingOutput = New-TestLayout -Name "existing-output"
    [void][IO.Directory]::CreateDirectory($existingOutput.OutputPath)
    $sentinel = Join-Path $existingOutput.OutputPath "sentinel.txt"
    Write-Utf8File -Path $sentinel -Content "keep"
    Assert-Rejected `
        -Name "existing-output" `
        -ExpectedMessage "Output directory already exists" `
        -Action { Invoke-ValidationOnly -Layout $existingOutput }
    if ((Get-Content -LiteralPath $sentinel -Raw) -ne "keep") {
        throw "Existing output sentinel was modified."
    }

    $badHash = New-TestLayout -Name "bad-hash"
    Assert-Rejected `
        -Name "manifest-hash" `
        -ExpectedMessage "Manifest SHA256 does not match" `
        -Action { Invoke-ValidationOnly -Layout $badHash -ManifestHash ("0" * 64) }

    $extraFile = New-TestLayout -Name "extra-file"
    Write-Utf8File -Path (Join-Path $extraFile.ApplicationRoot "Extra.dll") -Content "unexpected"
    Assert-Rejected `
        -Name "manifest-closure" `
        -ExpectedMessage "complete application package closure" `
        -Action { Invoke-ValidationOnly -Layout $extraFile }

    $hiddenFile = New-TestLayout -Name "hidden-file"
    $hiddenPath = Join-Path $hiddenFile.ApplicationRoot "Hidden.dll"
    Write-Utf8File -Path $hiddenPath -Content "hidden"
    (Get-Item -LiteralPath $hiddenPath).Attributes = [IO.FileAttributes]::Hidden
    Assert-Rejected `
        -Name "hidden-manifest-closure" `
        -ExpectedMessage "complete application package closure" `
        -Action { Invoke-ValidationOnly -Layout $hiddenFile }

    $escapeConfig = New-TestLayout -Name "path-escape"
    $escapeConfig.Config.application.executableRelativePath = "..\outside.exe"
    $escapePath = Join-Path $escapeConfig.EvidenceRoot "escape.json"
    $escapeConfig.Config | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $escapePath -Encoding utf8
    Assert-Rejected `
        -Name "path-traversal" `
        -ExpectedMessage "traversal segments" `
        -Action { Invoke-ValidationOnly -Layout $escapeConfig -ConfigPath $escapePath }

    $adsConfig = New-TestLayout -Name "ads-path"
    $adsConfig.Config.application.executableRelativePath = "App.exe:Zone.Identifier"
    $adsPath = Join-Path $adsConfig.EvidenceRoot "ads.json"
    $adsConfig.Config | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $adsPath -Encoding utf8
    Assert-Rejected `
        -Name "ads-path" `
        -ExpectedMessage "alternate data stream" `
        -Action { Invoke-ValidationOnly -Layout $adsConfig -ConfigPath $adsPath }

    $placeholderConfig = New-TestLayout -Name "placeholder"
    $placeholderConfig.Config.scenarios[0].steps[0].value = '${fixture:missing}'
    $placeholderPath = Join-Path $placeholderConfig.EvidenceRoot "placeholder.json"
    $placeholderConfig.Config | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $placeholderPath -Encoding utf8
    Assert-Rejected `
        -Name "unknown-placeholder" `
        -ExpectedMessage "unsupported placeholder" `
        -Action { Invoke-ValidationOnly -Layout $placeholderConfig -ConfigPath $placeholderPath }

    $outsideConfig = New-TestLayout -Name "outside-config"
    $outsidePath = Join-Path $outsideConfig.CaseRoot "outside.json"
    $outsideConfig.Config | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $outsidePath -Encoding utf8
    Assert-Rejected `
        -Name "config-outside-evidence" `
        -ExpectedMessage "Configuration is outside the trusted root" `
        -Action { Invoke-ValidationOnly -Layout $outsideConfig -ConfigPath $outsidePath }

    $mutationConfig = New-TestLayout -Name "template-mutation"
    $manifestInfo = Read-ApprovedManifest `
        -Path $mutationConfig.ManifestPath `
        -ExpectedSha256 $mutationConfig.ManifestHash `
        -ApplicationRoot $mutationConfig.ApplicationRoot
    $configuration = Read-ScenarioConfiguration `
        -Path $mutationConfig.ConfigPath `
        -ApplicationRoot $mutationConfig.ApplicationRoot `
        -EvidenceRoot $mutationConfig.EvidenceRoot `
        -Manifest $manifestInfo
    $configurationHash = Get-LowerFileHash $mutationConfig.ConfigPath
    Write-Utf8File -Path (Join-Path $mutationConfig.TemplateRoot "mutated.txt") -Content "changed"
    Assert-Rejected `
        -Name "fixture-template-mutation" `
        -ExpectedMessage "changed after validation" `
        -Action {
            Assert-ScenarioInputsUnchanged `
                -ConfigurationPath $mutationConfig.ConfigPath `
                -ExpectedConfigurationSha256 $configurationHash `
                -Configuration $configuration
        }

    $missingArchitecture = New-TestLayout -Name "missing-architecture"
    Assert-Rejected `
        -Name "actual-execution-requires-architecture" `
        -ExpectedMessage "ExpectedArchitecture is required" `
        -Action {
            & $runner `
                -ConfigurationPath $missingArchitecture.ConfigPath `
                -OutputDirectory $missingArchitecture.OutputPath `
                -ArchitectureLabel "validation-test" `
                -ApplicationRoot $missingArchitecture.ApplicationRoot `
                -EvidenceRoot $missingArchitecture.EvidenceRoot `
                -ManifestPath $missingArchitecture.ManifestPath `
                -ApprovedManifestSha256 $missingArchitecture.ManifestHash `
                -Trials 1 `
                -Smoke | Out-Null
        }

    $lowTrialConfig = New-TestLayout -Name "low-trials"
    Assert-Rejected `
        -Name "low-trial-benchmark" `
        -ExpectedMessage "At least 10 trials" `
        -Action {
            & $runner `
                -ConfigurationPath $lowTrialConfig.ConfigPath `
                -OutputDirectory $lowTrialConfig.OutputPath `
                -ArchitectureLabel "validation-test" `
                -ApplicationRoot $lowTrialConfig.ApplicationRoot `
                -EvidenceRoot $lowTrialConfig.EvidenceRoot `
                -ManifestPath $lowTrialConfig.ManifestPath `
                -ApprovedManifestSha256 $lowTrialConfig.ManifestHash `
                -ExpectedArchitecture "arm64" `
                -Trials 1 | Out-Null
        }

    [pscustomobject]@{
        Passed = 20
        ScratchRoot = $testRoot
        ValidationOnly = "passed"
        NoArgumentNoFixture = "passed"
        StrictExpectedValue = "passed"
        UnsafeRelativePaths = "rejected"
        TrustedRootRisks = "rejected"
        ReparseAncestor = "rejected"
        ExistingOutput = "rejected-preserved"
        ManifestHashMismatch = "rejected"
        ManifestClosureMismatch = "rejected"
        HiddenClosureMismatch = "rejected"
        PlaceholderValidation = "rejected"
        ConfigOutsideEvidence = "rejected"
        FixtureTemplateMutation = "rejected"
        MissingExecutionArchitecture = "rejected"
        LowTrialBenchmarkClaim = "rejected"
    } | ConvertTo-Json
} finally {
    if (Get-Variable -Name junctionRoot -Scope Script -ErrorAction SilentlyContinue) {
        if (Test-Path -LiteralPath $junctionRoot) {
            [IO.Directory]::Delete($junctionRoot)
        }
    }
    if (Test-Path -LiteralPath $testRoot) {
        [IO.Directory]::Delete($testRoot, $true)
    }
}
