# Repo to Arm Workbench

Repo to Arm is a constrained preview for SDK-style .NET Windows desktop applications. It connects the existing Scout assessment, a real Copilot source-editing agent, disposable Windows builds, and independent architecture evidence. It does not promise to port arbitrary repositories.

## Supported Scope

- Public GitHub source pinned to a full commit, with one explicit .NET 8, 9, or 10 Windows WPF/WinForms executable project.
- Full ARM64 portable output and a retained x64 comparison package.
- Text patches below 50 KB. Submodules, binary patches, renames, framework migrations, installers, and updater redesign require a different execution profile.
- Separate source worktrees for x64, ARM64, and existing test projects, so intermediate outputs and restore state cannot cross architectures.
- Discovered test projects run with explicit `dotnet test` commands. Custom repository test scripts are not automatically executed.

The command-line engine is usable independently of a graphical frontend.

## Trust and Approval

Repository instructions and build files are untrusted input. The coding agent receives only scoped text read/edit tools. It cannot run a shell, access host credentials, change the execution plan, or mark a test passed.

Source, Scout evidence, exact commands, and runner identity are bound to an approval hash. Agent edits produce a new build-patch approval. An interrupted edit preserves the partial patch and requires a separate approval to resume. Run mutations use an operating-system file lock; contention does not write stale state.

Third-party restore, build, and test code runs in a disposable GitHub-hosted Windows VM. Personal credentials and signing keys are not copied to that VM. GitHub's job-scoped service credentials and public artifact/log visibility remain part of the runner's trust boundary. Use a runner repository with no reusable repository secrets.

The runner must use a lightweight tag protected by an active repository ruleset that blocks updates and deletion with no bypass actors. Both the tag and its commit enter the approval. A mutable branch, including `main`, is rejected as an execution ref. Repository administrators can change rulesets, so control of the runner repository remains a trust assumption.

These controls do not protect against a malicious process already running as the operator and editing the workbench's state files. Approval is an explicit local operator action, not a cryptographic user-signature service.

## Setup

Keep the checkout, Python environment, source worktrees, artifacts, and captures outside OneDrive. Install the pinned Python dependency in a dedicated environment:

```powershell
python -m venv C:\Tools\RepoToArm\.venv
C:\Tools\RepoToArm\.venv\Scripts\python.exe -m pip install -r .\workbench\requirements.txt
```

The host requires Git, GitHub CLI, and PowerShell 7. The operator needs a Copilot-entitled GitHub account and write access to the isolated runner repository. Separate GitHub CLI configuration directories can keep the Copilot and repository identities independent. Tokens are obtained privately from GitHub CLI and are not stored in the workbench configuration.

Publish the reviewed workflow and engine to the runner repository's default branch before dispatching. Create a lightweight tag at that commit, then apply this ruleset shape with the actual tag name:

```json
{
  "name": "Repo to Arm immutable runner",
  "target": "tag",
  "enforcement": "active",
  "bypass_actors": [],
  "conditions": {
    "ref_name": {"include": ["refs/tags/runner-version"], "exclude": []}
  },
  "rules": [{"type": "update"}, {"type": "deletion"}]
}
```

Tag creation, ruleset changes, and workflow dispatch are external writes requiring operator authorization. Never move an existing runner tag to publish a fix; use a new tag and a new approval.

```powershell
python -m workbench.cli --root C:\Work\RepoToArm\runs configure `
  --runner-repository owner/trusted-toolkit `
  --runner-ref refs/tags/runner-version `
  --runner-ruleset-id 12345 `
  --copilot-user copilot-enabled-account

python -m workbench.cli --root C:\Work\RepoToArm\runs scout owner/application `
  --project src\Desktop\Desktop.csproj --commit <full-source-commit>
```

Add repeatable `--release-repo owner/development-builds` arguments when a verified related repository publishes another release channel. The run records Windows-specific observations across those channels. A macOS ARM64 asset cannot close a Windows gap, and a stable-channel gap cannot hide an observed Windows ARM64 prerelease. Filename evidence is not binary or runtime proof.

## Execution

Review the generated Scout report and `plan.json`. Source approval permits the unchanged x64 baseline and the scoped coding-agent phase. It does not approve running a third-party application on the host.

```powershell
python -m workbench.cli --root C:\Work\RepoToArm\runs approve <run-id> --plan-hash <source-plan-hash>
python -m workbench.cli --root C:\Work\RepoToArm\runs port <run-id>
```

The unchanged baseline must succeed before source editing begins. Review `port.patch` and `build-plan.json`, including changes to detected test commands, before the candidate build:

```powershell
python -m workbench.cli --root C:\Work\RepoToArm\runs approve <run-id> --plan-hash <build-plan-hash>
python -m workbench.cli --root C:\Work\RepoToArm\runs build <run-id>
python -m workbench.cli --root C:\Work\RepoToArm\runs verify <run-id>
```

No package is treated as device-verified by a successful build or a model's prose. Verification uses the existing PE inspector and rejects unexpected x64, x86, and Arm64EC content for this full-ARM64 profile. Artifacts, verification reports, recovery state, and device evidence are bound to the current build plan and GitHub run.

For a declared quality-gate demonstration, `inject-fault` adds a known x64 runtime DLL to a separate package copy. Run `verify` to record its rejection, then `repair` to let the restricted recovery agent request restoration from the current hash-bound known-good package. The controller re-runs the independent verifier afterward. This demonstrates package-contamination recovery, not arbitrary source-bug repair.

## Device Evidence

Approve the exact package hash and application command before launching third-party output on the physical laptop. Use the existing UI benchmark tools with a trusted, app-specific scenario configuration. Do not use screen recording during timed measurements.

`record-device --report <scenario-report.json>` imports package-bound scenario assertions and independently inspects the still-running process using `IsWow64Process2`. The report must contain the current `candidateBuild` object as `candidate`, `packageTreeHash`, `processId`, the process's UTC `processStartedAt`, and named, passing `scenarios`. The executable path, loaded package runtime modules, module hashes, and process instance must match. Scenario assertions remain attributable to the supplied harness; architecture is independently re-measured.

`export` labels output as `package-only` or `device-verified`. It rejects changed packages, modified reports, and stale build/device evidence. Package verification does not imply compatible third-party plugins, production signing, or improved battery life.

## Protocol and Diagnostics

Each command writes JSON Lines. Event records contain a monotonic sequence and real stage/status/detail values. A terminal `result` contains the current run; an `error` is accompanied by a nonzero exit code. Frontends must render these values, not infer success from an elapsed timer.

`status <run-id>` is read-only. `port` can discover a killed earlier edit and return an `interrupted-port` approval instead of executing new work. Review its retained partial patch, approve the new hash, and invoke `port` again.

Dispatch intent is persisted before the API request. An ambiguous response is reconciled against that invocation rather than silently dispatched twice. `build --retry-dispatch` explicitly permits another request only if no matching run is found. An interrupted artifact download resumes from its existing run, even when the build itself has already completed.

The run directory retains source manifests, Scout output, all patches, approval records, actual build logs, receipts, architecture inventories, process probes, and evidence reports. GitHub logs/artifacts are short-lived, so retain the local copies.
