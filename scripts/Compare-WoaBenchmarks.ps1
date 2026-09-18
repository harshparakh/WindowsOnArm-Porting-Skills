[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$BaselinePath,

    [Parameter(Mandatory)]
    [string]$CandidatePath,

    [Parameter(Mandatory)]
    [string]$OutputDirectory,

    [string]$BaselineLabel = "Baseline",

    [string]$CandidateLabel = "Candidate"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-MetricLabel {
    param([Parameter(Mandatory)][string]$Metric)

    $labels = @{
        "process-cold-window-ms" = "Process-cold launch"
        "process-cold-launch-ms" = "Process-cold launch and ready"
        "initial-folder-analysis-ms" = "Initial folder analysis"
        "scenario-settings-open-ms" = "Settings navigation"
        "scenario-home-return-ms" = "Return Home"
        "scenario-compress-fixture-ms" = "Fixture compression"
        "scenario-decompress-fixture-ms" = "Fixture decompression"
        "scenario-settings-open-cpu-ms" = "Settings navigation CPU"
        "scenario-home-return-cpu-ms" = "Return Home CPU"
        "scenario-compress-fixture-cpu-ms" = "Fixture compression CPU"
        "scenario-decompress-fixture-cpu-ms" = "Fixture decompression CPU"
        "warm-activation-ms" = "Warm activation"
        "settings-open-ms" = "Settings open"
        "working-set-bytes" = "Working set"
        "private-memory-bytes" = "Private memory"
        "process-cpu-ms" = "Process CPU"
        "query-program-notepad-ms" = "Program"
        "query-windows-search-file-ms" = "Windows Search"
        "query-browser-bookmark-ms" = "BrowserBookmark"
        "query-calculator-ms" = "Calculator"
        "query-shell-ms" = "Shell"
        "query-sys-ms" = "Sys"
        "query-url-ms" = "URL"
        "query-web-search-ms" = "WebSearch"
        "query-process-killer-ms" = "ProcessKiller"
        "query-plugins-manager-ms" = "PluginsManager"
        "query-windows-settings-ms" = "WindowsSettings"
    }
    if ($labels.ContainsKey($Metric)) {
        return $labels[$Metric]
    }
    return $Metric
}

function Format-MetricValue {
    param(
        [Parameter(Mandatory)][string]$Metric,
        [Parameter(Mandatory)][double]$Value
    )

    if ($Metric -like "*-bytes") {
        return "{0:N1} MiB" -f ($Value / 1MB)
    }
    if ($Metric -eq "process-cpu-ms") {
        return "{0:N2} s" -f ($Value / 1000)
    }
    return "{0:N2} ms" -f $Value
}

function Escape-Xml {
    param([Parameter(Mandatory)][string]$Value)

    return [Security.SecurityElement]::Escape($Value)
}

function Write-QueryChart {
    param(
        [Parameter(Mandatory)][object[]]$Rows,
        [Parameter(Mandatory)][string]$Path
    )

    $queryRows = @($Rows | Where-Object { $_.Metric -like "query-*" })
    $chartTitle = "Median query latency"
    if ($queryRows.Count -eq 0) {
        $queryRows = @($Rows | Where-Object { $_.Metric -like "scenario-*-ms" -and $_.Metric -notlike "*-cpu-ms" })
        $chartTitle = "Median desktop scenario latency"
    }
    if ($queryRows.Count -eq 0) {
        return
    }
    $width = 1200
    $left = 220
    $right = 170
    $top = 70
    $rowHeight = 54
    $height = $top + ($queryRows.Count * $rowHeight) + 70
    $plotWidth = $width - $left - $right
    $maximum = ($queryRows | ForEach-Object {
        [math]::Max($_.BaselineMedian, $_.CandidateMedian)
    } | Measure-Object -Maximum).Maximum
    $scale = if ($maximum -gt 0) { $plotWidth / $maximum } else { 1 }

    $svg = [Text.StringBuilder]::new()
    [void]$svg.AppendLine("<svg xmlns=`"http://www.w3.org/2000/svg`" width=`"$width`" height=`"$height`" viewBox=`"0 0 $width $height`">")
    [void]$svg.AppendLine("<rect width=`"100%`" height=`"100%`" fill=`"#0f172a`"/>")
    [void]$svg.AppendLine("<text x=`"40`" y=`"38`" fill=`"#f8fafc`" font-family=`"Segoe UI,Arial`" font-size=`"24`" font-weight=`"600`">$chartTitle</text>")
    [void]$svg.AppendLine("<rect x=`"$($width - 310)`" y=`"20`" width=`"16`" height=`"16`" fill=`"#94a3b8`"/><text x=`"$($width - 286)`" y=`"34`" fill=`"#cbd5e1`" font-family=`"Segoe UI,Arial`" font-size=`"14`">$(Escape-Xml $BaselineLabel)</text>")
    [void]$svg.AppendLine("<rect x=`"$($width - 155)`" y=`"20`" width=`"16`" height=`"16`" fill=`"#22c55e`"/><text x=`"$($width - 131)`" y=`"34`" fill=`"#cbd5e1`" font-family=`"Segoe UI,Arial`" font-size=`"14`">$(Escape-Xml $CandidateLabel)</text>")

    for ($index = 0; $index -lt $queryRows.Count; $index++) {
        $row = $queryRows[$index]
        $y = $top + ($index * $rowHeight)
        $baselineWidth = [math]::Round($row.BaselineMedian * $scale, 2)
        $candidateWidth = [math]::Round($row.CandidateMedian * $scale, 2)
        [void]$svg.AppendLine("<text x=`"20`" y=`"$($y + 24)`" fill=`"#e2e8f0`" font-family=`"Segoe UI,Arial`" font-size=`"15`">$(Escape-Xml $row.Label)</text>")
        [void]$svg.AppendLine("<rect x=`"$left`" y=`"$y`" width=`"$baselineWidth`" height=`"16`" rx=`"3`" fill=`"#94a3b8`"/>")
        [void]$svg.AppendLine("<rect x=`"$left`" y=`"$($y + 22)`" width=`"$candidateWidth`" height=`"16`" rx=`"3`" fill=`"#22c55e`"/>")
        [void]$svg.AppendLine("<text x=`"$($left + $baselineWidth + 8)`" y=`"$($y + 13)`" fill=`"#cbd5e1`" font-family=`"Consolas,monospace`" font-size=`"13`">$("{0:N1}" -f $row.BaselineMedian) ms</text>")
        [void]$svg.AppendLine("<text x=`"$($left + $candidateWidth + 8)`" y=`"$($y + 35)`" fill=`"#86efac`" font-family=`"Consolas,monospace`" font-size=`"13`">$("{0:N1}" -f $row.CandidateMedian) ms</text>")
    }

    [void]$svg.AppendLine("</svg>")
    $svg.ToString() | Set-Content -LiteralPath $Path -Encoding utf8
}

function Write-ReductionChart {
    param(
        [Parameter(Mandatory)][object[]]$Rows,
        [Parameter(Mandatory)][string]$Path
    )

    $selectedMetrics = @(
        "process-cold-window-ms",
        "process-cold-launch-ms",
        "initial-folder-analysis-ms",
        "scenario-settings-open-ms",
        "scenario-home-return-ms",
        "scenario-compress-fixture-ms",
        "scenario-decompress-fixture-ms",
        "warm-activation-ms",
        "settings-open-ms",
        "working-set-bytes",
        "private-memory-bytes",
        "process-cpu-ms"
    )
    $chartRows = @($Rows | Where-Object { $selectedMetrics -contains $_.Metric })
    $width = 1000
    $left = 210
    $right = 110
    $top = 70
    $rowHeight = 52
    $height = $top + ($chartRows.Count * $rowHeight) + 70
    $plotWidth = $width - $left - $right
    $minimum = [math]::Min(0, ($chartRows.MedianReductionPercent | Measure-Object -Minimum).Minimum)
    $maximum = [math]::Max(0, ($chartRows.MedianReductionPercent | Measure-Object -Maximum).Maximum)
    if ($minimum -eq $maximum) {
        $minimum = -1
        $maximum = 1
    }
    $scale = $plotWidth / ($maximum - $minimum)
    $zeroX = $left + ((0 - $minimum) * $scale)

    $svg = [Text.StringBuilder]::new()
    [void]$svg.AppendLine("<svg xmlns=`"http://www.w3.org/2000/svg`" width=`"$width`" height=`"$height`" viewBox=`"0 0 $width $height`">")
    [void]$svg.AppendLine("<rect width=`"100%`" height=`"100%`" fill=`"#0f172a`"/>")
    [void]$svg.AppendLine("<text x=`"40`" y=`"38`" fill=`"#f8fafc`" font-family=`"Segoe UI,Arial`" font-size=`"24`" font-weight=`"600`">Median change versus $(Escape-Xml $BaselineLabel)</text>")
    [void]$svg.AppendLine("<line x1=`"$zeroX`" y1=`"$($top - 10)`" x2=`"$zeroX`" y2=`"$($height - 45)`" stroke=`"#64748b`" stroke-width=`"2`"/>")

    for ($index = 0; $index -lt $chartRows.Count; $index++) {
        $row = $chartRows[$index]
        $y = $top + ($index * $rowHeight)
        $value = [double]$row.MedianReductionPercent
        $barWidth = [math]::Round([math]::Abs($value) * $scale, 2)
        $barX = if ($value -ge 0) { $zeroX } else { $zeroX - $barWidth }
        $barColor = if ($value -ge 0) { "#22c55e" } else { "#ef4444" }
        $textColor = if ($value -ge 0) { "#86efac" } else { "#fca5a5" }
        $textX = if ($value -ge 0) { $barX + $barWidth + 10 } else { $barX - 10 }
        $textAnchor = if ($value -ge 0) { "start" } else { "end" }
        [void]$svg.AppendLine("<text x=`"20`" y=`"$($y + 23)`" fill=`"#e2e8f0`" font-family=`"Segoe UI,Arial`" font-size=`"15`">$(Escape-Xml $row.Label)</text>")
        [void]$svg.AppendLine("<rect x=`"$barX`" y=`"$($y + 5)`" width=`"$barWidth`" height=`"24`" rx=`"4`" fill=`"$barColor`"/>")
        [void]$svg.AppendLine("<text x=`"$textX`" y=`"$($y + 23)`" text-anchor=`"$textAnchor`" fill=`"$textColor`" font-family=`"Consolas,monospace`" font-size=`"14`">$("{0:N1}" -f $value)%</text>")
    }

    [void]$svg.AppendLine("</svg>")
    $svg.ToString() | Set-Content -LiteralPath $Path -Encoding utf8
}

$baseline = Get-Content -LiteralPath $BaselinePath -Raw | ConvertFrom-Json
$candidate = Get-Content -LiteralPath $CandidatePath -Raw | ConvertFrom-Json
if ([int]$baseline.Trials -ne [int]$candidate.Trials) {
    throw "Benchmark trial counts differ: $($baseline.Trials) versus $($candidate.Trials)."
}
$baselineMetrics = @{}
$candidateMetrics = @{}
foreach ($summary in @($baseline.Summaries)) {
    $baselineMetrics[[string]$summary.Metric] = $summary
}
foreach ($summary in @($candidate.Summaries)) {
    $candidateMetrics[[string]$summary.Metric] = $summary
}
$baselineMetricNames = @($baselineMetrics.Keys | Sort-Object)
$candidateMetricNames = @($candidateMetrics.Keys | Sort-Object)
if (($baselineMetricNames -join "`n") -ne ($candidateMetricNames -join "`n")) {
    throw "Benchmark metric sets differ."
}

$rows = @()
foreach ($metric in $baselineMetricNames) {
    $baselineSummary = $baselineMetrics[$metric]
    $candidateSummary = $candidateMetrics[$metric]
    if ([int]$baselineSummary.Count -ne [int]$candidateSummary.Count -or
        [int]$baselineSummary.Count -ne [int]$baseline.Trials) {
        throw "Metric '$metric' sample counts are inconsistent: $($baselineSummary.Count) versus $($candidateSummary.Count), expected $($baseline.Trials)."
    }
    $medianReduction = if ([double]$baselineSummary.Median -ne 0) {
        (([double]$baselineSummary.Median - [double]$candidateSummary.Median) /
            [double]$baselineSummary.Median) * 100
    } else {
        0
    }
    $p95Reduction = if ([double]$baselineSummary.P95 -ne 0) {
        (([double]$baselineSummary.P95 - [double]$candidateSummary.P95) /
            [double]$baselineSummary.P95) * 100
    } else {
        0
    }
    $rows += [pscustomobject]@{
        Metric = $metric
        Label = Get-MetricLabel $metric
        BaselineCount = [int]$baselineSummary.Count
        CandidateCount = [int]$candidateSummary.Count
        BaselineMedian = [double]$baselineSummary.Median
        CandidateMedian = [double]$candidateSummary.Median
        MedianReductionPercent = [math]::Round($medianReduction, 2)
        BaselineP95 = [double]$baselineSummary.P95
        CandidateP95 = [double]$candidateSummary.P95
        P95ReductionPercent = [math]::Round($p95Reduction, 2)
    }
}

$output = [IO.Path]::GetFullPath($OutputDirectory)
[void][IO.Directory]::CreateDirectory($output)
$comparison = [pscustomobject]@{
    SchemaVersion = 1
    GeneratedAtEt = [DateTimeOffset]::Now.ToString("o")
    BaselineLabel = $BaselineLabel
    CandidateLabel = $CandidateLabel
    BaselinePath = [IO.Path]::GetFullPath($BaselinePath)
    CandidatePath = [IO.Path]::GetFullPath($CandidatePath)
    BaselineTrials = [int]$baseline.Trials
    CandidateTrials = [int]$candidate.Trials
    Rows = $rows
}
$comparison | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $output "comparison.json") -Encoding utf8
$rows | Export-Csv -LiteralPath (Join-Path $output "comparison.csv") -NoTypeInformation -Encoding utf8

$markdown = [Text.StringBuilder]::new()
[void]$markdown.AppendLine("# Windows on Arm Benchmark Comparison")
[void]$markdown.AppendLine()
[void]$markdown.AppendLine("- Baseline: $BaselineLabel, $($baseline.Trials) trials")
[void]$markdown.AppendLine("- Candidate: $CandidateLabel, $($candidate.Trials) trials")
[void]$markdown.AppendLine("- Reduction is calculated as `(baseline - candidate) / baseline`; positive values mean lower latency, memory, or CPU.")
[void]$markdown.AppendLine()
[void]$markdown.AppendLine("| Metric | Baseline n | Candidate n | Baseline median | Candidate median | Median reduction | Baseline p95 | Candidate p95 | P95 reduction |")
[void]$markdown.AppendLine("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
foreach ($row in $rows) {
    [void]$markdown.AppendLine(
        "| $($row.Label) | $($row.BaselineCount) | $($row.CandidateCount) | $(Format-MetricValue $row.Metric $row.BaselineMedian) | $(Format-MetricValue $row.Metric $row.CandidateMedian) | $($row.MedianReductionPercent)% | $(Format-MetricValue $row.Metric $row.BaselineP95) | $(Format-MetricValue $row.Metric $row.CandidateP95) | $($row.P95ReductionPercent)% |")
}
$markdown.ToString() | Set-Content -LiteralPath (Join-Path $output "comparison.md") -Encoding utf8

Write-QueryChart -Rows $rows -Path (Join-Path $output "query-latency.svg")
Write-ReductionChart -Rows $rows -Path (Join-Path $output "headline-reductions.svg")

$comparison | Select-Object BaselineLabel, CandidateLabel, BaselineTrials, CandidateTrials | ConvertTo-Json
