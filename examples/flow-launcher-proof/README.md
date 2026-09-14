# Flow Launcher ARM64 Proof

**Source commit:** `ccba7e62e351eea6538a0b57aaec3f76c209083c`
**Device:** Windows 11 build 26688 on Snapdragon X1E80100 ARM64
**Sample count:** 20 trials per architecture
**Benchmark method:** Same device, profile template, query corpus, Windows Search scope, power mode, and UI Automation harness. Screen recording was disabled during timed runs.

## Outputs

| Output | Result |
|---|---|
| ARM64 portable ZIP | 12 bundled plugins, 178,924,819 bytes, SHA-256 `5f0a8d31c2204aef848014aea9681891e110be7051c6936934c1c77a75212e1a` |
| x64 portable ZIP | 12 bundled plugins, 184,455,079 bytes, SHA-256 `b071b2ad4cef09eeec6751e9ded57a1d22038f32c7e168402038d9f08748cc90` |
| ARM64 PE inventory | 472 files, 0 mismatches: 296 AnyCPU, 153 managed ARM64, 23 native ARM64 |
| x64 PE inventory | 475 files, 0 mismatches: 296 AnyCPU, 153 managed x64, 26 native x64 |
| Existing tests | 463 passed, 0 failed |
| Native UI scenarios | 11 bundled-plugin scenarios, Settings, and global activation passed |
| Required native modules | ARM64 `Flow.Launcher.exe`, `coreclr.dll`, `PresentationFramework.dll`, `e_sqlite3.dll`, and `libSkiaSharp.dll` loaded |

## Same-commit median comparison

| Metric | x64 emulated | ARM64 native | Reduction |
|---|---:|---:|---:|
| Process-cold launch | 4,200.66 ms | 2,167.12 ms | 48.41% |
| Warm activation | 468.69 ms | 236.22 ms | 49.60% |
| Settings open | 1,711.49 ms | 1,006.17 ms | 41.21% |
| Process CPU | 14.59 s | 7.49 s | 48.66% |
| Working set | 805.5 MiB | 707.5 MiB | 12.17% |
| Program query | 692.69 ms | 303.92 ms | 56.13% |
| Windows Search query | 239.39 ms | 137.59 ms | 42.52% |
| BrowserBookmark query | 133.87 ms | 106.51 ms | 20.44% |

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
