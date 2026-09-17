from __future__ import annotations

import pathlib
import shutil
import subprocess
import uuid

from .agent import invoke_agent, schema
from .core import TOOLKIT, Store, WorkbenchError, below, digest, file_hash, now, read_json, tree_hash, tree_manifest, write_json


def candidate_identity(store: Store, state: dict) -> dict:
    candidate = state.get("candidateBuild")
    plan = read_json(store.path(state["id"]) / "build-plan.json")
    if not candidate or candidate["planHash"] != state["planHash"] or digest(plan) != state["planHash"]:
        raise WorkbenchError("Package authority does not belong to the current approved build plan.")
    build = state.get("githubRun", {})
    if build.get("operation") != "candidate" or build.get("conclusion") != "success" or build.get("id") != candidate["githubRunId"]:
        raise WorkbenchError("Package authority does not belong to the current successful candidate build.")
    receipt = pathlib.Path(state["artifacts"]["buildReceipt"]).resolve()
    if not receipt.is_relative_to(store.path(state["id"]).resolve()) or file_hash(receipt) != candidate["receiptSha256"]:
        raise WorkbenchError("The candidate build receipt changed.")
    return dict(candidate)


def active_root(store: Store, state: dict) -> pathlib.Path:
    if not state.get("activePackage"):
        raise WorkbenchError("No candidate package is available.")
    path = pathlib.Path(state["activePackage"]).resolve()
    run = store.path(state["id"]).resolve()
    if not path.is_dir() or not path.is_relative_to(run) or path == run:
        raise WorkbenchError("Active package is outside the trusted run.")
    tree_manifest(path)
    return path


def require_verification(store: Store, state: dict, *, passed: bool = True) -> dict:
    identity = candidate_identity(store, state)
    path = active_root(store, state)
    proof = state.get("verification", {})
    if proof.get("passed") is not passed or proof.get("candidate") != identity:
        raise WorkbenchError("Fresh independent verification is required for the current candidate.")
    if proof.get("packageRoot") != str(path) or proof.get("packageTreeHash") != tree_hash(path):
        raise WorkbenchError("The active package changed after verification.")
    report_path = pathlib.Path(proof["report"]).resolve()
    if not report_path.is_relative_to(store.path(state["id"]).resolve()) or file_hash(report_path) != proof["reportSha256"]:
        raise WorkbenchError("The bound architecture report changed.")
    report = read_json(report_path)
    if pathlib.Path(report["RootPath"]).resolve() != path or report["ExpectedArchitecture"] != "arm64":
        raise WorkbenchError("The architecture report is for another package or architecture.")
    return report


def verify_package(store: Store, state: dict, config: dict) -> dict:
    identity = candidate_identity(store, state)
    path = active_root(store, state)
    package_hash = tree_hash(path)
    state.pop("verification", None)
    state.pop("deviceEvidence", None)
    state["artifacts"].pop("deviceEvidence", None)
    state["artifacts"].pop("evidence", None)
    run = store.path(state["id"])
    report = run / "verification" / f"{uuid.uuid4().hex[:10]}-architecture.json"
    report.parent.mkdir(exist_ok=True)
    expected_exe = state["assemblyName"] + ".exe"
    if pathlib.PureWindowsPath(expected_exe).name != expected_exe or not (path / expected_exe).is_file():
        raise WorkbenchError("The expected application entry point is missing.")
    store.event(state, "verify", "running", "Inspecting actual PE metadata independently of the coding agent.")
    command = [
        config.get("pwshPath") or "pwsh", "-NoProfile", "-File", str(TOOLKIT / "scripts" / "Inspect-WoaPe.ps1"),
        "-RootPath", str(path), "-ExpectedArchitecture", "arm64", "-ReportPath", str(report),
        "-RequiredRelativePath", expected_exe,
    ]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=300)
    (report.with_suffix(".log")).write_text(completed.stdout + "\n" + completed.stderr, encoding="utf-8")
    if not report.exists():
        store.event(state, "verify", "failed", "The inspector did not produce a report.")
        raise WorkbenchError("Architecture inspector failed without a usable report.")
    result = read_json(report)
    if pathlib.Path(result["RootPath"]).resolve() != path or result["ExpectedArchitecture"] != "arm64" or not result["FileCount"]:
        raise WorkbenchError("Inspector report identity or scope mismatch.")
    ec = [item for item in result["Files"] if item.get("Machine") == "Arm64EC"]
    invalid = result["InvalidCount"] + len(ec)
    if tree_hash(path) != package_hash:
        raise WorkbenchError("Package bytes changed during architecture inspection.")
    state["artifacts"]["architectureReport"] = str(report)
    state["totals"] = {"files": result["FileCount"], "invalids": invalid}
    state["verification"] = {"passed": completed.returncode == 0 and invalid == 0, "report": str(report),
                             "reportSha256": file_hash(report), "packageTreeHash": package_hash, "at": now(),
                             "packageRoot": str(path), "candidate": identity}
    if completed.returncode or invalid:
        failures = [f"{item['Path']}: {item.get('Machine') or item.get('Error')}" for item in result["Files"] if not item["Valid"]]
        if ec:
            failures.append("Arm64EC is outside this full-ARM64 plan.")
        store.event(state, "verify", "failed", f"Rejected: {invalid} architecture errors. " + "; ".join(failures[:4]))
        raise WorkbenchError("Package rejected by independent architecture verification.")
    store.event(state, "verify", "passed", f"{result['FileCount']} files inspected; no incompatible architecture. Device evidence is separate.")
    return state


