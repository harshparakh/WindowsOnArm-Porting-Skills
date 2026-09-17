from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import zipfile

from .core import (
    COMMIT_PATTERN, PROJECT_SUFFIXES, RUN_PATTERN, WorkbenchError, commands_for, digest, discover_projects,
    file_hash, git, now, parse_repository, read_json, relative_path, test_commands_for, tree_hash, tree_manifest,
    write_json,
)
from .patches import MAX_PATCH_BYTES, decode_patch


BASE_KEYS = {
    "schemaVersion", "runId", "repository", "sourceCommit", "sourceHash", "project", "framework",
    "assemblyName", "sdkVersion", "execution", "architectures", "commands", "scope", "runner",
    "sourceIsolation", "testCommands", "assessmentSha256",
}
PATCH_KEYS = {"patchSha256", "workingSourceHash"}


def validate_plan(plan: dict, approved_hash: str, patch: str) -> str:
    if not isinstance(plan, dict) or set(plan) not in (BASE_KEYS, BASE_KEYS | PATCH_KEYS):
        raise WorkbenchError("Unexpected execution-plan fields.")
    if plan["schemaVersion"] != 1 or plan["execution"] != "github-hosted-windows":
        raise WorkbenchError("Unsupported execution-plan contract.")
    if not RUN_PATTERN.fullmatch(plan["runId"]) or not COMMIT_PATTERN.fullmatch(plan["sourceCommit"]):
        raise WorkbenchError("Invalid run/commit identity.")
    if parse_repository(plan["repository"]) != plan["repository"]:
        raise WorkbenchError("Repository identity must be canonical.")
    if not re.fullmatch(r"[0-9a-f]{64}", plan["assessmentSha256"]):
        raise WorkbenchError("The plan must bind the pinned source's Scout report.")
    relative_path(plan["project"])
    if pathlib.PurePosixPath(plan["project"]).suffix.lower() not in PROJECT_SUFFIXES:
        raise WorkbenchError("Only explicit SDK-style C# or Visual Basic project builds are supported.")
    if not isinstance(plan["scope"], str) or not plan["scope"].strip() or len(plan["scope"]) > 4000:
        raise WorkbenchError("The approval must include a bounded, explicit port scope.")
    if not re.fullmatch(r"net(?:8|9|10)\.0-windows(?:\d+\.\d+\.\d+\.\d+)?", plan["framework"]):
        raise WorkbenchError("Unsupported target framework.")
    if not re.fullmatch(r"(?:8|9|10)\.\d+\.\d{3}", plan["sdkVersion"]):
        raise WorkbenchError("An exact Microsoft .NET SDK version is required.")
    if plan["architectures"] != ["win-x64", "win-arm64"]:
        raise WorkbenchError("Unexpected architecture set.")
    expected = {rid: commands_for(plan["project"], plan["framework"], rid, f"<isolated-output>/{rid}")
                for rid in plan["architectures"]}
    if plan["commands"] != expected:
        raise WorkbenchError("Commands differ from the fixed supported execution profile.")
    if plan["sourceIsolation"] != "worktree-per-runtime" or not isinstance(plan["testCommands"], list):
        raise WorkbenchError("Restore/build/test output must be isolated in disposable per-runtime worktrees.")
    for command in plan["testCommands"]:
        if not isinstance(command, list) or len(command) != 9:
            raise WorkbenchError("Invalid approved test command.")
        relative_path(command[2])
        if (pathlib.PurePosixPath(command[2]).suffix.lower() not in PROJECT_SUFFIXES
                or command != ["dotnet", "test", command[2], "-c", "Release", "--logger", "trx",
                               "--results-directory", "<isolated-output>/tests"]):
            raise WorkbenchError("Tests must use the fixed explicit project test profile.")
    if digest(plan) != approved_hash:
        raise WorkbenchError("Approval does not cover the supplied execution plan.")
    runner = plan["runner"]
    if not isinstance(runner, dict) or set(runner) != {"repository", "ref", "commit", "workflow", "rulesetId"}:
        raise WorkbenchError("Missing pinned runner identity.")
    parse_repository(runner["repository"])
    if not COMMIT_PATTERN.fullmatch(runner["commit"]) or runner["workflow"] != "repo_to_arm_build.yml":
        raise WorkbenchError("Unsupported runner workflow identity.")
    if (not re.fullmatch(r"refs/tags/[A-Za-z0-9][A-Za-z0-9_.-]+", runner["ref"])
            or ".." in runner["ref"] or type(runner["rulesetId"]) is not int or runner["rulesetId"] <= 0):
        raise WorkbenchError("The approved workflow must use an immutable protected runner tag.")
    operation = "candidate" if PATCH_KEYS <= set(plan) else "baseline"
    if operation == "baseline" and patch:
        raise WorkbenchError("The unchanged baseline must not contain a source patch.")
    if operation == "candidate":
        if not patch.strip() or len(patch.encode("utf-8")) > MAX_PATCH_BYTES:
            raise WorkbenchError("Candidate patch is missing or exceeds the supported input limit.")
        if hashlib.sha256(patch.encode("utf-8")).hexdigest() != plan["patchSha256"]:
            raise WorkbenchError("Patch does not match the reviewed build plan.")
        if any(marker in patch for marker in ("GIT binary patch", "rename from ", "rename to ", "new file mode 120000")):
            raise WorkbenchError("Binary, rename, and link patches require a different reviewed execution profile.")
        for line in patch.splitlines():
            if line.startswith(("--- ", "+++ ")):
                value = line[4:].split("\t", 1)[0]
                if value == "/dev/null":
                    continue
                if value.startswith('"') or not value.startswith(("a/", "b/")):
                    raise WorkbenchError("Only ordinary repository-relative text patches are supported.")
                relative_path(value[2:])
    return operation


