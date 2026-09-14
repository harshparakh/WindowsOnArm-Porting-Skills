# Flow Launcher ARM64 Proof

**Source commit:** `f80c84078eaa4ef1245405a20dcac81b37278947`
**Device:** Windows 11 build 26688 on Snapdragon X1E80100 ARM64
**Sample count:** 20 trials per architecture
**Benchmark method:** Same device, profile template, query corpus, Windows Search scope, power mode, and UI Automation harness. Screen recording was disabled during timed runs.

## Outputs

| Output | Result |
|---|---|
| ARM64 portable ZIP | 12 bundled plugins, 178,924,954 bytes, SHA-256 `515e747cdf6a2acf37da0d6ec9a925dde3e1ad13992e4c4fd6580f9957b055fc` |
| x64 portable ZIP | 12 bundled plugins, 184,455,241 bytes, SHA-256 `41fa2b82b8e5d8626bc151cc65a68872ea51ad428fe86e038b3e100a767eb796` |
| ARM64 PE inventory | 472 files, 0 mismatches: 299 AnyCPU, 150 managed ARM64, 23 native ARM64 |
| x64 PE inventory | 475 files, 0 mismatches: 299 AnyCPU, 150 managed x64, 26 native x64 |
| Existing tests | 462 passed, 0 failed |
| Native UI scenarios | 11 bundled-plugin scenarios, Settings, and global activation passed |
| Required native modules | ARM64 `Flow.Launcher.exe`, `coreclr.dll`, `PresentationFramework.dll`, `e_sqlite3.dll`, and `libSkiaSharp.dll` loaded |

## Same-commit median comparison

| Metric | x64 emulated | ARM64 native | Reduction |
|---|---:|---:|---:|
| Process-cold launch | 4,920.09 ms | 2,407.23 ms | 51.07% |
| Warm activation | 527.33 ms | 246.62 ms | 53.23% |
| Settings open | 1,830.86 ms | 1,029.54 ms | 43.77% |
| Process CPU | 17.23 s | 8.01 s | 53.54% |
| Working set | 782.9 MiB | 675.8 MiB | 13.68% |
| Program query | 780.53 ms | 312.60 ms | 59.95% |
| Windows Search query | 265.04 ms | 152.38 ms | 42.51% |
| BrowserBookmark query | 149.68 ms | 116.93 ms | 21.88% |

Positive reduction means lower latency, memory, or CPU. These results demonstrate correlation under a controlled same-device test; they do not claim battery-life improvement.

## Fallback

The bundled Everything SDK is x64-only and was not included in the ARM64 package. Explorer uses Windows Search by default. Selecting Everything on ARM64 returns a clear Windows Search fallback instead of loading an incompatible DLL.

## Limitations

- Process-cold launch uses a restored profile and warm operating-system file cache; it is not reboot-cold.
- Settings-open measurement includes the stable UI Automation command path used for both architectures.
- Windows Performance Recorder kernel traces require an elevated session and are tracked separately.
- The development-signed MSIX was generated, but installation requires machine certificate trust.

## Related examples

- `../flow-launcher-assessment/assessment.md`
- `../ditto-assessment/assessment.md`
