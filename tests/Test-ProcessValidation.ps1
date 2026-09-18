[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$output = Join-Path ([IO.Path]::GetTempPath()) ("woa-process-tests-" + [Guid]::NewGuid().ToString("N"))
[void][IO.Directory]::CreateDirectory($output)
$image = (Get-Process -Id $PID).MainModule.FileName
$architecture = [Runtime.InteropServices.RuntimeInformation]::ProcessArchitecture.ToString()
$expected = if ($architecture -eq "Arm64") { "arm64" } elseif ($architecture -eq "X64") { "x64" } else { throw "Unsupported test process: $architecture" }
$inspector = Join-Path $root "scripts\Inspect-WoaProcess.ps1"
$reportPath = Join-Path $output "process.json"
& $inspector -ProcessId $PID -ExpectedExecutable $image -ApplicationRoot $PSHOME -ExpectedArchitecture $expected -ReportPath $reportPath | Out-Null
$report = Get-Content -LiteralPath $reportPath -Raw | ConvertFrom-Json
if (-not $report.passed -or $report.processId -ne $PID -or $report.executable -ne $image) {
    throw "The process inspector did not report the actual test process."
}
if ($report.architectureApi -ne "GetProcessInformation(ProcessMachineTypeInfo)" -or
    $report.processArchitecture -ne $report.imageArchitecture) {
    throw "Process architecture must use the process-information API and match the executable image."
}
$runtime = @($report.modules | Where-Object { $_.name -eq "coreclr.dll" -and $_.packageRelativePath })
if ($runtime.Count -ne 1) {
    throw "Expected one loaded package runtime module."
}
if ($runtime[0].sha256 -ne (Get-FileHash -LiteralPath $runtime[0].path -Algorithm SHA256).Hash.ToLowerInvariant()) {
    throw "The inspected runtime module hash is incorrect."
}
$rejected = $false
try {
    & $inspector -ProcessId $PID -ExpectedExecutable $image -ApplicationRoot $PSHOME -ExpectedArchitecture $expected -RequiredPackageModule "missing-test-module.dll" -ReportPath (Join-Path $output "rejected.json") | Out-Null
} catch {
    $rejected = $_.Exception.Message -like "*missing-test-module.dll*"
}
if (-not $rejected) {
    throw "The process inspector accepted a missing required runtime module."
}
[pscustomobject]@{ ProcessArchitecture = $report.processArchitecture; Passed = 2; Output = $output } | ConvertTo-Json