def prepare(directory: pathlib.Path) -> dict:
    plan = json.loads(os.environ["RTA_PLAN_JSON"])
    approved_hash = os.environ["RTA_APPROVED_PLAN_HASH"]
    patch = decode_patch(os.environ.get("RTA_PATCH", ""))
    operation = validate_plan(plan, approved_hash, patch)
    if not os.environ.get("GITHUB_ACTIONS") or not os.environ.get("RUNNER_TEMP"):
        raise WorkbenchError("Untrusted repository builds may run only in the configured disposable GitHub runner.")
    directory = directory.resolve()
    temp = pathlib.Path(os.environ["RUNNER_TEMP"]).resolve()
    if not directory.is_relative_to(temp) or directory == temp:
        raise WorkbenchError("Runner outputs must be beneath RUNNER_TEMP.")
    directory.mkdir(parents=True, exist_ok=True)
    if os.environ.get("GITHUB_SHA") != plan["runner"]["commit"] or os.environ.get("GITHUB_REPOSITORY") != plan["runner"]["repository"]:
        raise WorkbenchError("Runner code identity differs from the approved plan.")
    artifacts = directory / "artifacts"
    artifacts.mkdir(exist_ok=True)
    write_json(directory / "plan.json", plan)
    (directory / "port.patch").write_text(patch, encoding="utf-8", newline="\n")
    metadata = {"operation": operation, "approvedPlanHash": approved_hash, "runId": plan["runId"],
                "toolkitCommit": os.environ.get("GITHUB_SHA"), "githubRunId": os.environ.get("GITHUB_RUN_ID")}
    write_json(directory / "invocation.json", metadata)
    write_json(artifacts / "invocation.json", metadata)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"sdk={plan['sdkVersion']}\nrun_id={plan['runId']}\noperation={operation}\n")
    print(json.dumps(metadata))
    return metadata


