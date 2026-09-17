# Repo to Arm UI

Native WPF frontend for `workbench.cli`, targeting .NET 9. The UI invokes the configured Python executable, not candidate source or a shell. It has no third-party UI dependencies.

## Build and publish

Run from this directory with an installed .NET 9 SDK. Restore from the operator's approved NuGet configuration; no private feed is required by this project.

```powershell
$dotnet = (Get-Command dotnet -CommandType Application).Source
& $dotnet restore .\RepoToArm.Workbench.csproj
& $dotnet publish .\RepoToArm.Workbench.csproj -c Release -r win-arm64 --self-contained true --no-restore -p:PublishProfile=win-arm64
& $dotnet publish .\RepoToArm.Workbench.csproj -c Release -r win-x64 --self-contained true --no-restore -p:PublishProfile=win-x64
```

`Directory.Build.props` routes packages, intermediate files, binaries and publish output under `%LOCALAPPDATA%\AgencyCowork\artifacts\RepoToArm\workbench-ui`. The SDK's artifacts layout separates configuration/runtime intermediates and binaries, and each runtime has a separate publish directory. Distribute the entire self-contained directory, not just `RepoToArm.exe`.

## Launch

Use full local paths for the trusted Python executable, toolkit repository root, and runs directory:

```powershell
$app = Join-Path $env:LOCALAPPDATA 'AgencyCowork\artifacts\RepoToArm\workbench-ui\publish\win-arm64\RepoToArm.exe'
& $app --python 'C:\TrustedTools\Python\python.exe' --backend-root 'C:\Source\WindowsOnArm-Porting-Skills' --runs-root 'C:\RepoToArm\Runs'
```

The paths above are placeholders. The same three settings are editable in **Local configuration**. Configuration and the last run ID are saved to `%LOCALAPPDATA%\AgencyCowork\RepoToArm\ui-settings.json`. Unknown configuration keys are preserved. OneDrive, network, and junction/symlink paths are rejected.

Optional `--repo`, `--commit`, `--project`, and `--scope` launch arguments seed the source request. The **Pinned source and scope** panel exposes the same controls. A nonempty commit must be the full immutable SHA; the project accepts SDK-style `.csproj` or `.vbproj` paths. These values are passed as separate arguments, not a shell command.

Reopening the application refreshes the last run using `status`; no mutation is automatic. Switching backend or runs roots clears the selected run. Selecting another run ID requires a refresh before mutation buttons become available.

## Backend boundary

`BackendClient.CreateStartInfo` is the single action-to-CLI mapping:

```text
python -m workbench.cli --root <runsRoot> <command> [arguments]
```

It uses `UseShellExecute=false`, `ArgumentList`, the configured backend working directory, UTF-8, and concurrent asynchronous stdout/stderr readers. JSONL is read by complete line, including a final line without a newline.

- `event` records are displayed as plain text and update the live stage rail with the backend's exact reported status. They do not enable mutations or establish a fresh completed run; that requires the terminal result.
- A zero exit code **and** a valid final `result` with the matching run ID are required for fresh state. A nonzero exit, malformed JSONL, `error` record, or missing result is shown as failure.
- Reported `running`, `partial`, `passed`, `failed`, and other statuses remain distinct. Architecture inspection passing is not relabeled as native device verification.
- Absent fields display **Not reported** or **UNKNOWN**. File and invalid counts are shown only when the backend supplies `totals.files` and `totals.invalids`.
- Approval displays repository, source commit, project, source-diff path, complete plan hash, `approvalKind`, and expandable exact approval-plan JSON. The button repeats the kind and current hash. A confirmation click sends only the currently displayed plan hash.
- A `source-plan` approval enables Port, which validates the unchanged baseline on the isolated runner before invoking the restricted agent. Completed edits return a new `build-patch` plan hash requiring another explicit approval before Build. An approved build-patch plan also permits retrying Port after a reported build failure. Missing or unknown approval kinds do not enable approval, porting or building. Additional CLI approval arguments belong only in `CreateStartInfo`.
- An `interrupted-port` approval is bound to preserved partial edits, not a completed port. The UI displays `artifacts.partialPatch`, `interruptedPort.reason`, and the backend's `interrupted` stage status. Approving the current hash enables the same `port` command to resume. Build, verification, fault injection, repair and export remain disabled while an interrupted port is reported.
- A terminal `error` record updates the displayed run, including its hash and dynamic stage rows, even when the process exits nonzero. The command remains failed and requires a status refresh. A separate baseline stage appears whenever the backend supplies it.
- Any mutation can return a new `needs-approval` run after detecting an interrupted attempt, without performing the requested action. The UI adopts that returned hash, kind and stage state; it never promotes the requested action to completion based on exit code alone.
- A process failure or cancellation invalidates the cached state until `status` returns. Cancellation targets only the started Python process and its descendants. Remote jobs may continue; the UI never marks them canceled or complete without backend state.
- Status restoration that still reports `running` leaves ordinary mutation actions disabled but enables **Resume / reconcile**. That command acquires the backend's operation lock before preserving partial edits or resuming a known remote job. It does not silently resend an ambiguous dispatch or infer a remote outcome.
- Artifact values are selectable text, never commands. **Open output folder** opens only an existing direct local directory inside the configured runs root.
- The event pane retains the latest 1,000 received lines and labels any discarded lines. Timestamps are receipt times in Eastern Time. Errors are not silently discarded or replaced with successful sample output.

The backend must already have its isolated runner and agent permissions configured privately through CLI `configure`. The runner requires `--runner-ref refs/tags/<immutable-tag>` and `--runner-ruleset-id <numeric>` identifying an active exact-tag ruleset with no bypass and updates/deletions blocked. This UI supplies no runner ref default, collects no credentials, dispatches no candidate commands itself, and does not attest on-device behavior. Validate real runs, approval rejection, cancellation, DPI scaling, keyboard navigation and both published architectures on the intended hosts before making device-verification claims.