def inject_fault(store: Store, state: dict) -> dict:
    require_verification(store, state)
    original = active_root(store, state)
    x64 = pathlib.Path(state["packages"]["win-x64"]["root"]).resolve()
    if not x64.is_relative_to(store.path(state["id"]).resolve()):
        raise WorkbenchError("The x64 reference package is outside the run.")
    if tree_hash(x64) != state["packages"]["win-x64"]["treeHash"]:
        raise WorkbenchError("The x64 reference package changed after build ingestion.")
    wrong_binary = x64 / "coreclr.dll"
    if not wrong_binary.is_file():
        raise WorkbenchError("This declared fault requires the x64 self-contained .NET runtime.")
    copy = store.path(state["id"]) / "faults" / uuid.uuid4().hex[:8]
    shutil.copytree(original, copy)
    target = copy / "declared-demo-fault"
    target.mkdir()
    shutil.copy2(wrong_binary, target / "x64-runtime.dll")
    state["fault"] = {
        "declaredDemonstration": True, "kind": "extra-x64-native-binary",
        "originalPackage": str(original), "originalTreeHash": tree_hash(original),
        "injectedFile": "declared-demo-fault/x64-runtime.dll", "injectedSha256": file_hash(wrong_binary),
        "at": now(), "candidate": candidate_identity(store, state),
        "faultPackage": str(copy.resolve()), "faultTreeHash": tree_hash(copy),
    }
    state["activePackage"] = str(copy)
    state["verification"] = {"passed": False}
    state.pop("deviceEvidence", None)
    state.pop("recovery", None)
    for key in ("deviceEvidence", "architectureReport", "evidence"):
        state["artifacts"].pop(key, None)
    store.event(state, "verify", "needs-verification", "Declared fault injection: an x64 runtime DLL was added to a separate ARM64 package copy.")
    return state


