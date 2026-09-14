# Windows on Arm Porting Skills

An Agent Plugins 1.0 package for assessing, porting, packaging, and verifying Windows applications on ARM64.

## Skills

- `woa-scout`: measures repository impact, architecture gaps, dependency and packaging risk, and one-week feasibility.
- `woa-port`: chooses ARM64 or Arm64EC, isolates build outputs, handles native dependencies and fallbacks, and adds CI and packaging.
- `woa-verify`: proves binary and process architecture, runs UI Automation scenarios, captures benchmarks and traces, and generates evidence reports.

## Install

Install the local package in GitHub Copilot CLI:

```powershell
copilot plugin install C:\path\to\WindowsOnArm-Porting-Skills
```

The package follows Agent Plugins 1.0:

```text
plugin.json
skills/
  woa-scout/SKILL.md
  woa-port/SKILL.md
  woa-verify/SKILL.md
scripts/
schemas/
tests/
examples/
```

## Validate

```powershell
pwsh -File .\tests\Test-PluginPackage.ps1
python -m unittest discover -s .\tests -p "test_*.py"
```

## Direct script usage

Repository assessment:

```powershell
python .\scripts\woa_scout.py `
  --repo Flow-Launcher/Flow.Launcher `
  --local-path C:\src\Flow.Launcher `
  --output C:\artifacts\flow-assessment
```

PE architecture inventory:

```powershell
pwsh -File .\scripts\Inspect-WoaPe.ps1 `
  -RootPath C:\artifacts\publish\win-arm64 `
  -ExpectedArchitecture arm64 `
  -ReportPath C:\artifacts\architecture.json
```

UI benchmark:

```powershell
pwsh -File .\scripts\Invoke-WoaUiBenchmark.ps1 `
  -ConfigurationPath C:\artifacts\benchmark-config.json `
  -OutputDirectory C:\artifacts\benchmark `
  -ArchitectureLabel arm64-native `
  -ApplicationRoot C:\artifacts\application `
  -EvidenceRoot C:\artifacts\evidence `
  -AutomationToolRoot C:\tools\winapp `
  -ApprovedSetupCommand "C:\Windows\System32\PING.EXE|127.0.0.1 -t" `
  -Trials 20
```

Benchmark comparison:

```powershell
pwsh -File .\scripts\Compare-WoaBenchmarks.ps1 `
  -BaselinePath C:\artifacts\x64\benchmark-results.json `
  -CandidatePath C:\artifacts\arm64\benchmark-results.json `
  -OutputDirectory C:\artifacts\comparison `
  -BaselineLabel "x64 emulated" `
  -CandidateLabel "ARM64 native"
```

Elevated Windows Performance Recorder capture:

```powershell
pwsh -File .\scripts\Capture-WoaWpr.ps1 `
  -ApplicationPath C:\artifacts\application\Flow.Launcher.exe `
  -WorkingDirectory C:\artifacts\application `
  -ApplicationRoot C:\artifacts\application `
  -EvidenceRoot C:\evidence `
  -ProfileTemplatePath C:\evidence\profile-template `
  -ProfilePath C:\artifacts\application\UserData `
  -ScenarioPath C:\evidence\trace-queries.json `
  -TracePath C:\evidence\flow-arm64.etl
```

## Evidence policy

- Keep raw samples, logs, traces, screenshots, and package inventories.
- Report sample count, median, p95, and raw values.
- Use same-device, same-profile, same-corpus comparisons.
- Label process-cold measurements separately from reboot or disk-cold measurements.
- Do not claim battery or energy improvement without a controlled power experiment.
- Record unsupported plugins and fallback behavior explicitly.

## Flow Launcher proof project

The proof project demonstrates:

- Current-source x64 and ARM64 portable ZIPs.
- ARM64-native .NET, WPF, SQLite, and Skia modules.
- Windows Search fallback when no authoritative ARM64 Everything SDK is bundled.
- PE validation across every shipped EXE and DLL.
- Repeatable UI and benchmark evidence on physical ARM64 hardware.

Generated proof reports live under `examples/`.

- `examples/flow-launcher-proof/`: package, architecture, test, device, and benchmark summary.
- `examples/flow-launcher-assessment/`: pre-port Flow Launcher readiness assessment.
- `examples/ditto-assessment/`: second-repository scouting demonstration; no port was started.
