# Windows on Arm Porting Skills

A reusable Agent Plugins 1.0 package for assessing, porting, packaging, and verifying Windows applications on ARM64.

It turns one successful app port into a repeatable workflow:

1. **Assess** current source, architecture gaps, dependencies, packaging risk, estimated effort, and fit against an optional delivery window.
2. **Build** architecture-isolated ARM64 or Arm64EC outputs with explicit native-dependency and fallback decisions.
3. **Prove** binary and process architecture, UI behavior, package integrity, and before-and-after performance.

Flow Launcher is the complete proof project. Lively Wallpaper is a positive next-candidate assessment, while Ditto demonstrates that Scout can reject a poor fit before engineering starts.

## Skills

- `woa-scout`: measures repository impact, architecture gaps, dependency and packaging risk, general porting readiness, estimated effort, and fit against an optional delivery window.
- `woa-port`: chooses ARM64 or Arm64EC, isolates build outputs, handles native dependencies and fallbacks, and adds CI and packaging.
- `woa-verify`: proves binary and process architecture, runs UI Automation scenarios, captures benchmarks and traces, and generates evidence reports.

## Install

Clone the repository and install the package in GitHub Copilot CLI:

```powershell
git clone https://github.com/harshparakh/WindowsOnArm-Porting-Skills.git
copilot plugin install .\WindowsOnArm-Porting-Skills
```

Versioned package ZIPs are also attached to the GitHub releases.

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

The repository runs the same package validation on Windows for every push and pull request.

## Repo to Arm workbench preview

The [workbench](workbench/README.md) connects the three skills through a constrained coding agent, hash-bound approvals, disposable Windows builds, and independent package/process evidence. It currently targets SDK-style .NET Windows desktop projects, not arbitrary native or cross-platform repositories. Its setup, trust boundaries, recovery limits, and command-line protocol are documented separately from the three reusable skills.

## Direct script usage

Repository assessment:

```powershell
python .\scripts\woa_scout.py `
  --repo Flow-Launcher/Flow.Launcher `
  --local-path C:\src\Flow.Launcher `
  --delivery-window-days 7 `
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

General desktop UI scenarios use `scripts\Invoke-WoaScenarioBenchmark.ps1` and
`schemas\woa-scenarios.schema.json`. This runner binds execution to an approved
complete package manifest, creates fresh per-trial profiles/fixtures, and measures
native UI Automation sequences. It retains exact assertions, raw samples, CPU and
memory observations, median and p95. Validation-only and smoke runs are explicitly
distinguished from performance evidence; arbitrary shell setup and recording are
not supported.

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
- ARM64-native .NET, WPF, SQLite, Skia, and voidtools Everything SDK modules.
- Signed ARM64 Everything SDK2 and SDK3 wrappers with pinned provenance, matching exports, valid licenses, and live Flow Launcher UI validation.
- Windows Search as the default and runtime fallback when Everything is absent or stopped.
- PE validation across every shipped EXE and DLL.
- Repeatable UI and benchmark evidence on physical ARM64 hardware.
- An open upstream contribution at [Flow-Launcher/Flow.Launcher#4659](https://github.com/Flow-Launcher/Flow.Launcher/pull/4659).

Generated proof reports live under `examples/`.

- `examples/flow-launcher-proof/`: package, architecture, test, device, and benchmark summary.
- `examples/flow-launcher-assessment/`: pre-port Flow Launcher readiness assessment.
- `examples/lively-assessment/`: positive next-candidate assessment with no stable ARM64 package and a 7-28 day effort estimate.
- `examples/ditto-assessment/`: second-target scouting demonstration using the hackathon's seven-day delivery window; no port was started.

## Evidence boundaries

- Benchmark results are same-device, same-profile, same-corpus comparisons with 20 trials per architecture.
- Process-cold means a new process with a restored profile and warm operating-system file cache.
- No battery or energy improvement is claimed.
- Community plugins with in-process native dependencies require their own architecture validation.
- Windows Performance Recorder trace evidence is not included in the proof project.
- Remote Scout results cover the selected repository's stable GitHub release and sampled source. Verify separate nightly, development-build, and related-repository channels before treating a release gap as final.
