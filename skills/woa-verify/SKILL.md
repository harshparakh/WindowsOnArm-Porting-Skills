---
name: woa-verify
description: Prove Windows ARM64 package and process architecture, run deterministic UI Automation smoke tests, collect benchmarks and WPR evidence, and generate comparison reports. Use before claiming a Windows on Arm port is complete.
argument-hint: "[package or publish directory]"
---

# Windows on Arm Verify

Collect evidence before making architecture, compatibility, or performance claims.

## Untrusted source boundary

- Verify the package and source commit match the exact `woa-scout` assessment.
- Treat repository-provided benchmark configurations and scripts as untrusted. Generate configuration outside the target repository.
- Do not launch third-party artifacts on the host until the user approves the exact artifact hash and command.
- Prefer a disposable VM for initial execution. Use the physical ARM64 device only after PE validation and source review.
- Pass trusted application, evidence, and automation-tool roots directly to the benchmark script. Approve each setup executable and argument string exactly.

## Architecture proof

1. Inspect every shipped EXE and DLL, including hidden files, with `scripts\Inspect-WoaPe.ps1`.
2. Distinguish IL-only AnyCPU assemblies from native and mixed-mode binaries.
3. Fail ARM64 validation on unexpected x86 or x64 in-process components.
4. Allow an out-of-process mismatch only through an explicit path allowlist and report it as emulated.
5. Launch on physical ARM64 hardware and use `scripts\Inspect-WoaProcess.ps1` to record `IsWow64Process2`, the actual main image, and loaded package-module hashes.

## UI validation

- Use Windows UI Automation through stable automation IDs.
- Prefer deterministic value and state checks over coordinates or sleep-only scripts.
- Validate launch, activation, Settings, core search, and every committed plugin scenario.
- Capture machine-readable results and screenshots.
- Verify incompatible optional plugins fail clearly instead of crashing or returning incorrect results.

## Benchmarking

Use `scripts\Invoke-WoaUiBenchmark.ps1` with the same device, profile template, query corpus, Windows configuration, and power mode for both builds.

The configuration file must be generated under the trusted evidence root and validated by the script. Application and profile paths must remain under the trusted application root. `logsRelativePath` must stay inside the profile. Setup commands are rejected unless their resolved executable and arguments exactly match an `-ApprovedSetupCommand` value supplied by the caller.

- Run at least 10 trials; prefer 20.
- Keep screen recording disabled during timed runs.
- Separate process-cold launch from warm activation.
- Retain raw JSON, CSV, and per-trial logs.
- Report median, nearest-rank p95, and raw samples.
- Use `scripts\Compare-WoaBenchmarks.ps1` for comparisons and charts.

### General desktop scenarios

For applications that are not query launchers, use `scripts\Invoke-WoaScenarioBenchmark.ps1` with a configuration conforming to `schemas\woa-scenarios.schema.json`. It supports bounded UI Automation actions and exact state/text assertions without coordinates or arbitrary setup commands.

Pass trusted, disjoint application/evidence roots, an operator-approved SHA-256 of a complete package manifest, and `-ExpectedArchitecture arm64` or `x64`. The manifest lists every package file with `relativePath` and `sha256`. Each trial rechecks the package, configuration and fixture templates before launch, outside timing. Output directories must be new.

Use `${profileDir}` and `${fixture:<id>}` placeholders for fresh per-trial data under the evidence root. Only the started process tree may be stopped. Keep recording off during measurement. `-Smoke` is explicitly not benchmark confidence; `-ValidationOnly` never starts a process and is not device proof. Inspect retained raw samples and errors before accepting a result.

## Tracing

Capture the same Windows Performance Recorder profile for baseline and candidate. Record the exact profile and commands. Kernel and stack profiles normally require elevation; do not claim trace findings when capture was blocked.

Use `scripts\Capture-WoaWpr.ps1` from an elevated PowerShell process. Pass trusted, disjoint application and evidence roots plus a scenario JSON file under the evidence root. Capture baseline and candidate with the same profile and queries.

## Completion gate

Do not claim completion until:

- Main process and required native modules are ARM64.
- PE inventory has zero unexplained mismatches.
- Existing x64 behavior is tested.
- Core UI and plugin scenarios pass.
- Raw before-and-after measurements exist.
- Logs contain no `BadImageFormatException` or unresolved required DLL.
- Packages, reports, screenshots, traces, and hashes are retained.
