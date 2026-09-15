# Windows on Arm Assessment: Flow-Launcher/Flow.Launcher

- Generated: 2026-09-15T18:26:45.759720+00:00
- Source mode: local
- Default branch: `dev`
- Stars: 15,568
- Latest stable release: `v2.1.3`

## Decision

Viable native ARM64 candidate with explicit subsystem, dependency, installer, or plugin fallbacks. The 7 day delivery window overlaps the estimated 7-28 day effort and requires reduced scope or fallbacks.

| Score | Value |
|---|---:|
| Customer impact | 10/10 |
| Technical risk | 7/10 |
| Porting readiness | 5/10 |

## Effort and Delivery Window

- Estimated effort: 7-28 days (substantial)
- Delivery window: 7
- Window fit: conditional
- Rationale: The 7 day delivery window overlaps the estimated 7-28 day effort and requires reduced scope or fallbacks.

## Architecture Gap

- Latest release has x64 asset: False
- Latest release has Windows binary asset: True
- Latest release has ARM64 asset: False
- Source x64 references: 423
- Source Arm references: 60
- Committed native binary candidates: 487

## Build and Packaging

- Build systems: dotnet
- Packaging: squirrel, velopack, github-actions, appveyor, azure-pipelines
- Plugin surface: True
- Tests detected: True

## Risks

- **MEDIUM committed-binaries:** 487 committed EXE, DLL, LIB, or Node addon paths require architecture verification.
- **HIGH x64-assumptions:** Detected 423 x64 references versus 60 Arm references.
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
