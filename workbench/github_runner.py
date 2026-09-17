from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import re
import subprocess
import time
import uuid
from typing import Any

from .core import Store, WorkbenchError, canonical, digest, file_hash, invalidate_package_state, parse_repository, read_json, require_approval, safe_extract, tree_hash, write_json
from .patches import encode_patch


def gh(config: dict, arguments: list[str], payload: dict | None = None, *, binary: bool = False) -> Any:
    environment = dict(os.environ)
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "COPILOT_GITHUB_TOKEN"):
        environment.pop(key, None)
    if config.get("runnerGhConfigDir"):
        environment["GH_CONFIG_DIR"] = config["runnerGhConfigDir"]
    command = [config.get("ghPath") or "gh", *arguments]
    if payload is not None:
        command.extend(["--input", "-"])
    completed = subprocess.run(command, input=canonical(payload) if payload is not None else None,
                               capture_output=True, env=environment, timeout=180)
    if completed.returncode:
        raise WorkbenchError(f"GitHub operation failed: {completed.stderr.decode('utf-8', errors='replace').strip()}")
    if binary:
        return completed.stdout
    text = completed.stdout.decode("utf-8")
    return json.loads(text) if text.strip() else None


def validate_runner_ruleset(ruleset: dict, ref: str, ruleset_id: int) -> None:
    conditions = ruleset.get("conditions", {})
    references = conditions.get("ref_name", {})
    if (ruleset.get("id") != ruleset_id or ruleset.get("target") != "tag"
            or ruleset.get("enforcement") != "active" or ruleset.get("bypass_actors") != []
            or set(conditions) != {"ref_name"} or references.get("exclude") != []
            or ref not in references.get("include", [])):
        raise WorkbenchError("The runner tag requires an active exact-tag ruleset with no exclusions or bypass actors.")
    rules = {item.get("type") for item in ruleset.get("rules", [])}
    if not {"update", "deletion"} <= rules:
        raise WorkbenchError("The runner tag must block both updates and deletion.")


def pinned_runner(config: dict) -> dict:
    repository = parse_repository(config["runnerRepository"])
    ref = config.get("runnerRef")
    ruleset_id = config.get("runnerRulesetId")
    if not isinstance(ref, str) or not re.fullmatch(r"refs/tags/[A-Za-z0-9][A-Za-z0-9_.-]+", ref) or ".." in ref:
        raise WorkbenchError("Choose a protected refs/tags/<tag>, not a mutable runner branch.")
    if type(ruleset_id) is not int or ruleset_id <= 0:
        raise WorkbenchError("Configure the no-bypass runner tag ruleset ID.")
    ruleset = gh(config, ["api", f"repos/{repository}/rulesets/{ruleset_id}"])
    validate_runner_ruleset(ruleset, ref, ruleset_id)
    reference = gh(config, ["api", f"repos/{repository}/git/ref/{ref.removeprefix('refs/')}"])
    if reference.get("ref") != ref or reference["object"]["type"] != "commit":
        raise WorkbenchError("Use a protected lightweight tag pointing directly at the reviewed runner commit.")
    commit = reference["object"]["sha"]
    workflow = "repo_to_arm_build.yml"
    metadata = gh(config, ["api", f"repos/{repository}/contents/.github/workflows/{workflow}?ref={commit}"])
    if metadata.get("type") != "file":
        raise WorkbenchError("The configured runner does not expose the expected workbench workflow.")
    return {"repository": repository, "ref": ref, "commit": commit, "workflow": workflow, "rulesetId": ruleset_id}


def bind_runner(store: Store, state: dict, config: dict) -> dict:
    state["plan"]["runner"] = pinned_runner(config)
    state["planHash"] = digest(state["plan"])
    state["approvalPlan"] = state["plan"]
    write_json(store.path(state["id"]) / "plan.json", state["plan"])
    store.save(state)
    return state


def verified_run_metadata(config: dict, runner: dict, invocation: str, run_id: int) -> dict:
    expected_title = f"Repo to Arm {invocation}"
    for attempt in range(12):
        found = gh(config, ["api", f"repos/{runner['repository']}/actions/runs/{run_id}"])
        if found.get("id") != run_id:
            raise WorkbenchError("The returned workflow run ID does not match the dispatch receipt.")
        commit = found.get("head_sha")
        title = found.get("display_title") or ""
        if commit and commit != runner["commit"]:
            raise WorkbenchError("The dispatched workflow uses an unapproved runner commit.")
        if commit == runner["commit"] and title == expected_title:
            return found
        if re.fullmatch(r"Repo to Arm [0-9a-f]{12}-[0-9a-f]{8}", title) and title != expected_title:
            raise WorkbenchError("The dispatched workflow belongs to another invocation.")
        if attempt < 11:
            time.sleep(1)
    raise WorkbenchError("Workflow identity has not materialized. The dispatch receipt is retained for a read-only resume.")