def execute(directory: pathlib.Path) -> None:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise WorkbenchError("Refusing to execute repository builds outside the isolated runner.")
    plan = read_json(directory / "plan.json")
    invocation = read_json(directory / "invocation.json")
    patch = (directory / "port.patch").read_bytes().decode("utf-8")
    operation = validate_plan(plan, invocation["approvedPlanHash"], patch)
    source = directory / "source"
    git(["clone", "--no-checkout", "--filter=blob:none", f"https://github.com/{plan['repository']}.git", str(source)],
        home=directory / "git-home")
    git(["checkout", "--detach", plan["sourceCommit"]], cwd=source, home=directory / "git-home")
    if tree_hash(source) != plan["sourceHash"]:
        raise WorkbenchError("Runner source bytes differ from the approved source snapshot.")
    if operation == "candidate":
        git(["apply", "--check", str(directory / "port.patch")], cwd=source, home=directory / "git-home")
        git(["apply", str(directory / "port.patch")], cwd=source, home=directory / "git-home")
        if tree_hash(source) != plan["workingSourceHash"]:
            raise WorkbenchError("Applied source patch differs from the reviewed working tree.")
    projects = discover_projects(source)
    if test_commands_for(projects, "<isolated-output>/tests") != plan["testCommands"]:
        raise WorkbenchError("Detected test projects differ from the approved test commands.")
    dotnet = pathlib.Path(os.environ["RTA_DOTNET_PATH"]).resolve()
    if not dotnet.is_file() or not dotnet.is_relative_to(pathlib.Path(os.environ["RUNNER_TEMP"]).resolve()):
        raise WorkbenchError("The exact SDK must come from the isolated runner installation.")
    actual_sdk = subprocess.check_output([str(dotnet), "--version"], cwd=source, text=True).strip()
    if actual_sdk != plan["sdkVersion"]:
        raise WorkbenchError(f"SDK mismatch: expected {plan['sdkVersion']}, found {actual_sdk}.")
    output = directory / "artifacts"
    output.mkdir(exist_ok=True)
    logs = output / "logs"
    logs.mkdir(exist_ok=True)
    commands = []
    runtime_ids = ["win-x64"] if operation == "baseline" else ["win-x64", "win-arm64"]
    environment = {**os.environ, "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1",
                   "DOTNET_ROOT": str(dotnet.parent), "DOTNET_MULTILEVEL_LOOKUP": "0"}

    def isolated_source(label: str) -> pathlib.Path:
        target = directory / "sources" / label
        target.parent.mkdir(exist_ok=True)
        git(["worktree", "add", "--detach", str(target), plan["sourceCommit"]], cwd=source, home=directory / "git-home")
        if operation == "candidate":
            git(["apply", str(directory / "port.patch")], cwd=target, home=directory / "git-home")
        if tree_hash(target) != plan.get("workingSourceHash", plan["sourceHash"]):
            raise WorkbenchError("Isolated source does not reproduce the exact approved input tree.")
        return target

    for runtime in runtime_ids:
        working = isolated_source(runtime)
        publish = directory / "publish" / runtime
        for index, command in enumerate(commands_for(plan["project"], plan["framework"], runtime, str(publish))):
            command[0] = str(dotnet)
            log = logs / f"{runtime}-{index}.log"
            with log.open("w", encoding="utf-8") as stream:
                completed = subprocess.run(command, cwd=working, env=environment, stdout=stream, stderr=subprocess.STDOUT,
                                           text=True, timeout=1200)
            commands.append({"arguments": command, "workingDirectory": str(working), "exitCode": completed.returncode, "log": log.name})
            write_json(output / "commands.json", commands)
            if completed.returncode:
                raise WorkbenchError(f"{runtime} {command[1]} failed; see {log.name}.")
        manifest = tree_manifest(publish)
        if not any(item["path"].lower().endswith(".exe") for item in manifest):
            raise WorkbenchError("Published package contains no executable.")
        write_json(output / f"{runtime}-files.json", manifest)
        archive = output / f"{runtime}.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for item in manifest:
                bundle.write(publish / pathlib.PurePosixPath(item["path"]), item["path"])
    tests = [project for project in projects if project["isTestProject"]]
    test_results = []
    test_source = isolated_source("tests") if tests else None
    for index, project in enumerate(tests):
        log = logs / f"test-{index}.log"
        command = [str(dotnet), "test", project["path"], "-c", "Release", "--logger", "trx", "--results-directory", str(output / "tests")]
        with log.open("w", encoding="utf-8") as stream:
            completed = subprocess.run(command, cwd=test_source, env=environment, stdout=stream, stderr=subprocess.STDOUT,
                                       text=True, timeout=1200)
        test_results.append({"project": project["path"], "exitCode": completed.returncode, "log": log.name})
        write_json(output / "test-execution.json", test_results)
        if completed.returncode:
            raise WorkbenchError(f"Existing tests failed: {project['path']}.")
    receipt = {
        "schemaVersion": 1, "completedAt": now(), **invocation,
        "repository": plan["repository"], "sourceCommit": plan["sourceCommit"], "sdkVersion": actual_sdk,
        "patchSha256": plan.get("patchSha256"), "tests": test_results,
        "testCoverage": "existing-projects-executed" if tests else "no-test-projects-detected",
        "packages": [{"runtime": rid, "file": f"{rid}.zip", "sha256": file_hash(output / f"{rid}.zip"),
                      "bytes": (output / f"{rid}.zip").stat().st_size} for rid in runtime_ids],
    }
    write_json(output / "build-receipt.json", receipt)
    print(json.dumps(receipt, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("prepare", "execute"))
    parser.add_argument("--directory", required=True, type=pathlib.Path)
    args = parser.parse_args()
    try:
        if args.operation == "prepare":
            prepare(args.directory)
        else:
            execute(args.directory)
    except (WorkbenchError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        runner_temp = os.environ.get("RUNNER_TEMP")
        safe_failure_root = runner_temp and args.directory.resolve().is_relative_to(pathlib.Path(runner_temp).resolve()) and args.directory.resolve() != pathlib.Path(runner_temp).resolve()
        if safe_failure_root and args.directory.is_dir():
            write_json(args.directory / "artifacts" / "failure.json", {"at": now(), "error": str(error)})
        raise


if __name__ == "__main__":
    main()
