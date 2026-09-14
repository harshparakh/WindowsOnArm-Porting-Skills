[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$script = Join-Path $root "scripts\Compare-WoaBenchmarks.ps1"
$testRoot = Join-Path $env:TEMP ("woa-comparison-" + [Guid]::NewGuid().ToString("N"))
[void][IO.Directory]::CreateDirectory($testRoot)

function New-Benchmark {
    param(
        [Parameter(Mandatory)][int]$Trials,
        [Parameter(Mandatory)][int]$Count,
        [Parameter(Mandatory)][double]$ColdMedian,
        [Parameter(Mandatory)][double]$QueryMedian
    )

    return [pscustomobject]@{
        Trials = $Trials
        Summaries = @(
            [pscustomobject]@{
                Metric = "process-cold-window-ms"
                Count = $Count
                Median = $ColdMedian
                P95 = $ColdMedian
            },
            [pscustomobject]@{
                Metric = "query-calculator-ms"
                Count = $Count
                Median = $QueryMedian
                P95 = $QueryMedian
            }
        )
    }
}

try {
    $baselinePath = Join-Path $testRoot "baseline.json"
    $candidatePath = Join-Path $testRoot "candidate.json"
    New-Benchmark -Trials 20 -Count 20 -ColdMedian 100 -QueryMedian 50 |
        ConvertTo-Json -Depth 5 |
        Set-Content -LiteralPath $baselinePath -Encoding utf8
    New-Benchmark -Trials 1 -Count 1 -ColdMedian 150 -QueryMedian 70 |
        ConvertTo-Json -Depth 5 |
        Set-Content -LiteralPath $candidatePath -Encoding utf8

    try {
        & $script `
            -BaselinePath $baselinePath `
            -CandidatePath $candidatePath `
            -OutputDirectory (Join-Path $testRoot "unequal-trials")
        throw "Comparison accepted unequal trial counts."
    } catch {
        if ($_.Exception.Message -notlike "*trial counts differ*") {
            throw "Unexpected unequal-trial rejection: $($_.Exception.Message)"
        }
    }

    New-Benchmark -Trials 20 -Count 19 -ColdMedian 150 -QueryMedian 70 |
        ConvertTo-Json -Depth 5 |
        Set-Content -LiteralPath $candidatePath -Encoding utf8
    try {
        & $script `
            -BaselinePath $baselinePath `
            -CandidatePath $candidatePath `
            -OutputDirectory (Join-Path $testRoot "unequal-samples")
        throw "Comparison accepted unequal metric counts."
    } catch {
        if ($_.Exception.Message -notlike "*sample counts are inconsistent*") {
            throw "Unexpected unequal-sample rejection: $($_.Exception.Message)"
        }
    }

    New-Benchmark -Trials 20 -Count 20 -ColdMedian 150 -QueryMedian 70 |
        ConvertTo-Json -Depth 5 |
        Set-Content -LiteralPath $candidatePath -Encoding utf8
    $regressionOutput = Join-Path $testRoot "regression"
    & $script `
        -BaselinePath $baselinePath `
        -CandidatePath $candidatePath `
        -OutputDirectory $regressionOutput `
        -BaselineLabel "baseline" `
        -CandidateLabel "regression" | Out-Null
    $svg = Get-Content -LiteralPath (Join-Path $regressionOutput "headline-reductions.svg") -Raw
    if ($svg -notlike "*#ef4444*" -or $svg -notlike "*-50.0%*") {
        throw "Regression chart did not render a red negative bar."
    }

    [pscustomobject]@{
        UnequalTrials = "rejected"
        UnequalSamples = "rejected"
        RegressionBar = "red"
    } | ConvertTo-Json
} finally {
    if (Test-Path -LiteralPath $testRoot) {
        [IO.Directory]::Delete($testRoot, $true)
    }
}