def dispatch_build(store: Store, state: dict, config: dict, plan: dict, patch: str, operation: str,
                   stage: str, *, retry_dispatch: bool = False) -> tuple[dict, str]:
    runner = plan["runner"]
    intent = state.get("pendingDispatch")
    if intent:
        if intent["planHash"] != digest(plan) or intent["repository"] != runner["repository"] or intent["operation"] != operation:
            raise WorkbenchError("An unconfirmed dispatch belongs to another plan; resolve it before changing execution.")
        if intent.get("runId"):
            found = verified_run_metadata(config, runner, intent["invocation"], intent["runId"])
            return found, intent["invocation"]
        runs = gh(config, ["api", f"repos/{runner['repository']}/actions/workflows/{runner['workflow']}/runs?event=workflow_dispatch&per_page=100"])["workflow_runs"]
        matches = [item for item in runs if item["display_title"] == f"Repo to Arm {intent['invocation']}"
                   and item["head_sha"] == runner["commit"]]
        if len(matches) > 1:
            raise WorkbenchError("An unconfirmed dispatch has multiple matching runs; no result will be selected automatically.")
        if matches:
            return matches[0], intent["invocation"]
        if not retry_dispatch:
            raise WorkbenchError("Dispatch outcome is unconfirmed. No duplicate was sent. Retry monitoring, or explicitly use build --retry-dispatch after reviewing GitHub.")
        state.setdefault("dispatchHistory", []).append({**intent, "resolution": "explicit-retry-with-no-matching-run"})
    invocation = f"{state['id']}-{uuid.uuid4().hex[:8]}"
    payload = {"ref": runner["ref"], "inputs": {
        "run_id": invocation, "plan": canonical(plan).decode("utf-8"),
        "approved_plan_hash": digest(plan), "patch": encode_patch(patch),
    }}
    if len(canonical(payload)) > 64000:
        raise WorkbenchError("The complete reviewed plan and patch exceed the workflow input budget.")
    state["pendingDispatch"] = {
        "invocation": invocation, "planHash": digest(plan), "repository": runner["repository"],
        "operation": operation, "requestedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    store.event(state, stage, "running", f"Dispatching {operation} build to a disposable Windows VM.")
    dispatched = gh(config, ["api", "--method", "POST", "-H", "X-GitHub-Api-Version: 2026-03-10",
                            f"repos/{runner['repository']}/actions/workflows/{runner['workflow']}/dispatches"], payload)
    if not dispatched or not dispatched.get("workflow_run_id"):
        raise WorkbenchError("Dispatch returned no run identity. Its intent is retained; a retry will not silently submit another run.")
    state["pendingDispatch"]["runId"] = dispatched["workflow_run_id"]
    store.save(state)
    found = verified_run_metadata(config, runner, invocation, dispatched["workflow_run_id"])
    return found, invocation


def run_build(store: Store, state: dict, config: dict, *, retry_dispatch: bool = False) -> dict:
    run = store.path(state["id"])
    candidate = (run / "build-plan.json").exists() and state.get("workingSourceHash")
    kind = "build-patch" if candidate else "source-plan"
    require_approval(store, state, kind)
    plan = read_json(run / ("build-plan.json" if candidate else "plan.json"))
    runner = plan.get("runner")
    if not runner:
        raise WorkbenchError("Configure and bind an isolated runner before approving the plan.")
    current = pinned_runner(config)
    if current != runner:
        raise WorkbenchError("Runner code/ref changed after approval. Rebind and reapprove the plan.")
    patch = (run / "port.patch").read_bytes().decode("utf-8") if candidate else ""
    from .runner import validate_plan
    validate_plan(plan, digest(plan), patch)
    operation = "candidate" if candidate else "baseline"
    stage = "build" if candidate else "baseline"
    existing = state.get("githubRun", {})
    same_plan = (existing.get("operation") == operation and existing.get("planHash") == digest(plan)
                 and existing.get("repository") == runner["repository"])
    if same_plan and existing.get("artifactsIngested") and existing.get("conclusion") == "success":
        store.event(state, stage, "passed", "This exact isolated build is already retained; no additional workflow was dispatched.")
        return state
    if candidate:
        invalidate_package_state(state)
    if existing and not existing.get("artifactsIngested"):
        if existing["operation"] != operation or existing["planHash"] != digest(plan) or existing["repository"] != runner["repository"]:
            raise WorkbenchError("An unfinished isolated run belongs to another plan; resolve it before dispatching new work.")
        found = gh(config, ["api", f"repos/{runner['repository']}/actions/runs/{existing['id']}"])
        invocation = existing["invocation"]
        store.event(state, stage, "running", f"Resuming monitoring of isolated run {found['id']}.", url=found["html_url"])
    else:
        found, invocation = dispatch_build(store, state, config, plan, patch, operation, stage, retry_dispatch=retry_dispatch)
        if found["display_title"] != f"Repo to Arm {invocation}" or found["head_sha"] != runner["commit"]:
            raise WorkbenchError("Dispatched runner identity does not match the approved invocation.")
        state["githubRun"] = {"id": found["id"], "repository": runner["repository"], "url": found["html_url"],
                              "operation": operation, "invocation": invocation, "planHash": digest(plan)}
        state.pop("pendingDispatch", None)
        store.event(state, stage, "running", f"Isolated {operation} run {found['id']} started.", url=found["html_url"])
    deadline = time.monotonic() + 40 * 60
    previous_status = None
    while time.monotonic() < deadline:
        found = gh(config, ["api", f"repos/{runner['repository']}/actions/runs/{found['id']}"])
        if found["head_sha"] != runner["commit"]:
            raise WorkbenchError("Runner source identity changed.")
        if found["status"] != previous_status:
            store.event(state, stage, "running", f"GitHub runner: {found['status']}.", url=found["html_url"])
            previous_status = found["status"]
        if found["status"] == "completed":
            break
        time.sleep(12)
    else:
        raise WorkbenchError("Isolated build monitoring timed out; inspect/resume the recorded GitHub run.")
    state["githubRun"]["conclusion"] = found["conclusion"]
    artifact_list = gh(config, ["api", f"repos/{runner['repository']}/actions/runs/{found['id']}/artifacts"])["artifacts"]
    artifacts = [item for item in artifact_list if item["name"] == f"rta-{invocation}" and not item["expired"]]
    if len(artifacts) != 1:
        raise WorkbenchError(f"Expected one artifact for run {found['id']}; build concluded {found['conclusion']}.")
    folder = run / "builds" / f"{invocation}-{uuid.uuid4().hex[:6]}"
    folder.mkdir(parents=True)
    archive = folder / "runner-artifacts.zip"
    archive.write_bytes(gh(config, ["api", f"repos/{runner['repository']}/actions/artifacts/{artifacts[0]['id']}/zip"], binary=True))
    extracted = folder / "output"
    safe_extract(archive, extracted)
    write_json(folder / "github-run.json", {
        "id": found["id"], "url": found["html_url"], "conclusion": found["conclusion"],
        "headSha": found["head_sha"], "artifactSha256": file_hash(archive), "planHash": digest(plan),
    })
    if found["conclusion"] != "success":
        failure_path = extracted / "failure.json"
        failure = read_json(failure_path).get("error", found["conclusion"]) if failure_path.exists() else found["conclusion"]
        logs = []
        for file in sorted((extracted / "logs").glob("*.log")):
            logs.append(file.read_text(encoding="utf-8", errors="replace")[-6000:])
        state["buildFailure"] = str(failure) + "\n" + "\n".join(logs)[-16000:]
        state["artifacts"]["buildLogs"] = str(extracted)
        state["githubRun"]["artifactsIngested"] = True
        store.event(state, stage, "failed", f"Isolated build failed: {failure}", url=found["html_url"])
        raise WorkbenchError(state["buildFailure"])
    receipt = read_json(extracted / "build-receipt.json")
    if receipt["sourceCommit"] != plan["sourceCommit"] or receipt["approvedPlanHash"] != digest(plan) or receipt["toolkitCommit"] != runner["commit"]:
        raise WorkbenchError("Build receipt does not match the approved source and runner identity.")
    if (receipt.get("githubRunId") != str(found["id"]) or receipt.get("runId") != plan["runId"]
            or receipt.get("operation") != operation or receipt.get("repository") != plan["repository"]
            or receipt.get("sdkVersion") != plan["sdkVersion"] or receipt.get("patchSha256") != plan.get("patchSha256")):
        raise WorkbenchError("Build receipt invocation, SDK, or patch identity mismatch.")
    expected_runtime_ids = ["win-x64", "win-arm64"] if candidate else ["win-x64"]
    if [item["runtime"] for item in receipt["packages"]] != expected_runtime_ids:
        raise WorkbenchError("Build output architecture set is incomplete.")
    packages = {}
    for item in receipt["packages"]:
        if item["file"] != f"{item['runtime']}.zip":
            raise WorkbenchError("Package receipt contains an unexpected path.")
        package = extracted / item["file"]
        if file_hash(package) != item["sha256"]:
            raise WorkbenchError("Package identity does not match the build receipt.")
        destination = folder / item["runtime"]
        safe_extract(package, destination)
        packages[item["runtime"]] = {"root": str(destination), "zip": str(package), "sha256": item["sha256"],
                                     "treeHash": tree_hash(destination)}
    state.pop("buildFailure", None)
    if candidate:
        invalidate_package_state(state)
        state["candidateBuild"] = {
            "planHash": digest(plan), "githubRunId": found["id"], "runnerCommit": runner["commit"],
            "repository": runner["repository"], "receiptSha256": file_hash(extracted / "build-receipt.json"),
        }
        state["packages"] = packages
        state["activePackage"] = packages["win-arm64"]["root"]
        state["artifacts"].update({"arm64Package": packages["win-arm64"]["zip"], "x64Package": packages["win-x64"]["zip"],
                                   "buildReceipt": str(extracted / "build-receipt.json")})
    else:
        state["baseline"] = {"passed": True, "packages": packages, "receipt": str(extracted / "build-receipt.json")}
    state["githubRun"]["artifactsIngested"] = True
    store.event(state, stage, "passed", f"Isolated {operation} build completed. Device/runtime success is not yet claimed.",
                url=found["html_url"])
    return state
