# Windows on Arm Assessment: rocksdanister/lively

- Generated: 2026-09-15T21:35:30.682129+00:00
- Source mode: github
- Default branch: `core-separation`
- Stars: 19,619
- Latest stable release: `v2.2.1.0`

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

- Latest release has x64 asset: False
- Latest release has Windows binary asset: True
- Latest release has ARM64 asset: False
- Source x64 references: 154
- Source Arm references: 85
- Committed native binary candidates: 0

## Build and Packaging

- Build systems: dotnet
- Packaging: msix, inno-setup
- Plugin surface: True
- Tests detected: False

## Risks

- **HIGH x64-assumptions:** Detected 154 x64 references versus 85 Arm references.
- **MEDIUM installer-updater:** Installer or updater tooling has architecture-specific behavior.
- **MEDIUM plugin-boundary:** Plugin loading requires in-process and out-of-process architecture classification.
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