async def repair_package(store: Store, state: dict, config: dict) -> dict:
    if not state.get("fault", {}).get("declaredDemonstration") or state.get("verification", {}).get("passed"):
        raise WorkbenchError("Recovery requires a declared fault and a failing verification result.")
    report = require_verification(store, state, passed=False)
    fault = state["fault"]
    package = active_root(store, state)
    if fault["candidate"] != candidate_identity(store, state) or str(package) != fault["faultPackage"] or tree_hash(package) != fault["faultTreeHash"]:
        raise WorkbenchError("Recovery is authorized only for this candidate's declared package-contamination copy.")
    failures = [item for item in report["Files"] if not item["Valid"]]
    if report["InvalidCount"] != 1 or len(failures) != 1 or failures[0]["Path"].replace("\\", "/") != fault["injectedFile"]:
        raise WorkbenchError("The failure is not exclusively the declared injected binary; automatic restoration is not permitted.")
    original = pathlib.Path(state["fault"]["originalPackage"]).resolve()
    if not original.is_relative_to(store.path(state["id"]).resolve()) or tree_hash(original) != state["fault"]["originalTreeHash"]:
        raise WorkbenchError("Known-good package identity changed; automatic restoration is not permitted.")
    restored: list[str] = []

    def read_report(args):
        return {"expectedArchitecture": report["ExpectedArchitecture"], "invalidCount": report["InvalidCount"],
                "invalidFiles": [item for item in report["Files"] if not item["Valid"]],
                "declaredFault": state["fault"]["kind"]}

    def restore(args):
        if restored:
            raise WorkbenchError("A restoration was already performed; independent verification must run next.")
        if not isinstance(args.get("reason"), str) or not args["reason"].strip():
            raise WorkbenchError("The recovery agent must explain why restoration is justified.")
        if tree_hash(original) != state["fault"]["originalTreeHash"]:
            raise WorkbenchError("Known-good package changed during recovery.")
        require_verification(store, state, passed=False)
        destination = store.path(state["id"]) / "repaired" / uuid.uuid4().hex[:8]
        shutil.copytree(original, destination)
        if tree_hash(destination) != state["fault"]["originalTreeHash"]:
            raise WorkbenchError("Restoration did not reproduce the known-good package.")
        restored.append(str(destination))
        state["activePackage"] = str(destination)
        state["recovery"] = {"at": now(), "reason": args["reason"], "restoredTreeHash": tree_hash(destination),
                             "candidate": candidate_identity(store, state),
                             "failureReportSha256": state["verification"]["reportSha256"]}
        state.pop("verification", None)
        state.pop("deviceEvidence", None)
        store.event(state, "port", "running", "Recovery agent restored the independently identified known-good package; re-verification is required.")
        return {"restored": True, "verification": "not-yet-run"}

    tools = [
        {"name": "rta_read_failure", "description": "Read the independent architecture failure and declared fault type.",
         "parameters": schema({}, []), "handler": read_report},
        {"name": "rta_restore_known_good_package", "description": "Create a new package copy from the immutable known-good package. Cannot mark verification passed.",
         "parameters": schema({"reason": {"type": "string"}}, ["reason"]), "handler": restore},
    ]
    store.event(state, "port", "running", "Routing the declared package-contamination failure to the scoped recovery agent.")
    await invoke_agent(store, state, config, purpose="port", tool_definitions=tools, prompt=(
        "This is a labeled fault-injection exercise, not an unexplained production failure. "
        "Inspect the independent failure report. If it confirms an incompatible injected binary and a known-good "
        "package exists, explain and restore the known-good package using the provided tool. "
        "Do not claim verification passed: the controller will run the deterministic inspector after you return."
    ))
    if not restored:
        raise WorkbenchError("The recovery agent did not restore a package.")
    store.event(state, "port", "passed", "Package restoration completed. Running the independent verifier again.")
    return verify_package(store, state, config)


def collect_process_proof(store: Store, state: dict, process_id: int, config: dict) -> dict:
    if type(process_id) is not int or process_id <= 0:
        raise WorkbenchError("Device evidence must identify the still-running application process.")
    package = active_root(store, state)
    report = store.path(state["id"]) / "device-probes" / f"{uuid.uuid4().hex[:10]}.json"
    report.parent.mkdir(exist_ok=True)
    completed = subprocess.run([
        config.get("pwshPath") or "pwsh", "-NoProfile", "-File", str(TOOLKIT / "scripts" / "Inspect-WoaProcess.ps1"),
        "-ProcessId", str(process_id), "-ExpectedExecutable", str(package / (state["assemblyName"] + ".exe")),
        "-ApplicationRoot", str(package), "-ExpectedArchitecture", "arm64", "-ReportPath", str(report),
    ], capture_output=True, text=True, timeout=120)
    report.with_suffix(".log").write_text(completed.stdout + "\n" + completed.stderr, encoding="utf-8")
    if completed.returncode or not report.exists():
        raise WorkbenchError("Independent inspection of the running native process failed. See device-probes.")
    proof = read_json(report)
    if proof.get("passed") is not True or proof.get("osArchitecture") != "Arm64" or proof.get("processArchitecture") != "Arm64":
        raise WorkbenchError("The live process probe did not establish ARM64 execution on ARM64 Windows.")
    if proof.get("processId") != process_id or proof.get("architectureApi") != "IsWow64Process2":
        raise WorkbenchError("Live process proof identity is invalid.")
    executable = package / (state["assemblyName"] + ".exe")
    if pathlib.Path(proof["executable"]).resolve() != executable or proof["executableSha256"] != file_hash(executable):
        raise WorkbenchError("The live executable is not the verified package entry point.")
    modules = [item for item in proof["modules"] if item["packageRelativePath"]]
    if len([item for item in modules if item["name"].casefold() == "coreclr.dll"]) != 1:
        raise WorkbenchError("The package's native .NET runtime must be observed in the live process.")
    for module in modules:
        path = below(package, module["packageRelativePath"], must_exist=True)
        if pathlib.Path(module["path"]).resolve() != path or file_hash(path) != module["sha256"]:
            raise WorkbenchError("A loaded package module differs from the inspected package.")
    return {"path": str(report), "sha256": file_hash(report), "result": proof}


