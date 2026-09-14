# Windows on Arm Assessment: Flow-Launcher/Flow.Launcher

- Generated: 2026-09-14T20:24:25.003419+00:00
- Source mode: local
- Default branch: `dev`
- Stars: 15,565
- Latest stable release: `v2.1.3`

## Decision

Feasible in one week only with an explicit subsystem or installer fallback.

| Score | Value |
|---|---:|
| Customer impact | 10/10 |
| Technical risk | 7/10 |
| One-week feasibility | 5/10 |

## Architecture Gap

- Latest release has x64 asset: False
- Latest release has Windows binary asset: True
- Latest release has ARM64 asset: False
- Source x64 references: 143
- Source Arm references: 20
- Committed native binary candidates: 2

## Build and Packaging

- Build systems: dotnet
- Packaging: squirrel, velopack, github-actions, appveyor, azure-pipelines
- Plugin surface: True
- Tests detected: True

## Risks

- **MEDIUM committed-binaries:** 2 committed EXE, DLL, LIB, or Node addon paths require architecture verification.
- **HIGH x64-assumptions:** Detected 143 x64 references versus 20 Arm references.
- **MEDIUM installer-updater:** Installer or updater tooling has architecture-specific behavior.
- **MEDIUM plugin-boundary:** Plugin loading requires in-process and out-of-process architecture classification.
- **MEDIUM release-gap:** Latest stable release has x64 assets and no ARM64-labeled asset.

## Committed Outcome

- Architecture-isolated x64 and ARM64 builds
- ARM64 portable artifact
- PE architecture inventory
- Physical-device core scenario validation
- Before-and-after evidence

## Fallback

- Use an operating-system service instead of an unavailable x64-only optional dependency.
- Defer installer or updater integration before reducing native core quality.
- Report incompatible optional plugins instead of loading them in-process.
