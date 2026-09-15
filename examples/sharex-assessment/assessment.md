# Windows on Arm Assessment: ShareX/ShareX

- Generated: 2026-09-15T21:18:23.521952+00:00
- Source mode: github
- Default branch: `develop`
- Stars: 39,584
- Latest stable release: `v21.0.0`

## Decision

Viable native ARM64 candidate with explicit subsystem, dependency, installer, or plugin fallbacks.

| Score | Value |
|---|---:|
| Customer impact | 10/10 |
| Technical risk | 7/10 |
| Porting readiness | 5/10 |

## Effort and Delivery Window

- Estimated effort: 7-28 days (substantial)
- Delivery window: not supplied
- Window fit: not-assessed
- Rationale: No delivery window was supplied; readiness and effort are reported independently.

## Architecture Gap

- Latest release has x64 asset: True
- Latest release has Windows binary asset: True
- Latest release has ARM64 asset: False
- Source x64 references: 398
- Source Arm references: 344
- Committed native binary candidates: 1

## Build and Packaging

- Build systems: dotnet
- Packaging: squirrel, inno-setup, github-actions
- Plugin surface: False
- Tests detected: False

## Risks

- **MEDIUM committed-binaries:** 1 committed EXE, DLL, LIB, or Node addon paths require architecture verification.
- **HIGH x64-assumptions:** Detected 398 x64 references versus 344 Arm references.
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