def record_device_evidence(store: Store, state: dict, evidence_path: pathlib.Path, config: dict | None = None) -> dict:
    require_verification(store, state)
    package = active_root(store, state)
    evidence = read_json(evidence_path)
    if evidence.get("packageTreeHash") != tree_hash(package):
        raise WorkbenchError("Device evidence does not match this exact package.")
    if evidence.get("candidate") != candidate_identity(store, state):
        raise WorkbenchError("Device evidence must identify the approved candidate and isolated build.")
    scenarios = evidence.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise WorkbenchError("Explicit scenario evidence is required.")
    if not all(isinstance(item, dict) and item.get("passed") is True
               and isinstance(item.get("name"), str) and item["name"].strip() for item in scenarios):
        raise WorkbenchError("A device scenario is missing or failed.")
    if len({item["name"].casefold().strip() for item in scenarios}) != len(scenarios):
        raise WorkbenchError("Each device scenario must have a distinct name.")
    proof = collect_process_proof(store, state, evidence.get("processId"), config or {})
    if evidence.get("processStartedAt") != proof["result"]["processStartedAt"]:
        raise WorkbenchError("The scenario report belongs to another process instance.")
    require_verification(store, state)
    destination = store.path(state["id"]) / "device-evidence.json"
    write_json(destination, {**evidence, "liveProcessProof": proof,
                             "scenarioSource": "operator-supplied scenario assertions; live architecture independently inspected"})
    state["deviceEvidence"] = {"path": str(destination), "sha256": file_hash(destination),
                               "packageTreeHash": tree_hash(package), "candidate": candidate_identity(store, state)}
    state["artifacts"]["deviceEvidence"] = str(destination)
    store.event(state, "evidence", "passed", "Package-bound native device and scenario evidence recorded.")
    return state


def export_evidence(store: Store, state: dict) -> dict:
    require_verification(store, state)
    package = active_root(store, state)
    if tree_hash(package) != state["verification"]["packageTreeHash"]:
        raise WorkbenchError("Package changed after verification.")
    device = state.get("deviceEvidence")
    if device:
        path = pathlib.Path(device["path"]).resolve()
        if (not path.is_relative_to(store.path(state["id"]).resolve()) or file_hash(path) != device["sha256"]
                or device["packageTreeHash"] != tree_hash(package) or device["candidate"] != candidate_identity(store, state)):
            raise WorkbenchError("Device evidence changed or is for another candidate.")
    level = "device-verified" if device else "package-only"
    report = {
        "schemaVersion": 1, "generatedAt": now(), "runId": state["id"], "repository": state["repository"],
        "sourceCommit": state["sourceCommit"], "planHash": state["planHash"], "verificationLevel": level,
        "architecture": state["verification"], "deviceEvidence": state.get("deviceEvidence"),
        "sourcePatch": state["artifacts"].get("patch"), "build": state.get("githubRun"),
        "recovery": state.get("recovery"), "declaredFault": state.get("fault"),
        "limitations": [] if level == "device-verified" else ["Physical-device validation is not yet recorded."],
    }
    destination = store.path(state["id"]) / "evidence.json"
    write_json(destination, report)
    state["artifacts"]["evidence"] = str(destination)
    store.event(state, "evidence", "passed" if level == "device-verified" else "partial",
                f"Evidence exported with explicit level: {level}.")
    return state
