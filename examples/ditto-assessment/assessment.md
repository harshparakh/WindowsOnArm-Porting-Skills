# Windows on Arm Assessment: sabrogden/Ditto

- Generated: 2026-09-14T20:24:35.401593+00:00
- Source mode: github
- Default branch: `master`
- Stars: 7,144
- Latest stable release: `3.25.113.0`

## Decision

High-risk one-week port; constrain scope to architecture proof and one core scenario.

| Score | Value |
|---|---:|
| Customer impact | 10/10 |
| Technical risk | 10/10 |
| One-week feasibility | 1/10 |

## Architecture Gap

- Latest release has x64 asset: False
- Latest release has Windows binary asset: True
- Latest release has ARM64 asset: False
- Source x64 references: 293
- Source Arm references: 220
- Committed native binary candidates: 3

## Build and Packaging

- Build systems: dotnet, native-msbuild, node
- Packaging: inno-setup, github-actions
- Plugin surface: False
- Tests detected: False

## Risks

- **HIGH native-code:** Repository contains native C, C++, Rust, or VCXPROJ build surfaces.
- **HIGH committed-binaries:** 3 committed EXE, DLL, LIB, or Node addon paths require architecture verification.
- **HIGH x64-assumptions:** Detected 293 x64 references versus 220 Arm references.
- **MEDIUM installer-updater:** Installer or updater tooling has architecture-specific behavior.
- **MEDIUM release-gap:** Latest stable release has x64 assets and no ARM64-labeled asset.
- **MEDIUM test-coverage:** No obvious test directory or test project was detected.

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
