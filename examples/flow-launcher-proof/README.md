# Flow Launcher ARM64 Proof

**Package source commit:** `5656eb19cd5a9deef267f5befe4ddb6cd8bb1feb`
**Benchmark source commit:** `cd253079cb97afdab016f267a2f8c03d2a6a070b`
**Device:** Windows 11 build 26688 on Snapdragon X1E80100 ARM64
**Sample count:** 20 trials per architecture
**Benchmark method:** Same device, profile template, query corpus, Windows Search scope, power mode, and UI Automation harness. Screen recording was disabled during timed runs.

## Outputs

| Output | Result |
|---|---|
| ARM64 portable ZIP | 12 bundled plugins, 179,013,557 bytes, SHA-256 `77ec4f3499288fdf0ad371b2f3df12f4a66f205470c7a66b3abe30d467da7d0d` |
| x64 portable ZIP | 12 bundled plugins, 184,457,346 bytes, SHA-256 `c642103fc279d1e9308b07dc923d8edbfcb1259c3bc16d6017caf729d9bfb329` |
| ARM64 PE inventory | 474 files, 0 mismatches: 296 AnyCPU, 153 managed ARM64, 25 native ARM64 |
| x64 PE inventory | 475 files, 0 mismatches: 296 AnyCPU, 153 managed x64, 26 native x64 |
| Existing tests | 466 passed, 0 failed |
| Native UI scenarios | 11 query scenarios, Settings, warm activation, global activation, and native Everything file search passed |
| Required native modules | ARM64 `Flow.Launcher.exe`, `coreclr.dll`, `PresentationFramework.dll`, `e_sqlite3.dll`, `libSkiaSharp.dll`, and `Everything3.dll` loaded |

## Same-commit median comparison

| Metric | x64 emulated | ARM64 native | Reduction |
|---|---:|---:|---:|
| Process-cold launch | 4,900.47 ms | 2,429.89 ms | 50.42% |
| Warm activation | 538.29 ms | 289.18 ms | 46.28% |
| Settings open | 1,945.28 ms | 1,168.32 ms | 39.94% |
| Process CPU | 17.23 s | 8.20 s | 52.43% |
| Working set | 638.2 MiB | 540.0 MiB | 15.39% |
| Program query | 855.61 ms | 326.16 ms | 61.88% |
| Windows Search query | 332.80 ms | 258.25 ms | 22.40% |
| BrowserBookmark query | 188.80 ms | 125.17 ms | 33.70% |

Positive reduction means lower latency, memory, or CPU. These results demonstrate correlation under a controlled same-device test; they do not claim battery-life improvement.

## Native Everything and fallback

The ARM64 package includes voidtools-signed SDK2 and SDK3 wrappers. Their signatures, PE architecture, exported APIs, licenses, standalone IPC, and actual Flow Launcher result path were validated on the ARM64 device. Windows Search remains the default and the runtime fallback when Everything is absent or stopped.

## Limitations

- Process-cold launch uses a restored profile and warm operating-system file cache; it is not reboot-cold.
- Settings-open measurement includes the stable UI Automation command path used for both architectures.
- Windows Performance Recorder kernel traces require an elevated session and are tracked separately.
- The development-signed MSIX was generated, but installation requires machine certificate trust.
- The benchmark corpus retains Windows Search as the Explorer scenario; native Everything behavior was validated separately and is not presented as an Everything performance comparison.

## Upstream contribution

- Pull request: [Flow-Launcher/Flow.Launcher#4659](https://github.com/Flow-Launcher/Flow.Launcher/pull/4659)
- Current package proof commit: `5656eb19cd5a9deef267f5befe4ddb6cd8bb1feb`

## Related examples

- `../flow-launcher-assessment/assessment.md`
- `../ditto-assessment/assessment.md`
